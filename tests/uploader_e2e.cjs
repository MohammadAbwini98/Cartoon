// End-to-end test of uploader/index.html in headless Chromium with a mock of
// the claude.ai `assets` and `db` capabilities. The pieces the page uploads are
// written to disk and rebuilt by remake_studio/ingest/reassemble.py.
//   node tests/uploader_e2e.cjs            (exit 0 = all scenarios pass)
const { chromium } = require("playwright");
const fs = require("fs"), path = require("path"), os = require("os"), crypto = require("crypto");
const { execFileSync } = require("child_process");

const ROOT = path.resolve(__dirname, "..");
const work = fs.mkdtempSync(path.join(os.tmpdir(), "uploader-e2e-"));
const MiB = 1048576;

// Page = the published fragment wrapped in the skeleton claude.ai adds.
const page_html = path.join(work, "index.html");
fs.writeFileSync(page_html, `<!doctype html><html><head><meta charset="utf-8"></head><body>${fs.readFileSync(path.join(ROOT, "uploader/index.html"), "utf8")}</body></html>`);
fs.copyFileSync(path.join(ROOT, "uploader/transfer-core.js"), path.join(work, "transfer-core.js"));

const mock = (cfg) => {
  // runs in the page before its scripts
  const st = window.__mock = { uploads: [], docs: {}, listeners: [], cfg, calls: 0 };
  const err = (code) => Object.assign(new Error(code), { code });
  const hex = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
  const assets = {
    async upload(blob, opt) {
      st.calls++;
      const type = (opt && opt.type) || blob.type;
      const f = st.cfg.failOn && st.cfg.failOn[st.calls];
      if (f) throw err(f);
      if (st.cfg.refuseTypes && st.cfg.refuseTypes.includes(type)) throw err("unsupported_type");
      if (blob.size > 20 * 1048576) throw err("too_large");
      const bytes = new Uint8Array(await blob.arrayBuffer());
      const id = hex();
      st.uploads.push({ id, type, bytes });
      return { id, url: "/_blob/" + id, sizeBytes: bytes.length, contentType: type };
    },
    async list() { const b = st.uploads.reduce((s, u) => s + u.bytes.length, 0); return { assets: [], usage: { files: st.uploads.length, bytes: b, maxFiles: 1000, maxBytes: 2 * 1073741824 } }; },
  };
  const snap = (p) => ({ id: p.split("/").pop(), exists: p in st.docs, data: () => st.docs[p] && structuredClone(st.docs[p]), metadata: {} });
  const notify = () => st.listeners.forEach(([c, cb]) => cb(qs(c)));
  const qs = (c) => { const docs = Object.keys(st.docs).filter((k) => k.startsWith(c + "/") && k.split("/").length === c.split("/").length + 1).map(snap); return { docs, size: docs.length, empty: !docs.length, metadata: {} }; };
  const db = {
    doc: (p) => ({ get: async () => snap(p), set: async (d) => { st.docs[p] = structuredClone(d); notify(); },
                   update: async (d) => { if (!(p in st.docs)) throw err("invalid_argument"); Object.assign(st.docs[p], structuredClone(d)); notify(); } }),
    collection: (c) => ({ get: async () => qs(c), onSnapshot: (cb) => { st.listeners.push([c, cb]); cb(qs(c)); return () => {}; } }),
  };
  window.claude = { use: async (n) => (n === "assets" ? assets : n === "db" ? db : null) };
};

async function open(browser, cfg, carryDocs) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.error("page error:", e.message));
  if (process.env.DEBUG) page.on("console", (m) => console.error("console:", m.text()));
  await page.addInitScript(mock, cfg);
  if (carryDocs) await page.addInitScript((d) => { window.__carry = d; }, carryDocs);
  await page.goto("file://" + page_html);
  if (carryDocs) await page.evaluate(() => Object.assign(window.__mock.docs, window.__carry));
  return { ctx, page };
}

async function dumpPieces(page, dir) {
  fs.mkdirSync(dir, { recursive: true });
  const ups = await page.evaluate(() => window.__mock.uploads.map((u) => ({ id: u.id, type: u.type, b64: btoa(Array.from(u.bytes, (x) => String.fromCharCode(x)).join("")) })));
  for (const u of ups) fs.writeFileSync(path.join(dir, u.id), Buffer.from(u.b64, "base64"));
  return ups;
}

function rebuild(dir, out, sha) {
  return JSON.parse(execFileSync("python3", ["-m", "remake_studio.ingest.reassemble", dir, out, "--expect-sha256", sha], { cwd: ROOT }).toString());
}

// Playwright's path-based setInputFiles silently drops non-ASCII paths in this
// environment, so the file goes in by bytes, keeping its real Arabic name.
const pick = (page, f) => page.setInputFiles("#file", { name: path.basename(f), mimeType: "video/mp4", buffer: fs.readFileSync(f) });
let passed = 0, lastPage = null, stepName = "";
const step = (n, pg) => { stepName = n; lastPage = pg; };
const ok = (s) => console.log(`ok ${++passed} - ${s}`);

