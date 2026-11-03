# Cloud storage (Cloudflare R2)

Status: **implemented, not yet verified against R2.** Verified only against a local S3 stand-in that enforces
SigV4 signatures and link expiry, plus AWS's published header and presigned signing examples.

## Two buckets, three credentials

| Bucket | Holds | Who has credentials |
|---|---|---|
| `remake-queue` | signed jobs, claims, status, heartbeats, diagnostics, worker results | cloud orchestrator token **and** Mac token |
| `remake-media` | uploaded episode, verified sources, renders | cloud orchestrator token only |

The Mac never holds media credentials: a job carries presigned, expiring GET links for its inputs, and the
worker accepts input links only over HTTPS from the R2 host in its config. The browser never holds any
credential: it gets presigned links for exactly the parts of one object.

## Create in the Cloudflare dashboard (R2)

1. **Buckets:** `remake-queue` and `remake-media`, location automatic. Leave public access **off** on both
   (no r2.dev URL, no custom domain).
2. **API tokens** (R2 → Manage API tokens → Create API token), each "Object Read & Write", scoped with
   "Apply to specific buckets only":
   * `remake-cloud`: `remake-queue` + `remake-media`
   * `remake-mac`: `remake-queue` only
   Each token gives an Access Key ID and a Secret Access Key, shown once.
3. **Lifecycle rules** (each bucket → Settings → Object lifecycle rules):
   * `remake-media`: abort incomplete multipart uploads after 7 days; delete prefix `upload-pages/` after 7 days.
   * `remake-queue`: delete prefix `inputs/` and `results/` after 30 days (copies live in media).
4. **CORS:** not needed. The upload page is served from the same R2 origin as its upload links.
5. Note your **account id** (R2 overview); the endpoint is `https://<account-id>.r2.cloudflarestorage.com`.

## Where each credential goes

| Value | Cloud environment secret (environment settings → Edit) | Mac (`~/.remake-worker/secrets.json`, written by setup) |
|---|---|---|
| `remake-cloud` key id / secret | `REMAKE_STORE_ACCESS_KEY`, `REMAKE_STORE_SECRET_KEY` and `REMAKE_MEDIA_ACCESS_KEY`, `REMAKE_MEDIA_SECRET_KEY` (same token) | — |
| `remake-mac` key id / secret | — | prompted by setup, hidden, stored mode 600 |
| job-signing key | `REMAKE_JOB_SIGNING_KEY` = contents of the Mac's `~/.remake-worker/job-signing.key` | created by setup |

Then set the account id in `config/hybrid.toml` (both endpoints). Never paste a key into the chat.

## Verification to run once credentials exist (Claude runs these)

```bash
python3 -m remake_studio.storage check
```
Checks read/write/list/delete and conditional writes on the media bucket. The queue bucket is checked
the same way by the orchestrator (`remake hybrid status`) and by the Mac setup's storage step.

```bash
python3 -m remake_studio.storage prepare "دروب ريمي الحلقة 1 كاملة.mp4" <bytes>
```
Creates the multipart upload and prints the upload link for you.

```bash
python3 -m remake_studio.storage complete <upload id>
```
Completes the upload, downloads the object from R2 and compares its SHA-256 with your browser's.

Storage is "VERIFIED" only after `check` passes and one real upload completes with matching hashes.
