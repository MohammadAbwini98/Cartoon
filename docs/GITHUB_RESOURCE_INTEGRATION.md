# GitHub resource integration assessment

Date: 2026-11-01. I inspected these repositories in a cloud session by reading their source, manifests and licences. I did not execute any third-party code. The existing Animation Remake Studio project was not available during this review. Every "integrate" decision below therefore assumes the ComfyUI + ControlNet + AnimateDiff baseline you described. Re-confirm each one against `EXISTING_PIPELINE_AUDIT.md`.

## Bottom line

None of the four repositories is an AI video-generation or video-to-video engine. Three of them are code-drawn renderers: a model writes Canvas, WebGL or p5.js code that paints every pixel.

| | What they cannot do |
|---|---|
| Source input | Ingest source footage, poses, depth or reference images |
| Stylisation | Run diffusion |
| Identity | Preserve a character's identity from a reference image |

The fourth (Video Production Skills) is a set of method documents plus small Python validators.

None of them replaces the ComfyUI stack. Their value lies in four places:
- the render-harness discipline
- QA steps
- the paid-provider task and budget protocol
- character model-sheet and style guidance

Segmentation, pose/depth extraction, identity conditioning, temporal consistency and lip sync must come from our own stack.

## Inventory (as inspected)

| Repo | Commit / date | Licence | Stack | Size | What it is |
|---|---|---|---|---|---|
| [lemomo-ai/lemo-opuscar](https://github.com/lemomo-ai/lemo-opuscar) | `bebbbe1` 2026-11-01 | Code + docs MIT; demo films and stills CC BY 4.0 | Node ≥ 20, playwright-core, three r170. Python tiers: numpy/scipy/soundfile/soxr/pillow; voice adds kokoro-onnx, edge-tts, faster-whisper; music adds numba | 114 MB checkout, 66 styles | Style library + shared toolkit for films drawn in code. README: "No video generation, no stock footage." |
| [JohnHeibel/ClaudeAnimationBase](https://github.com/JohnHeibel/ClaudeAnimationBase) | `0ac8bf2` 2026-09-24 | MIT | p5 ^2.3.3, p5.brush ^2.2.3, puppeteer-core, Chrome | ~1.2k lines of code + 461-line guide | Starter kit for one painted character (Clawd). The author warns watercolour fills run at "seconds per frame" without a dedicated GPU. |
| [francozanardi/papermotion](https://github.com/francozanardi/papermotion) | `9ce8490` 2026-11-01 | MIT | TypeScript 7, Vite 8, Vitest 5, pnpm 11, playwright-core | ~4.8k lines in `src/` | Paper-cutout engine: bone rigs with springs, deterministic physics, gait IK. The README describes it as "Experimental, and made entirely with AI … APIs that change between versions." |
| [Ucmp0001/video-production-skills](https://github.com/Ucmp0001/video-production-skills) | `ea682de` 2026-10-08 | MIT | Python 3.10+ standard library; ffprobe optional | 576 KB | 11 `SKILL.md` workflows + `source_manifest.py`, `check_srt.py`, validators. "Includes no model accounts or API credits and does not guarantee automatic finished videos." |

## Decisions by technique

Key: **I** = integrate into our pipeline · **B** = adapt as an optional rendering backend · **G** = production guidance only · **R** = reject.

### Lemo-Opuscar

| Technique | File | Decision | Reasoning |
|---|---|---|---|
| Deterministic page contract: `window.render(t)`, parallel segment render, lockfile, no partial outputs | `core/render/video.mjs`, `core/README.md` | **R** as an episode renderer; **B** for overlay layers only | Every pixel is drawn by code, and it cannot take source frames or references. It could render title cards, credits, light-band and flare FX layers to composite over diffusion output, but only if the audit finds no existing titling path. |
| Mux and master: two-pass `loudnorm`; refuses missing, damaged or truncated audio; never leaves a partial file | `core/render/mux.sh` | **I** (adapt) | The fail-closed behaviour is worth copying into our final export. Its −14 LUFS target is a web target; set loudness per delivery (EBU R128 broadcast is −23 LUFS). |
| ASR with per-word timestamps (faster-whisper) | `core/tts/asr_check.py` | **I** (adapt the timestamp half only) | Word timestamps are what lip sync and dialogue segmentation need. Its text comparison only special-cases English and CJK; Arabic would fall into exact word matching with no normalisation of alef/hamza forms, tashkeel or ta marbuta, so drop the comparison. It must run on a separated vocal stem: the dub mixes music and effects under dialogue. |
| Review loop: contact sheets every 1–2 s, 0.2 s frame strips on key actions, `ebur128`, `blackdetect`, re-transcribe the final mix | `TECHNIQUE.md` §8 | **I** | Maps directly onto our automated QA. `breakdown.py` already does the black, freeze, loudness and contact-sheet parts. |
| On-screen text reading-time check | `core/render/readcheck.mjs` | **R** | We are not planning burned-in on-screen text. Subtitles are better checked by `check_srt.py` (below). |
| `cel-anime-80s` style prompt: model sheet before any shot; a colour model per lighting set; one protagonist accent colour; characters on twos and acting beats on threes; lip flaps of 3–4 mouth shapes at 8 fps; "borrow the era's craft, never a specific work's characters" | `styles/cel-anime-80s/STYLE.md` | **G** (main input to the style bible) | It is the closest written craft spec to a late-70s/80s TV-anime look. The no-copying rule matches our redesign goal. |
| DIRECTOR.md model-sheet requirement (turnaround, 3–4 expressions, 2–3 key poses, palette, two style frames) and rights rule (§12) | `DIRECTOR.md` | **G** | Adopted as the minimum content of each character bible entry. |
| TTS (Kokoro offline; edge-tts online), sampled score, procedural SFX | `core/tts`, `core/audio` | **R** for final audio; **G** for temporary tracks | Only relevant if the original audio is not cleared. Its tooling targets English and Chinese voices. Arabic replacement dialogue needs a different provider and human casting. |

### ClaudeAnimationBase

| Technique | Decision | Reasoning |
|---|---|---|
| p5.brush painted renderer + Clawd character | **R** | Built around one specific character, with no source conditioning. The painterly look is not the target. |
| Review modes in `render.mjs`: `--strip` (every frame of a stretch), `--crop` (full-res detail), `--crop-at` (a crop that follows a world point through the camera) | **I** (re-implement the idea, not the code) | Following a face or hand crop through every frame is the most effective way to see identity drift and flicker. In our raster pipeline, drive the crop from the segmentation bounding boxes. |
| 31-emotion model sheet + 5 key views; guide rules on timing, readability and "faces act instead of snapping" | **G** | A ready format for the expression and turnaround pages of the character bible. |

### Papermotion

| Technique | Decision | Reasoning |
|---|---|---|
| 2D bone rig (`Skeleton.pose(targets, weight)`), springs, gait IK, deterministic fixed timestep | **R** for now; re-evaluate only if the pilot fails the identity gate | A rigged puppet has zero identity drift by construction, which is diffusion's weakest point. But the engine's stated principle is "motion from springs driven by intents, never keyframing positions." That works against matching source acting frame by frame. It is paper-cutout only, AI-written with changing APIs, and needs a second JS toolchain. If diffusion identity cannot be stabilised, the fallback should be a cut-out rig in a mature 2D tool driven by extracted pose tracks, not this engine. |
| Deterministic rendering, seeded RNG, no wall-clock time | **G** | The same rule applies to us: fixed seeds and recorded parameters per shot in the manifest. |

### Video Production Skills

| Technique | File | Decision | Reasoning |
|---|---|---|---|
| Source manifest: sha256 + ffprobe fields + `rights_status`, never uploads | `skills/video-source-index/scripts/source_manifest.py` | **I** (already folded into `breakdown.py` → `source.json` / `manifest.json`) | Provenance and a per-asset rights field are required deliverables. |
| Paid-generation protocol (detailed below the table) | `skills/generated-broll/SKILL.en.md`, `references/generation-plan.en.md` | **I** as the specification for our provider interface | It matches your requirements almost item for item: budget, authorization, retries, validation, artifact tracking. |
| Subtitle check: characters per second, minimum duration, overlaps | `skills/voice-subtitle-sync/scripts/check_srt.py` | **I** if subtitles are delivered | Tune `--max-cps` for Arabic. Its default of 20 assumes Latin script. |
| Release QA, reference study, source index, workflow resume | `skills/video-release-qa`, `video-reference-study`, … | **G** | Method documents, no executable checks. |

The paid-generation protocol requires:
- a written plan per request: provider, model, price source and date, budget cap, authorized scope and retry cap
- task states: prepared → submitted (task_id) → running → complete → downloaded/inspected
- recovering a timed-out task by its ID before ever resubmitting
- tracking cumulative retry cost
- sanitized receipts with no auth headers or signed URLs
- hashing the original output
- measuring native resolution rather than trusting the prompt

## Source-conditioned examples in the awesome list

All of these are creator accounts. None publishes a project or call log, so none provides reusable code.

| Case | What it shows | Decision |
|---|---|---|
| [@AxtonLiu talking-head redraw](https://x.com/AxtonLiu/status/2102827887732932956) | Original audio, captions and duration kept; new visuals drawn over that timing. | **G**: confirms the "lock picture to the source timeline" approach. |
| [@OriSilver Blender previs → Seedance](https://x.com/OriSilver/status/2102817977812824335) | A reference clip was sampled. Camera, blocking and storyboard were rebuilt in Blender as a ~30 s previs. Previs + the creator's own character were then fed to Seedance 2.5 as references. | **B**, the most relevant pattern for a premium backend: a control video built from the source (pose/depth/line), plus character-bible references, into a reference-to-video model. Candidate for the pilot comparison, subject to budget approval. |
| [@Tariq_at local ComfyUI short](https://x.com/Tariq_at/status/2102561072624410878) | Claims frames were made "100% locally in ComfyUI"; no workflow, models or hardware published. | **R**: nothing inspectable. |
| [@AnduArtist base video + code overlay](https://x.com/AnduArtist/status/2102548178646016377) | A generated base clip with code-drawn animation registered on top. | **G** for overlay compositing. |

## Not covered by any of these resources (gaps for our stack)

- Shot-boundary detection that handles **dissolves**. `breakdown.py` missed a synthetic cross-dissolve: its scores stayed below 1e-4 through the dissolve.
- Character and background segmentation, and tracking masks across a shot.
- Pose, depth and line extraction tuned for **cel animation**. Detectors trained on photos often fail on anime faces and limbs.
- Identity-preserving generation (reference adapters or per-character LoRA) and temporal consistency.
- Arabic dialogue: vocal separation, VAD, ASR and speaker attribution.
- Anime-style lip sync. Photoreal lip-sync models and SyncNet-type metrics are trained on real faces.

Follow-ups not yet done: the third-party test suites were not run, because execution of third-party code was not permitted in this session. Licences were read at the commits listed above. Re-check them before vendoring any file.
