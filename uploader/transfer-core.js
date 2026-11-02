/* Transfer core: incremental SHA-256 and the chunk container format shared by
 * the browser uploader and remake_studio/ingest/reassemble.py.
 *
 * Chunk container, version 1 ("RMSC1"):
 *   one ASCII header line, then the payload
 *   "RMSC1 <transfer_id> <index> <total> <payload_len> <payload_sha256> <enc>\n"
 *   enc = "bin": payload bytes follow as-is (uploaded as video/mp4)
 *   enc = "b64": payload follows base64-encoded (uploaded as text/plain)
 * The header starts with "R", so no chunk ever begins with markup.
 * Works in browsers (window.TransferCore) and Node (module.exports).
 */
(function (root) {
  "use strict";

  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]);

  /** Streaming SHA-256: update() any number of times, then hex(). */
  class Sha256 {
    constructor() {
      this.h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
      this.w = new Uint32Array(64);
      this.buf = new Uint8Array(64);
      this.bufLen = 0;
      this.total = 0;            // bytes hashed (Number is exact to 2^53)
      this.done = false;
    }
    _block(b, off) {
      const w = this.w, h = this.h;
      for (let i = 0; i < 16; i++) {
        const j = off + i * 4;
        w[i] = (b[j] << 24) | (b[j + 1] << 16) | (b[j + 2] << 8) | b[j + 3];
      }
      for (let i = 16; i < 64; i++) {
        const x = w[i - 15], y = w[i - 2];
        const s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
        const s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
      }
      let a = h[0], bb = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
      for (let i = 0; i < 64; i++) {
        const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
        const t1 = (hh + S1 + ((e & f) ^ (~e & g)) + K[i] + w[i]) | 0;
        const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
        const t2 = (S0 + ((a & bb) ^ (a & c) ^ (bb & c))) | 0;
        hh = g; g = f; f = e; e = (d + t1) | 0; d = c; c = bb; bb = a; a = (t1 + t2) | 0;
      }
      h[0] += a; h[1] += bb; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
    }
    update(data) {
      if (this.done) throw new Error("Sha256: update after hex()");
      const b = data instanceof Uint8Array ? data : new Uint8Array(data);
      let i = 0;
      this.total += b.length;
      if (this.bufLen) {
        const take = Math.min(64 - this.bufLen, b.length);
        this.buf.set(b.subarray(0, take), this.bufLen);
        this.bufLen += take; i = take;
        if (this.bufLen === 64) { this._block(this.buf, 0); this.bufLen = 0; }
      }
      for (; i + 64 <= b.length; i += 64) this._block(b, i);
      if (i < b.length) { this.buf.set(b.subarray(i), 0); this.bufLen = b.length - i; }
      return this;
    }
    hex() {
      if (!this.done) {
        const bits = this.total * 8;
        const pad = new Uint8Array(((this.bufLen < 56) ? 56 : 120) - this.bufLen + 8);
        pad[0] = 0x80;
        const hi = Math.floor(bits / 0x100000000), lo = bits >>> 0;
        const n = pad.length;
        pad[n - 8] = hi >>> 24; pad[n - 7] = hi >>> 16; pad[n - 6] = hi >>> 8; pad[n - 5] = hi;
        pad[n - 4] = lo >>> 24; pad[n - 3] = lo >>> 16; pad[n - 2] = lo >>> 8; pad[n - 1] = lo;
        const t = this.total;
        this.update(pad);
        this.total = t;
        this.done = true;
      }
      return Array.from(this.h, (x) => (x >>> 0).toString(16).padStart(8, "0")).join("");
    }
  }

  const MAGIC = "RMSC1";
  const CHUNK_BYTES = { bin: 16 * 1024 * 1024, b64: 14 * 1024 * 1024 };   // b64 grows 4/3 -> < 20 MiB

  function header(transferId, index, total, payloadLen, payloadSha, enc) {
    if (!/^[A-Za-z0-9_-]{4,80}$/.test(transferId)) throw new Error("bad transfer id");
    if (enc !== "bin" && enc !== "b64") throw new Error("bad encoding");
    return `${MAGIC} ${transferId} ${index} ${total} ${payloadLen} ${payloadSha} ${enc}\n`;
  }

  function parseHeader(line) {
    const p = line.replace(/\n$/, "").split(" ");
    if (p.length !== 7 || p[0] !== MAGIC) throw new Error("not an RMSC1 chunk");
    return { transferId: p[1], index: +p[2], total: +p[3], payloadLen: +p[4], sha256: p[5], enc: p[6] };
  }

  function planChunks(size, enc) {
    const n = Math.max(1, Math.ceil(size / CHUNK_BYTES[enc]));
    return Array.from({ length: n }, (_, i) => ({
      index: i, start: i * CHUNK_BYTES[enc], end: Math.min(size, (i + 1) * CHUNK_BYTES[enc]) }));
  }

  /** Stable id for resuming the same file: first-MiB digest + size. */
  function transferId(firstMiBSha, size) {
    return `t${firstMiBSha.slice(0, 20)}-${size}`;
  }

  const api = { Sha256, MAGIC, CHUNK_BYTES, header, parseHeader, planChunks, transferId };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.TransferCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
