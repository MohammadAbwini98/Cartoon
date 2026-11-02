"""Synthetic clips with known ground truth, generated with ffmpeg lavfi.

Every clip is encoded near-losslessly (x264 crf 8) so detector behaviour is
not dominated by compression noise; a separate test re-encodes at crf 28.
"""
import os
import subprocess

R = "24"


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args], check=True)


def seg(out, src, frames, vf="", rate=R, size="640x360"):
    """One segment of exactly `frames` frames from a lavfi source."""
    chain = src if "=" in src else f"{src}=s={size}:r={rate}"
    filt = ",".join(x for x in (vf, "format=yuv420p", "setsar=1") if x)
    _ff("-f", "lavfi", "-i", chain, "-frames:v", str(frames), "-vf", filt, "-r", rate,
        "-c:v", "libx264", "-crf", "8", "-g", "48", out)
    return out


def concat(out, parts, crf="8"):
    inputs = []
    for p in parts:
        inputs += ["-i", p]
    n = len(parts)
    _ff(*inputs, "-filter_complex", "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-crf", crf, "-g", "48", out)
    return out


def xfade(out, a, b, a_frames, dur_frames, rate=24, transition="fade"):
    """Cross-dissolve: the transition occupies frames [a_frames - dur_frames, a_frames)."""
    off = (a_frames - dur_frames) / rate
    _ff("-i", a, "-i", b, "-filter_complex",
        f"[0:v][1:v]xfade=transition={transition}:duration={dur_frames / rate}:offset={off}",
        "-c:v", "libx264", "-crf", "8", "-g", "48", out)
    return out


def frames(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path],
                       capture_output=True, text=True, check=True)
    return int(r.stdout.strip())


# ---------------------------------------------------------------- scenarios
# Each returns (path, truth) where truth = {"gradual": [(start, end_excl, kind)],
# "cuts": [...], "flashes": [(start, end_excl, returns_to_same)]}

def static_dissolve(d):
    a = seg(f"{d}/a.mp4", "smptebars", 30)
    b = seg(f"{d}/b.mp4", "color=c=0xB03020:s=640x360:r=24", 30)
    return xfade(f"{d}/static_dissolve.mp4", a, b, 30, 12), {"gradual": [(18, 30, "dissolve")], "cuts": []}


def moving_dissolve(d):
    a = seg(f"{d}/a.mp4", "testsrc2", 40)
    b = seg(f"{d}/b.mp4", "mandelbrot", 40)
    return xfade(f"{d}/moving_dissolve.mp4", a, b, 40, 18), {"gradual": [(22, 40, "dissolve")], "cuts": []}


def long_dissolve_23976(d):
    r = "24000/1001"
    a = seg(f"{d}/a.mp4", "smptebars", 60, rate=r)
    b = seg(f"{d}/b.mp4", "testsrc", 60, rate=r)
    return xfade(f"{d}/long_dissolve.mp4", a, b, 60, 36, rate=24000 / 1001), \
        {"gradual": [(24, 60, "dissolve")], "cuts": []}


def fade_out_in(d):
    a = seg(f"{d}/a.mp4", "testsrc2", 36, vf="fade=t=out:st=1:d=0.5")      # frames 24..35 fade to black
    b = seg(f"{d}/b.mp4", "smptebars", 36, vf="fade=t=in:st=0:d=0.5")      # frames 0..11 fade from black
    return concat(f"{d}/fade_out_in.mp4", [a, b]), \
        {"gradual": [(24, 36, "fade_out"), (36, 48, "fade_in")], "cuts": []}


def slow_pan(d):
    big = f"{d}/big.png"
    _ff("-f", "lavfi", "-i", "mandelbrot=s=1600x360", "-frames:v", "1", big)
    out = f"{d}/slow_pan.mp4"
    _ff("-loop", "1", "-i", big, "-frames:v", "72", "-vf",
        "crop=640:360:x='min(n*8\\,960)':y=0,format=yuv420p,setsar=1", "-r", R,
        "-c:v", "libx264", "-crf", "8", out)
    return out, {"gradual": [], "cuts": []}


def zoom(d):
    big = f"{d}/big.png"
    _ff("-f", "lavfi", "-i", "testsrc=s=1280x720", "-frames:v", "1", big)
    out = f"{d}/zoom.mp4"
    _ff("-loop", "1", "-i", big, "-frames:v", "72", "-vf",
        "zoompan=z='1+0.012*on':d=1:s=640x360:fps=24,format=yuv420p,setsar=1",
        "-c:v", "libx264", "-crf", "8", out)
    return out, {"gradual": [], "cuts": []}


def moving_object(d):
    out = f"{d}/moving_object.mp4"
    _ff("-f", "lavfi", "-i", "color=c=0x305080:s=640x360:r=24", "-f", "lavfi", "-i",
        "color=c=0xF0D060:s=160x160:r=24", "-filter_complex",
        "[0:v][1:v]overlay=x='40+n*7':y=120:shortest=1,format=yuv420p,setsar=1[v]",
        "-map", "[v]", "-frames:v", "72", "-c:v", "libx264", "-crf", "8", out)
    return out, {"gradual": [], "cuts": []}


def flash_same_shot(d):
    a = seg(f"{d}/a.mp4", "smptebars", 20)
    w = seg(f"{d}/w.mp4", "color=c=white:s=640x360:r=24", 2)
    return concat(f"{d}/flash_same.mp4", [a, w, a]), \
        {"gradual": [], "cuts": [], "flashes": [(20, 22, True)]}


def flash_into_new_shot(d):
    a = seg(f"{d}/a.mp4", "smptebars", 20)
    w = seg(f"{d}/w.mp4", "color=c=white:s=640x360:r=24", 2)
    b = seg(f"{d}/b.mp4", "color=c=0xB03020:s=640x360:r=24", 20)
    return concat(f"{d}/flash_new.mp4", [a, w, b]), \
        {"gradual": [], "cuts": [20, 22], "flashes": [(20, 22, False)]}


def episode(d, crf="8"):
    """Hard cuts + 3-frame shot + dissolve + fade out/in + flash inside a shot."""
    p = [seg(f"{d}/e1.mp4", "testsrc2", 48),                       # 0..47
         seg(f"{d}/e2.mp4", "smptebars", 3)]                       # 48..50 (3-frame shot)
    x = xfade(f"{d}/ex.mp4", seg(f"{d}/e3.mp4", "mandelbrot", 40),
              seg(f"{d}/e4.mp4", "color=c=0x208040:s=640x360:r=24", 40), 40, 16)   # 51..114, dissolve 75..90
    p.append(x)
    p.append(seg(f"{d}/e5.mp4", "testsrc", 36, vf="fade=t=out:st=1:d=0.5"))       # 115..150, fade 139..150
    p.append(seg(f"{d}/e6.mp4", "rgbtestsrc", 30, vf="fade=t=in:st=0:d=0.5"))     # 151..180, fade 151..162
    bars = seg(f"{d}/e7.mp4", "smptehdbars", 20)
    p += [bars, seg(f"{d}/e8.mp4", "color=c=white:s=640x360:r=24", 2), bars]      # 181..200, flash 201..202, 203..222
    return concat(f"{d}/episode_crf{crf}.mp4", p, crf=crf), {
        "frames": 223,
        "cuts": [48, 51, 115, 181],
        "gradual": [(75, 91, "dissolve"), (139, 151, "fade_out"), (151, 163, "fade_in")],
        "flashes": [(201, 203, True)],
    }
