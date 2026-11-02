"""Assemble a remade shot with the original soundtrack, frame-exact.

  python3 -m remake_studio.assemble shot <frames_dir> <episode.mp4> <start_frame> <end_frame_excl> <out.mp4>
        [--size 1920x1080]
  python3 -m remake_studio.assemble compare <episode.mp4> <start_frame> <end_frame_excl> <remake.mp4> <out.mp4>

`shot` requires exactly end-start frames, encodes them at the episode's frame
rate, and cuts the episode's audio for [start/fps, end/fps) by sample-accurate
trimming of decoded audio (no keyframe seeking). The result is probed: frame
count, duration and audio length must match, or the file is not kept.
`compare` writes source | remake side by side over the original audio.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from fractions import Fraction

from remake_studio.media import validate


class AssemblyError(Exception):
    pass


def _ff(*args):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args],
                       capture_output=True, text=True)
    if r.returncode:
        raise AssemblyError(r.stderr.strip()[-800:])


def episode_fps(path: str) -> Fraction:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate",
                        "-of", "csv=p=0", path], capture_output=True, text=True, check=True)
    return Fraction(r.stdout.strip())


def frames_in(frames_dir: str) -> list[str]:
    exts = (".png", ".jpg", ".jpeg", ".webp")
    return sorted(os.path.join(frames_dir, f) for f in os.listdir(frames_dir) if f.lower().endswith(exts))


def assemble_shot(frames_dir: str, episode: str, start: int, end: int, out: str, size: str | None = None,
                  audio: bool = True) -> dict:
    n = end - start
    files = frames_in(frames_dir)
    if len(files) != n:
        raise AssemblyError(f"shot needs exactly {n} frames ({start}..{end - 1}); found {len(files)} in {frames_dir}")
    fps = episode_fps(episode)
    t0, t1 = start / fps, end / fps
    lst = out + ".frames.txt"
    with open(lst, "w") as f:
        for p in files:
            f.write(f"file '{os.path.abspath(p)}'\n")
    vf = "format=yuv420p" if not size else f"scale={size.replace('x', ':')}:flags=lanczos,format=yuv420p"
    tmp = out + ".part.mp4"
    args = ["-r", str(fps), "-f", "concat", "-safe", "0", "-i", lst]
    if audio:
        args += ["-i", episode, "-filter_complex",
                 f"[1:a:0]atrim=start={float(t0):.9f}:end={float(t1):.9f},asetpts=PTS-STARTPTS[a]",
                 "-map", "0:v:0", "-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    else:
        args += ["-map", "0:v:0"]
    try:
        _ff(*args, "-vf", vf, "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-r", str(fps),
            "-fps_mode", "cfr", "-movflags", "+faststart", "-f", "mp4", tmp)
        probe, problems = validate.check(tmp, {"frames": n, "fps": str(fps), **({"has_audio": True} if audio else {})})
        want = float(n / fps)
        if abs(probe.get("duration_s", 0) - want) > 1.5 / float(fps):
            problems.append(f"duration {probe.get('duration_s')} s, expected {want:.4f} s")
        if audio:
            a = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration",
                                "-of", "csv=p=0", tmp], capture_output=True, text=True).stdout.strip()
            if not a or abs(float(a) - want) > 0.05:
                problems.append(f"audio length {a} s, expected {want:.4f} s")
        if problems:
            raise AssemblyError("; ".join(problems))
        os.replace(tmp, out)
    finally:
        for p in (tmp, lst):
            if os.path.exists(p):
                os.remove(p)
    return {"out": out, "frames": n, "fps": str(fps), "start_s": float(t0), "end_s": float(t1),
            "sha256": validate.sha256(out), "probe": probe}


def compare(episode: str, start: int, end: int, remake: str, out: str, height: int = 540) -> str:
    fps = episode_fps(episode)
    t0, t1 = float(start / fps), float(end / fps)
    _ff("-i", episode, "-i", remake, "-filter_complex",
        f"[0:v]trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS,scale=-2:{height}[s];"
        f"[1:v]setpts=PTS-STARTPTS,scale=-2:{height}[r];[s][r]hstack=inputs=2,format=yuv420p[v];"
        f"[0:a:0]atrim=start={t0:.9f}:end={t1:.9f},asetpts=PTS-STARTPTS[a]",
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-crf", "18", "-c:a", "aac", "-r", str(fps), out)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("shot")
    s.add_argument("frames_dir"); s.add_argument("episode"); s.add_argument("start", type=int)
    s.add_argument("end", type=int); s.add_argument("out"); s.add_argument("--size"); s.add_argument("--no-audio", action="store_true")
    c = sub.add_parser("compare")
    c.add_argument("episode"); c.add_argument("start", type=int); c.add_argument("end", type=int)
    c.add_argument("remake"); c.add_argument("out")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "shot":
            print(json.dumps(assemble_shot(a.frames_dir, a.episode, a.start, a.end, a.out, a.size, not a.no_audio), indent=2))
        else:
            print(compare(a.episode, a.start, a.end, a.remake, a.out))
    except AssemblyError as e:
        print(f"assembly failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
