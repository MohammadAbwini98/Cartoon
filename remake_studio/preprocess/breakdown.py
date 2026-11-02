#!/usr/bin/env python3
"""Episode preprocessing, stage 1: source verification and shot breakdown.

Read-only on the source: the MP4 is only opened as an ffprobe/ffmpeg input,
and the output directory may not contain it. Needs ffmpeg/ffprobe only.

  python3 -m remake_studio.preprocess.breakdown "<episode.mp4>" <out_dir> [--threshold 0.30]

Three decode passes over the video, plus one over the audio:
  1. analysis: per-frame scene score, interlace (idet), black and frozen
     intervals, letterbox (cropdetect), exact decoded frame count
  2. thumbnails (64x36): flash classification and dissolve/fade detection by
     blend fit (transitions.py)
  3. keyframes: first / middle / last frame of every segment, selected by exact
     frame index (no timestamp seeking, so no off-by-one at 23.976/29.97)

Writes to <out_dir>:
  source.json          sha256, size, ffprobe output, fps, decoded frame count
  signal.json          idet verdict, black/freeze intervals, crop, loudness
  scene_scores.csv     frame, pts_time, score (retune cuts without re-decoding)
  shots.csv / .json    frame-exact segments ([start, end) frames + timecode), each
                       kind = shot | dissolve | fade_in | fade_out | flash;
                       transitions carry per-frame blend weights
  transitions.json     detected gradual transitions, flashes, detector params
  keyframes/           shot_NNNN_{first,mid,last}.jpg
  contact_sheet_NN.jpg 5x4 mid keyframes per sheet (labelled if drawtext exists)
  audio.wav            first audio stream, 48 kHz stereo PCM (analysis copy)
  manifest.json        per-shot records with empty slots for character refs,
                       generated assets, model parameters, outputs and QA

Known limits:
  * Dissolve/fade thresholds are tuned on synthetic clips only; calibrate on
    the real episode. A gradual in-shot lighting change fits the blend model
    and can be reported as a dissolve. Whip pans can still create false cuts.
  * Assumes constant frame rate; warns if the container reports otherwise.
  * No speech detection: dialogue timing needs vocal separation + ASR (stage 2).
"""
import argparse, csv, hashlib, json, os, re, shutil, subprocess, sys
from fractions import Fraction

from remake_studio.preprocess import transitions


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} … failed ({r.returncode}):\n{r.stderr[-1500:]}")
    return r


def ff(*args):
    return run(["ffmpeg", "-hide_banner", "-nostdin", *args])


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def has_filter(name):
    return re.search(rf"^\s*\S+\s+{name}\s", run(["ffmpeg", "-hide_banner", "-filters"]).stdout, re.M) is not None


def dump_json(path, obj, **kw):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, **kw)


def tc(frame, fps):
    s = float(frame / fps)
    return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}"


def analysis_pass(src):
    """One decode: scene score for every frame + signal checks."""
    vf = ("idet,blackdetect=d=0.04:pix_th=0.10,freezedetect=n=0.003:d=1,cropdetect=round=2,"
          "select='gte(scene,0)',metadata=print:key=lavfi.scene_score")
    err = ff("-i", src, "-map", "0:v:0", "-vf", vf, "-f", "null", "-").stderr
    scores = [(int(f), float(t), float(s)) for f, t, s in re.findall(
        r"frame:(\d+)\s+pts:\S+\s+pts_time:([-\d.]+)\s*\n.*?lavfi\.scene_score=([\d.]+)", err)]
    idet = re.findall(r"Multi frame detection: TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)", err)
    tff, bff, prog, und = map(int, idet[-1]) if idet else (0, 0, 0, 0)
    fs = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", err)]
    fe = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", err)]
    crops = re.findall(r"crop=(\d+:\d+:\d+:\d+)", err)
    sig = {
        "idet": {"tff": tff, "bff": bff, "progressive": prog, "undetermined": und,
                 "verdict": "interlaced or telecined: deinterlace / IVTC before analysis"
                 if tff + bff > 0.1 * max(1, tff + bff + prog) else "progressive"},
        "black_intervals_s": [{"start": float(a), "end": float(b)} for a, b in
                              re.findall(r"black_start:([\d.]+)\s+black_end:([\d.]+)", err)],
        "freeze_intervals_ge_1s": [{"start": s, "end": fe[i] if i < len(fe) else None}
                                   for i, s in enumerate(fs)],
        "dominant_crop": max(set(crops), key=crops.count) if crops else None,
    }
    return scores, sig


def pick_cuts(scores, threshold, min_frames, nframes):
    """Cut before frame f when its score passes the threshold and is the local
    maximum within ±2 frames (one cut per 2-3 frame transition, not several)."""
    s = {f: v for f, _, v in scores}
    cuts = []
    for f, _, v in scores:
        if v >= threshold and f > 0:
            if f - (cuts[-1] if cuts else 0) >= min_frames and nframes - f >= min_frames:
                cuts.append(f)
    return cuts


