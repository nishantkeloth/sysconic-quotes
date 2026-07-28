// TEMPORARY diagnostic endpoint -- GET, no auth. Runs one step further each
// time based on ?step=N, returning JSON after that step instead of
// continuing further. Since each request runs the same cascade fresh,
// whichever step number stops returning valid JSON (and instead shows
// Vercel's crash page) is the actual failing operation -- this works even
// when the crash happens too fast/hard for console.log output to be visible
// anywhere. Steps: 1=basic info only, 2=import puppeteer-core, 3=import
// chromium, 4=resolve executablePath, 5=launch browser, 6=full PDF render.
// DELETE this file (and its vercel.json build/route entries) once
// api/pdf-render.js is confirmed working -- it's unauthenticated and can
// launch a browser, so it shouldn't stay live.
//
// Found via this bisection: both @sparticuz/chromium AND puppeteer-core are
// pure ESM packages on Vercel's actual runtime (Node 24) -- require()'ing
// either crashes the function during module init with ERR_REQUIRE_ESM,
// before any handler code or try/catch runs. Both must use dynamic import().

module.exports = async (req, res) => {
  const step = parseInt((req.query && req.query.step) || '1', 10) || 1;
  const info = {
    step,
    node: process.version,
    arch: process.arch,
    platform: process.platform,
    memory: process.memoryUsage(),
  };

  let browser;
  try {
    let puppeteer, chromium, execPath;

    if (step >= 2) {
      const puppeteerMod = await import('puppeteer-core');
      puppeteer = puppeteerMod.default;
      info.puppeteerLoaded = !!puppeteer;
    }
    if (step >= 3) {
      const chromiumMod = await import('@sparticuz/chromium');
      chromium = chromiumMod.default;
      info.chromiumLoaded = !!chromium;
      info.chromiumArgsCount = (chromium.args || []).length;
    }
    if (step >= 4) {
      execPath = await chromium.executablePath();
      info.execPath = execPath;
      const fs = require('fs');
      info.execExists = fs.existsSync(execPath);
    }
    if (step >= 5) {
      browser = await puppeteer.launch({
        args: chromium.args,
        executablePath: execPath,
      });
      info.browserLaunched = true;
    }
    if (step >= 6) {
      const page = await browser.newPage();
      await page.setContent('<h1>Debug OK</h1>', { waitUntil: 'load', timeout: 15000 });
      const pdfBuffer = await page.pdf({ format: 'A4' });
      info.pdfBytes = pdfBuffer.length;
    }

    info.memoryAfter = process.memoryUsage();
    res.setHeader('Content-Type', 'application/json');
    res.status(200).json({ ok: true, ...info });
  } catch (e) {
    res.setHeader('Content-Type', 'application/json');
    res.status(500).json({ ok: false, ...info, error: String((e && e.stack) || e) });
  } finally {
    if (browser) {
      try {
        await browser.close();
      } catch (_) {}
    }
  }
};
