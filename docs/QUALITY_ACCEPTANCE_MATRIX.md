# Quality acceptance matrix (draft)

Status: draft, written before access to the episode or the existing project. The thresholds are proposed starting values. Calibrate them during the pilot against two baselines:
- **the source itself:** many metrics are judged relative to how the original behaves, because cel animation has holds, smears and limited animation that a fixed absolute threshold would mis-score
- **the existing benchmark results**, once the project audit has been read

**Measured** means a script produces the number. **Reviewed** means a person scores it against a rubric, using side-by-side evidence. A reviewed criterion is never reported as a measurement.

## Gates

| Gate | Scope | Must pass before |
|---|---|---|
| G0 Source | Breakdown verified on the real episode; shot list reviewed against the contact sheets | Any redesign work |
| G1 Keyframes | Character and background bible approved; redesigned keyframes for the pilot shots | Any motion render |
| G2 Short pilot | Hardest 2–4 s, local baseline vs any authorized premium candidate | A 30–60 s scene |
| G3 Scene | One complete 30–60 s scene | Full-episode render (needs your explicit approval and budget) |
| G4 Episode | Full delivery | Private viewing (project scope: private, non-commercial research) |

## Measured criteria

Applies from gate G2 onward unless marked otherwise.

| # | Criterion | Metric and tool | Proposed pass threshold |
|---|---|---|---|
| M1 | Frame timing | Output frame count per shot vs source `[start_frame, end_frame)` from `manifest.json` (ffprobe `-count_frames`) | Exact match, 0 frames. Applies at every gate. |
| M2 | Cut placement | Output scene-score peaks at the same frame indices as the source cuts | Every source cut within 0 frames. No new cut with a score above 0.3. |
| M3 | Total duration and A/V length | ffprobe duration of video and audio streams | Video frames = source frames; audio length within 20 ms of video |
| M4 | Missing, black or frozen frames | `blackdetect`, `freezedetect` on the output, diffed against `signal.json` | No black or freeze interval that is absent from the source (±1 frame). No dropped or duplicated frames beyond the source's own hold pattern. |
| M5 | Hold stability (cel holds) | Mean absolute pixel change of the output on frames where the source is a hold | ≤ 1.0 / 255, so held drawings must not "boil" |
| M6 | Temporal flicker | Warping error: source optical flow applied to output frames t→t+1, masked for occlusion; reported as a ratio to the same error on the source | Ratio ≤ 1.25 |
| M7 | Pose preservation | Keypoints on source and output, compared as **normalised joint angles** (absolute positions differ by design because proportions change) | Median angle error ≤ 15°, 90th percentile ≤ 30° per shot. Detector reliability on anime is validated first on 50 hand-labelled frames. |
| M8 | Character identity | Embedding similarity of every character crop to that character's approved bible references (anime-capable image encoder; photo face-recognition models are not valid here) | Per-shot mean ≥ the bible's own inter-view similarity minus 0.05. Shot-to-shot spread ≤ 0.05. |
| M9 | Silhouette and occlusion | IoU of the source vs output character masks (segmentation on both); depth order at overlaps must match | IoU ≥ 0.75 (redesign tolerance). Zero depth-order inversions. |
| M10 | Background stability | Masked background SSIM between consecutive frames after camera-motion compensation | ≥ 0.97 on static-camera shots |
| M11 | Lip sync | Mouth-state timeline (open/closed, by construction if mouths are rendered as a controlled layer) vs the speech-activity timeline from the vocal stem | Onset and offset within ±2 frames for ≥ 95 % of lines. See the dub note below. |
| M12 | Audio identity and sync (original Arabic soundtrack) | Cross-correlation of the output audio vs `audio.wav` | Peak at lag 0 ± 1 ms; no drift across the episode |
| M13 | Loudness | `ebur128` integrated loudness and true peak | Per delivery spec: broadcast −23 LUFS ±1, ≤ −1 dBTP; web −14/−16 LUFS |
| M14 | Resolution and encoding | ffprobe: size, frame rate, pixel format, colour tags; mezzanine + delivery encodes | 1920×1080 minimum; constant frame rate equal to the source fps, or a documented conversion; BT.709 tagged |
| M15 | Reproducibility | Re-render one pilot shot with the recorded seed, models and parameters (MPS kernels are not always bit-exact) | SSIM ≥ 0.98 against the first render |
| M16 | Cost and resources | Per shot: wall time, peak unified memory, GPU-minutes, API spend, logged in the manifest | Peak memory below 15 GB on the 18 GB M3 Pro, leaving room for the OS. API spend within the approved budget. |

## Reviewed criteria

Scored 1–5 against a rubric, from side-by-side, A/B and wipe evidence.

| # | Criterion | Pass |
|---|---|---|
| R1 | Narrative and shot continuity: every story beat present, in order | 5/5 |
| R2 | Acting intent and expressions: the emotion reads the same as in the source | ≥ 4 average; no shot below 3 |
| R3 | Hands, faces and fine detail: no extra or fused fingers, no melting features | No defects in G3 |
| R4 | Edges and compositing: no halos, cut-outs or mask chatter | ≥ 4 |
| R5 | Style consistency with the style bible | ≥ 4 |
| R6 | Overall professional quality, viewed at full speed with sound, start to finish | Sign-off by you |

## Notes

**Dubbed source.** If the Arabic soundtrack was dubbed over animation timed for another language, the source's mouth movements do not match the Arabic dialogue. In that case M11 syncs to the Arabic speech, not to the source mouth shapes.

**Evidence retention.** Every failed render is kept with its parameters. Every gate report links its side-by-side MP4s and metric CSVs.

## Usage scope

Private, non-commercial research and testing (owner's instruction, 2026-11-02). The original Arabic soundtrack,
dialogue, voices, narrative structure, scene order and timing are kept for the private test, and source and
generated media stay private. Copyright clearance is not a pilot or engineering gate. Private use does not by
itself establish permission; distribution or licensing would be reconsidered separately if the scope changes.
