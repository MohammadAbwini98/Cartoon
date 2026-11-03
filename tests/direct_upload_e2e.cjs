// node tests/direct_upload_e2e.cjs <upload link> <file> [--expect-error]
// Opens the presigned upload page in headless Chromium, uploads the file (by bytes, keeping its
// real name), and waits for the page's completion or error message. Prints the page's SHA-256.
const { chromium } = require("playwright");
const fs = require("fs"), path = require("path");
const [link, file, flag] = process.argv.slice(2);
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  const errors = [];
  p.on("pageerror", (e) => errors.push(e.message));
  await p.goto(link);
  await p.setInputFiles("#file", { name: path.basename(file), mimeType: "video/mp4", buffer: fs.readFileSync(file) });
  await p.click("#go");
  const want = flag === "--expect-error" ? /resume/ : /Upload complete/;
  await p.waitForFunction((src) => new RegExp(src).test(document.getElementById("msg").textContent), want.source, { timeout: 120000 });
  const out = { msg: await p.locator("#msg").textContent(), sha: (await p.locator("#sha").textContent()).trim(), errors };
  console.log(JSON.stringify(out));
  await b.close();
})().catch((e) => { console.error(e.message); process.exit(1); });
