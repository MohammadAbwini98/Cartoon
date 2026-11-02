// node tests/make_chunks.mjs <file> <out_dir> <bin|b64> [chunkBytes]
// Produces RMSC1 chunks exactly as the browser uploader does (same core
// module), named by a random id like real asset ids, and prints
// {transferId, sha256, size, total} as JSON.
import { createRequire } from "node:module";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { randomBytes } from "node:crypto";
const require = createRequire(import.meta.url);
const T = require("../uploader/transfer-core.js");

const [file, outDir, enc, chunkOverride] = process.argv.slice(2);
const data = readFileSync(file);
if (chunkOverride) T.CHUNK_BYTES[enc] = +chunkOverride;
mkdirSync(outDir, { recursive: true });
const firstMiB = new T.Sha256().update(data.subarray(0, 1048576)).hex();
const tid = T.transferId(firstMiB, data.length);
const plan = T.planChunks(data.length, enc);
const whole = new T.Sha256();
for (const c of plan) {
  const payload = data.subarray(c.start, c.end);
  whole.update(payload);
  const sha = new T.Sha256().update(payload).hex();
  const head = Buffer.from(T.header(tid, c.index, plan.length, payload.length, sha, enc), "ascii");
  const body = enc === "b64" ? Buffer.from(payload.toString("base64"), "ascii") : payload;
  writeFileSync(`${outDir}/${randomBytes(16).toString("hex")}`, Buffer.concat([head, body]));
}
console.log(JSON.stringify({ transferId: tid, sha256: whole.hex(), size: data.length, total: plan.length }));
