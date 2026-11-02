# breakdown.py validation record

## Status

Validated on synthetic video only. **Not yet run on the real episode**: `~/Downloads/دروب ريمي الحلقة 1 كاملة.mp4` was not reachable from the cloud session, so G0 remains open.

Environment: Linux cloud container, ffmpeg with drawtext, Python 3 (stdlib only).

## Test 1: 23.976 fps synthetic episode, 214 frames

The filename was Arabic with spaces. The picture was letterboxed 640×272 inside 640×360, with an AAC sine track. Ground truth was built from segments with known frame counts.

| Check | Expected | Result |
|---|---|---|
| Decoded frame count | 214 | 214 ✅ |
| Hard cuts (incl. a 3-frame shot) | 60, 63, 113, 125, 165 | 60, 63, 113, 125, 165 ✅ (exact) |
| Cross-dissolve, frames ≈183–195 | a gradual transition | **Not detected** ❌. Max scene score in the dissolve < 1e-4. Known limit. |
| Black interval | 4.713–5.214 s (frames 113–124) | 4.71304–5.21354 s ✅ |
| Freeze ≥ 1 s | 5.214–6.882 s (static 40-frame shot) | 5.21354–6.88187 s ✅ |
| Letterbox | 640:272:0:44 | 640:272:0:44 ✅ |
| Interlace verdict | progressive | progressive (213 P / 1 undetermined) ✅ |
| Keyframes are the exact frame | 18/18 | 18/18 ✅ |
| Audio extraction + loudness | WAV written, LUFS reported | −21.8 LUFS ✅ |

How exactness was checked: each keyframe was compared by PSNR against independently decoded frames n−1, n and n+1. A tie on pixel-identical static frames counts as a pass.

## Test 2: interlaced 720×576, 25 fps TFF

The verdict was "interlaced or telecined: deinterlace / IVTC before analysis" ✅.

## Test 3: safety

| Case | Result |
|---|---|
| Output directory = the source's folder | Refused ✅ |
| Missing source | Clean error ✅ |
| Source file after runs | Byte size unchanged ✅. The source is only opened as an ffmpeg input. |

## Before trusting it on the episode

- [ ] Run on the real MP4 on the Mac. Record the ffmpeg version, and whether drawtext is present: Homebrew builds differ.
- [ ] Hand-check every cut on the contact sheets plus a sample of 0.2 s strips. Log misses (dissolves, Dezaki-style freeze-frame "postcard" transitions) and false cuts (flashes, whip pans). Tune `--threshold` from `scene_scores.csv`, or add a learned detector.
- [ ] If `idet` says interlaced or telecined: run IVTC/deinterlace into a working mezzanine first, re-run, and keep the original untouched.