def build_segments(nframes, fps, cuts, gradual, flashes, short_frames):
    """Partition [0, nframes) into shots and transitions, frame-exact.

    A dissolve or fade becomes its own segment between the shots it joins and
    carries the per-frame blend weights. A flash that leads into a new
    picture is labelled. Segments shorter than `short_frames` that are not
    transitions or flashes are flagged for manual review.
    """
    bounds = {0, nframes, *cuts}
    for t in gradual:
        bounds.update((t.start, t.end_excl))
    bounds = sorted(b for b in bounds if 0 <= b <= nframes)
    by_start = {t.start: t for t in gradual}
    flash_at = {(f["start_frame"], f["end_frame_excl"]) for f in flashes if not f["returns_to_same_shot"]}
    segs = []
    for i in range(len(bounds) - 1):
        s, e = bounds[i], bounds[i + 1]
        r = {"shot": i + 1, "kind": "shot", "start_frame": s, "end_frame_excl": e, "frames": e - s,
             "start_tc": tc(s, fps), "end_tc": tc(e, fps),
             "pick": {"first": s, "mid": (s + e - 1) // 2, "last": e - 1}}
        t = by_start.get(s)
        if t is not None and t.end_excl == e:
            r.update(kind=t.kind, transition=t.as_dict(), from_segment=i if i > 0 else None,
                     to_segment=i + 2 if e < nframes else None)
        elif (s, e) in flash_at:
            r["kind"] = "flash"
        elif e - s < short_frames:
            r["review"] = True
        segs.append(r)
    return segs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("out")
    ap.add_argument("--threshold", type=float, default=0.30, help="scene score 0-1 (default 0.30)")
    ap.add_argument("--min-frames", type=int, default=1,
                    help="shortest shot kept, in frames (default 1: keep every cut; flashes are resolved separately)")
    ap.add_argument("--short-frames", type=int, default=3,
                    help="flag non-transition segments shorter than this for review (default 3)")
    a = ap.parse_args(argv)

    src, out = os.path.realpath(a.video), os.path.realpath(a.out)
    if not os.path.isfile(src):
        sys.exit(f"source not found or not a regular file: {a.video}")
    if os.path.dirname(src) == out or src.startswith(out + os.sep):
        sys.exit("refusing: the output directory must not contain the source video")
    os.makedirs(os.path.join(out, "keyframes"), exist_ok=True)

    print("1/5 hash + probe")
    info = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json",
                           "-show_format", "-show_streams", src]).stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    fps = Fraction(v["avg_frame_rate"]) if v.get("avg_frame_rate", "0/0") != "0/0" else Fraction(v["r_frame_rate"])
    vfr_warning = v.get("r_frame_rate") != v.get("avg_frame_rate")

    print("2/5 analysis pass (scene scores, interlace, black, freeze, crop)")
    scores, sig = analysis_pass(src)
    nframes = len(scores)
    if nframes == 0:
        sys.exit("no frames decoded: is this a video file?")
    lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", ff("-i", src, "-vn", "-af", "ebur128", "-f", "null", "-").stderr) \
        if any(s["codec_type"] == "audio" for s in info["streams"]) else []
    sig["integrated_loudness_lufs"] = float(lufs[-1]) if lufs else None
    sig["vfr_warning"] = vfr_warning

    source = {"path": src, "bytes": os.path.getsize(src), "sha256": sha256(src),
              "fps": str(fps), "fps_float": round(float(fps), 6), "frames_decoded": nframes,
              "duration_s": float(info["format"]["duration"]), "ffprobe": info}
    dump_json(os.path.join(out, "source.json"), source, indent=2, ensure_ascii=False)
    dump_json(os.path.join(out, "signal.json"), sig, indent=2)
    with open(os.path.join(out, "scene_scores.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["frame", "pts_time", "score"]); w.writerows(scores)
    print(f"    {v['codec_name']} {v['width']}x{v['height']} @ {float(fps):.3f} fps, {nframes} frames, "
          f"{source['duration_s']:.2f}s; {sig['idet']['verdict']}; crop {sig['dominant_crop']}; "
          f"{len(sig['black_intervals_s'])} black / {len(sig['freeze_intervals_ge_1s'])} freeze intervals; "
          f"{sig['integrated_loudness_lufs']} LUFS" + ("; WARNING: frame rate may be variable" if vfr_warning else ""))

    print("3/5 shot boundaries, flashes, dissolves and fades")
    cuts = pick_cuts(scores, a.threshold, a.min_frames, nframes)
    crop = sig["dominant_crop"]
    full = f"{v['width']}:{v['height']}:0:0"
    thumbs = transitions.thumbnails(src, crop=crop if crop and crop != full else None)
    if len(thumbs) != nframes:
        raise RuntimeError(f"thumbnail pass decoded {len(thumbs)} frames, analysis pass {nframes}")
    flashes, drop = transitions.detect_flashes(thumbs, cuts)
    cuts = [c for c in cuts if c not in drop]
    gradual = transitions.detect_gradual(thumbs, cuts)
    shots = build_segments(nframes, fps, cuts, gradual, flashes, a.short_frames)
    n_shot = sum(r["kind"] == "shot" for r in shots)
    print(f"    {n_shot} shots, {len(gradual)} gradual transitions "
          f"({', '.join(sorted({t.kind for t in gradual})) or 'none'}), {len(flashes)} flashes "
          f"({len(drop) // 2} inside a shot), {sum(r.get('review', False) for r in shots)} short segments flagged")

    print("4/5 keyframes (exact frame index, one pass)")
    want = sorted({fr for r in shots for fr in r["pick"].values()})
    tmp = os.path.join(out, ".kf")
    os.makedirs(tmp, exist_ok=True)
    script = os.path.join(out, ".kf_filter.txt")
    with open(script, "w") as f:
        f.write("select='" + "+".join(f"eq(n,{n})" for n in want) + "'")
    ff("-loglevel", "error", "-y", "-i", src, "-map", "0:v:0", "-filter_script:v", script,
       "-fps_mode", "passthrough", "-q:v", "2", os.path.join(tmp, "%06d.jpg"))
    got = sorted(os.listdir(tmp))
    if len(got) != len(want):
        raise RuntimeError(f"expected {len(want)} keyframes, decoder returned {len(got)}")
    byframe = dict(zip(want, got))
    for r in shots:
        r["keyframes"] = {}
        for k, fr in r["pick"].items():   # first/mid/last may be one frame in a 1-2 frame shot
            rel = f"keyframes/shot_{r['shot']:04d}_{k}.jpg"
            shutil.copyfile(os.path.join(tmp, byframe[fr]), os.path.join(out, rel))
            r["keyframes"][k] = rel
    shutil.rmtree(tmp); os.remove(script)

    with open(os.path.join(out, "shots.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["shot", "kind", "start_frame", "end_frame_excl", "frames", "start_tc", "end_tc",
                    "review", "mid_keyframe"])
        for r in shots:
            w.writerow([r["shot"], r["kind"], r["start_frame"], r["end_frame_excl"], r["frames"],
                        r["start_tc"], r["end_tc"], int(r.get("review", False)), r["keyframes"]["mid"]])
    dump_json(os.path.join(out, "shots.json"), shots, indent=2)
    dump_json(os.path.join(out, "transitions.json"),
              {"gradual": [t.as_dict() for t in gradual], "flashes": flashes,
               "dropped_flash_cuts": sorted(drop), "params": vars(transitions.Params())}, indent=2)

    print("5/5 contact sheets + audio + manifest")
    label = has_filter("drawtext")
    for p in range(0, len(shots), 20):
        lst = os.path.join(out, ".sheet.txt")
        with open(lst, "w") as f:
            for r in shots[p:p + 20]:
                f.write(f"file '{os.path.join(out, r['keyframes']['mid'])}'\n")
        vf = "scale=320:-2"
        if label:
            vf += (",drawtext=text='%{eif\\:n+" + str(p + 1) + "\\:d}':x=6:y=6:fontsize=24:"
                   "fontcolor=white:box=1:boxcolor=black@0.6")
        ff("-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
           "-vf", vf + ",tile=5x4:padding=4:color=white", "-frames:v", "1",
           os.path.join(out, f"contact_sheet_{p // 20 + 1:02d}.jpg"))
        os.remove(lst)
    if not label:
        print("    note: this ffmpeg build has no drawtext; sheets are unlabelled (order = shot order)")

    if any(s["codec_type"] == "audio" for s in info["streams"]):
        ff("-loglevel", "error", "-y", "-i", src, "-map", "0:a:0", "-c:a", "pcm_s16le",
           "-ar", "48000", "-ac", "2", os.path.join(out, "audio.wav"))

    manifest = {
        "schema": "remake-manifest/0.1",
        "source": {k: source[k] for k in ("path", "sha256", "fps", "frames_decoded", "duration_s")},
        "usage_scope": "private non-commercial research; source and outputs stay private",
        "shots": [{**{k: r[k] for k in ("shot", "kind", "start_frame", "end_frame_excl", "start_tc",
                                        "end_tc", "keyframes")},
                   **({"transition": r["transition"], "from_segment": r["from_segment"],
                       "to_segment": r["to_segment"]} if "transition" in r else {}),
                   "characters": [], "character_refs": [], "background_ref": None,
                   "dialogue": [], "camera": None, "generated_assets": [], "model_params": None,
                   "output_clips": [], "qa": None} for r in shots]}
    dump_json(os.path.join(out, "manifest.json"), manifest, indent=2, ensure_ascii=False)
    print(f"done -> {out}")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        sys.exit(str(e))
