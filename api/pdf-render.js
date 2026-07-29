// ══ Self-hosted pixel-exact PDF via headless Chromium ═══════════════════════
// Renders the *actual* HTML the browser shows (built client-side from the same
// printHTML() used for the on-screen print view) through a real Chromium
// instance running inside this Vercel function -- same idea as the old
// /api/pdf/render (PDFShift) route in api/pdf.py, but self-hosted so it's not
// gated by a third-party service's monthly credit quota. Kept as a separate
// file/route (not a replacement of api/pdf.py) so the PDFShift path stays
// available as a fallback if this one ever needs to be disabled.
//
// Why Node here and not Python: @sparticuz/chromium + puppeteer-core is the
// standard, well-supported way to run headless Chromium inside a Vercel
// serverless function. There's no equivalent turnkey Python package for this,
// so this one route runs on the Node runtime (@vercel/node) while every other
// route in this project stays on Python -- see vercel.json, which lists each
// function's runtime explicitly (legacy `builds` array convention used
// throughout this project; every new api/* file must be added there too).

// Both @sparticuz/chromium (149.0.0) AND puppeteer-core (25.4.0) ship as pure
// ESM packages -- require()'ing them appears to work in some local Node setups
// (Node 22/24's require(esm) interop can mask this) but fails hard on Vercel's
// actual function runtime with ERR_REQUIRE_ESM. A plain top-level `require()`
// of either one crashes the function during module init, before the handler
// or its try/catch even runs -- which is what caused every prior "instant,
// uncatchable crash with zero logged output" seen while debugging this.
// Both must be loaded via dynamic import() instead, cached after first call.
let chromiumPromise = null;
function getChromium() {
  if (!chromiumPromise) chromiumPromise = import('@sparticuz/chromium').then((m) => m.default);
  return chromiumPromise;
}
let puppeteerPromise = null;
function getPuppeteer() {
  if (!puppeteerPromise) puppeteerPromise = import('puppeteer-core').then((m) => m.default);
  return puppeteerPromise;
}
const jwt = require('jsonwebtoken'); // confirmed CJS-compatible, fine to require() directly

function verifyToken(req) {
  const auth = (req.headers && req.headers['authorization']) || '';
  if (!auth.startsWith('Bearer ')) return null;
  try {
    return jwt.verify(auth.slice(7), process.env.JWT_SECRET, { algorithms: ['HS256'] });
  } catch (e) {
    return null;
  }
}

function footerTemplate() {
  // Repeating page-number footer shown on every page (unlike the old PDFShift
  // route, Puppeteer's displayHeaderFooter can't be told to start on page 2
  // only -- see docs/note below. Showing it on page 1 too is harmless since
  // page 1 already carries the full letterhead.)
  return `<div style="width:100%;box-sizing:border-box;padding:0 10mm;font-family:Arial,Helvetica,sans-serif;font-size:8px;color:#7c8798;display:flex;justify-content:flex-end">
    <span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span>
  </div>`;
}

module.exports = async (req, res) => {
  if (req.method !== 'POST') {
    res.status(405).json({ error: 'Method not allowed' });
    return;
  }

  const claims = verifyToken(req);
  if (!claims) {
    res.status(401).json({ error: 'Unauthorized' });
    return;
  }

  const body = req.body || {};
  const html = body.html;
  if (!html) {
    res.status(400).json({ error: 'No HTML provided' });
    return;
  }
  let filename = (body.filename || 'Quotation.pdf').toString();
  filename = filename.replace(/[\r\n"\\]/g, '').slice(0, 150) || 'Quotation.pdf';
  if (!filename.toLowerCase().endsWith('.pdf')) filename += '.pdf';

  let browser;
  try {
    const [chromium, puppeteer] = await Promise.all([getChromium(), getPuppeteer()]);
    // Note: chromium.defaultViewport isn't a real static member on this
    // package version (verified empty/undefined in testing) -- omitted
    // rather than passing undefined, letting puppeteer-core use its own
    // default viewport instead.
    browser = await puppeteer.launch({
      args: chromium.args,
      executablePath: await chromium.executablePath(),
    });
    const page = await browser.newPage();
    await page.emulateMediaType('print'); // same @media print rules the browser applies
    await page.setContent(html, { waitUntil: 'networkidle0', timeout: 30000 }); // networkidle0 so remote product/logo images finish loading before the snapshot
    const pdfBuffer = await page.pdf({
      printBackground: true,
      preferCSSPageSize: true, // respects the app's own `@page{margin:12mm 10mm 15mm 10mm;size:A4}` rule instead of Puppeteer's 1in default
      displayHeaderFooter: true,
      headerTemplate: '<div></div>',
      footerTemplate: footerTemplate(),
    });

    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
    res.status(200).send(Buffer.from(pdfBuffer));
  } catch (e) {
    res.status(500).json({ error: 'PDF generation failed: ' + String((e && e.message) || e).slice(0, 200) });
  } finally {
    if (browser) {
      try { await browser.close(); } catch (_) {}
    }
  }
};
