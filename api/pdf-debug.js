// TEMPORARY diagnostic endpoint -- GET, no auth, verbose step-by-step logging,
// so the actual failure point/cause is visible directly in the browser
// response instead of hunting through Vercel's Logs UI. Mirrors the exact
// launch sequence used by api/pdf-render.js. DELETE this file (and its
// vercel.json build/route entries) once that route is confirmed working --
// it's unauthenticated and launches a browser, so it shouldn't stay live.

let chromiumPromise = null;
function getChromium() {
  if (!chromiumPromise) chromiumPromise = import('@sparticuz/chromium').then((m) => m.default);
  return chromiumPromise;
}
const puppeteer = require('puppeteer-core');

module.exports = async (req, res) => {
  const steps = [];
  const log = (msg) => {
    steps.push(msg);
    console.log('[pdf-debug]', msg);
  };

  log(`start: node=${process.version} arch=${process.arch} platform=${process.platform}`);
  log(`memory at start: ${JSON.stringify(process.memoryUsage())}`);

  let browser;
  try {
    log('importing @sparticuz/chromium...');
    const chromium = await getChromium();
    log(`chromium module loaded, args count=${(chromium.args || []).length}`);

    log('resolving executablePath...');
    const execPath = await chromium.executablePath();
    log(`executablePath resolved: ${execPath}`);

    const fs = require('fs');
    log(`executable exists on disk: ${fs.existsSync(execPath)}`);

    log('launching puppeteer...');
    browser = await puppeteer.launch({
      args: chromium.args,
      executablePath: execPath,
    });
    log('browser launched OK');

    const page = await browser.newPage();
    log('page created');
    await page.setContent('<h1>Debug OK</h1>', { waitUntil: 'load', timeout: 15000 });
    log('content set');
    const pdfBuffer = await page.pdf({ format: 'A4' });
    log(`pdf generated, bytes=${pdfBuffer.length}`);

    res.setHeader('Content-Type', 'application/json');
    res.status(200).json({ ok: true, steps, pdfBytes: pdfBuffer.length });
  } catch (e) {
    log(`ERROR: ${e && e.message}`);
    res.setHeader('Content-Type', 'application/json');
    res.status(500).json({ ok: false, steps, error: String((e && e.stack) || e) });
  } finally {
    if (browser) {
      try {
        await browser.close();
      } catch (_) {}
    }
  }
};