(async () => {
  const src = path.join(work, "دروب ريمي الحلقة 1 كاملة.mp4");
  fs.writeFileSync(src, crypto.randomBytes(40 * MiB + 4321));
  const sha = crypto.createHash("sha256").update(fs.readFileSync(src)).digest("hex");
  const browser = await chromium.launch();

  // 1. full upload, then rebuild in Python
  {
    const { ctx, page } = await open(browser, {});
    step("1: choose", page);
    await pick(page, src);
    await page.waitForFunction(() => !document.getElementById("start").disabled);
    if ((await page.locator("#fChunks").textContent()).trim() !== "3 × 16.0 MB") throw new Error("plan");
    step("1: upload", page);
    await page.click("#start");
    await page.waitForFunction(() => /Upload complete/.test(document.getElementById("msg").textContent), null, { timeout: 120000 });
    const shown = (await page.locator("#shaOut").textContent()).trim();
    if (shown !== sha) throw new Error(`page sha ${shown} != ${sha}`);
    const ups = await dumpPieces(page, path.join(work, "p1"));
    if (ups.length !== 3 || ups.some((u) => u.type !== "video/mp4")) throw new Error("expected 3 binary pieces");
    const r = rebuild(path.join(work, "p1"), path.join(work, "out1.mp4"), sha);
    if (r.sha256 !== sha) throw new Error("rebuild mismatch");
    const docs = await page.evaluate(() => window.__mock.docs);
    const t = Object.entries(docs).find(([k]) => /^transfers\/[^/]+$/.test(k))[1];
    if (t.status !== "complete" || t.sha256 !== sha || t.done !== 3 || t.role !== "episode") throw new Error("transfer record " + JSON.stringify(t));
    const rows = await page.locator("#listWrap tbody tr").count();
    if (rows !== 1) throw new Error("transfer list rows " + rows);
    ok("3-piece upload; page hash = node hash; Python rebuild verified; transfer record complete");
    await ctx.close();
  }

  // 2. failure on piece 2, then resume by choosing the file again
  {
    const { ctx, page } = await open(browser, { failOn: { 2: "quota_or_state" } });
    step("2: fail", page);
    await pick(page, src);
    await page.waitForFunction(() => !document.getElementById("start").disabled);
    await page.click("#start");
    await page.waitForFunction(() => /storage is full/.test(document.getElementById("msg").textContent), null, { timeout: 60000 });
    const after1 = await page.evaluate(() => window.__mock.uploads.length);
    if (after1 !== 1) throw new Error("expected 1 stored piece before failure, got " + after1);
    await page.evaluate(() => { window.__mock.cfg.failOn = {}; });
    step("2: rechoose", page);
    await page.setInputFiles("#file", []);
    await pick(page, src);
    await page.waitForFunction(() => /1 of 3 pieces/.test(document.getElementById("fResume").textContent));
    if ((await page.locator("#start").textContent()).trim() !== "Resume upload") throw new Error("resume label");
    await page.click("#start");
    await page.waitForFunction(() => /Upload complete/.test(document.getElementById("msg").textContent), null, { timeout: 120000 });
    const ups = await dumpPieces(page, path.join(work, "p2"));
    if (ups.length !== 3) throw new Error("resume re-uploaded a finished piece: " + ups.length + " uploads");
    if ((await page.locator("#c0").getAttribute("class")) !== "skip") throw new Error("piece 1 should show as checked/skipped");
    if ((await page.locator("#shaOut").textContent()).trim() !== sha) throw new Error("hash after resume");
    rebuild(path.join(work, "p2"), path.join(work, "out2.mp4"), sha);
    ok("failure on piece 2 reported; resume skipped the stored piece, uploaded 2, rebuild verified");
    await ctx.close();
  }

  // 3. store refuses binary pieces -> automatic switch to text-encoded pieces
  {
    const { ctx, page } = await open(browser, { refuseTypes: ["video/mp4"] });
    step("3: fallback", page);
    await pick(page, src);
    await page.waitForFunction(() => !document.getElementById("start").disabled);
    await page.click("#start");
    await page.waitForFunction(() => /Upload complete/.test(document.getElementById("msg").textContent), null, { timeout: 120000 });
    const ups = await dumpPieces(page, path.join(work, "p3"));
    if (ups.length !== 3 || ups.some((u) => u.type !== "text/plain")) throw new Error("expected 3 text pieces, got " + ups.map((u) => u.type));
    if (ups.some((u) => Buffer.from(u.b64, "base64").length > 20 * MiB)) throw new Error("piece over 20 MiB");
    rebuild(path.join(work, "p3"), path.join(work, "out3.mp4"), sha);
    const d = await page.evaluate(() => Object.entries(window.__mock.docs).find(([k]) => /^transfers\/[^/]+$/.test(k))[1]);
    if (d.mode !== "b64") throw new Error("mode not recorded");
    ok("binary refused -> switched to base64 pieces (all < 20 MiB); rebuild verified");
    await ctx.close();
  }

  // 4. outside claude.ai: no window.claude -> clear message, upload disabled
  {
    const ctx = await browser.newContext(); const page = await ctx.newPage();
    await page.goto("file://" + page_html);
    await page.waitForFunction(() => /Open this page on claude.ai/.test(document.getElementById("caps").textContent));
    ok("without claude.ai capabilities the page explains and stays inert");
    await ctx.close();
  }

  await browser.close();
  fs.rmSync(work, { recursive: true, force: true });
  console.log(`all ${passed} scenarios passed`);
})().catch(async (e) => {
  console.error(`FAIL at step ${stepName}:`, e.message);
  if (lastPage) { try { console.error(await lastPage.evaluate(() => ({ msg: document.getElementById("msg").textContent, resume: document.getElementById("fResume").textContent, log: document.getElementById("log").textContent.slice(-1500) }))); } catch {} }
  process.exit(1);
});
