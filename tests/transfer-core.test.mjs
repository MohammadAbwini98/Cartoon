// node tests/transfer-core.test.mjs — checks the browser hashing and chunk
// format against Node's crypto. Exit code 0 = pass.
import { createRequire } from "node:module";
import { createHash, randomBytes } from "node:crypto";
import assert from "node:assert/strict";

const require = createRequire(import.meta.url);
const T = require("../uploader/transfer-core.js");
let n = 0;
const ok = (name) => { n++; console.log("ok", n, "-", name); };

// 1. Known vectors
assert.equal(new T.Sha256().update(new Uint8Array(0)).hex(),
  "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
assert.equal(new T.Sha256().update(new TextEncoder().encode("abc")).hex(),
  "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
ok("FIPS 180-2 vectors");

// 2. Every length around block boundaries, single update
for (let len = 0; len < 300; len++) {
  const b = randomBytes(len);
  assert.equal(new T.Sha256().update(b).hex(), createHash("sha256").update(b).digest("hex"), `len ${len}`);
}
ok("lengths 0..299 match node:crypto");

// 3. Streaming in uneven pieces equals one-shot, on 40 MiB
const big = randomBytes(40 * 1024 * 1024 + 13);
const s = new T.Sha256();
let off = 0, step = 1;
while (off < big.length) { const e = Math.min(big.length, off + step); s.update(big.subarray(off, e)); off = e; step = step * 7 + 3; if (step > 9e6) step = 61; }
const t0 = Date.now();
const one = new T.Sha256().update(big).hex();
const mbps = (big.length / 1048576) / ((Date.now() - t0) / 1000);
assert.equal(s.hex(), createHash("sha256").update(big).digest("hex"));
assert.equal(one, createHash("sha256").update(big).digest("hex"));
ok(`40 MiB streamed in uneven pieces matches (one-shot speed ${mbps.toFixed(0)} MiB/s)`);

// 4. Chunk plan covers the file exactly, chunks under the 20 MiB asset cap
for (const enc of ["bin", "b64"]) {
  for (const size of [1, T.CHUNK_BYTES[enc] - 1, T.CHUNK_BYTES[enc], T.CHUNK_BYTES[enc] + 1, 734003200]) {
    const plan = T.planChunks(size, enc);
    assert.equal(plan[0].start, 0); assert.equal(plan.at(-1).end, size);
    for (let i = 1; i < plan.length; i++) assert.equal(plan[i].start, plan[i - 1].end);
    const maxPayload = Math.max(...plan.map((c) => c.end - c.start));
    const stored = enc === "b64" ? Math.ceil(maxPayload / 3) * 4 : maxPayload;
    assert.ok(stored + 200 < 20 * 1024 * 1024, `${enc} chunk ${stored} too big`);
  }
}
ok("chunk plans partition the file and stay under 20 MiB stored");

// 5. Header round trip, and it never starts with markup
const h = T.header("tabc123-99", 3, 10, 16777216, "a".repeat(64), "bin");
assert.equal(h[0], "R");
assert.deepEqual(T.parseHeader(h), { transferId: "tabc123-99", index: 3, total: 10, payloadLen: 16777216, sha256: "a".repeat(64), enc: "bin" });
assert.throws(() => T.header("bad id!", 0, 1, 1, "a", "bin"));
assert.throws(() => T.parseHeader("<html>"));
ok("header round trip and validation");
console.log(`all ${n} checks passed`);
