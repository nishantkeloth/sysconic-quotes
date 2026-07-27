// Sysconic Quote Manager — automated frontend test suite
// Boots the real index.html in jsdom with mocked APIs and verifies core behavior.
// Run:  npm i jsdom --no-save && node tests/run-tests.mjs
import { readFileSync } from 'fs';
import { JSDOM } from 'jsdom';
import path from 'path';

const INDEX = path.resolve(process.cwd(), 'index.html');
const html = readFileSync(INDEX, 'utf-8');

let passed = 0, failed = 0;
const results = [];
function check(name, cond, extra = '') {
  if (cond) { passed++; results.push(`  PASS  ${name}`); }
  else { failed++; results.push(`  FAIL  ${name}${extra ? ' — ' + extra : ''}`); }
}

function boot({ fetchMock } = {}) {
  const captured = { requests: [] };
  const dom = new JSDOM(html, {
    runScripts: 'dangerously', url: 'https://test.local/',
    beforeParse(w) {
      w.fetch = async (url, opts = {}) => {
        captured.requests.push({ url: String(url), method: opts.method || 'GET', body: opts.body });
        // api() reads r.text() (not r.json()) since the AUTH-002 rework, so the
        // mock must provide status/text too or every mocked call errors out.
        const mk = (obj) => ({ ok: true, status: 200, json: async () => obj, text: async () => JSON.stringify(obj) });
        if (fetchMock) { const r = fetchMock(String(url), opts); if (r) return mk(r); }
        return mk({ quote: { id: 'q1' }, quotes: [], customers: [] });
      };
      w.alert = () => {}; w.confirm = () => true; w.prompt = () => 'x';
    },
  });
  const w = dom.window;
  const run = (code) => w.eval(code);
  return { w, run, captured };
}

const wait = (ms) => new Promise(r => setTimeout(r, ms));

async function freshEditor(opts = {}) {
  const ctx = boot(opts);
  await wait(250);
  ctx.run(`
    A.screen='app'; A.user={id:'u1',name:'T',role:'admin',company_id:'c1'};
    A.activeQuote={id:'q1',title:'Test Quote',customer:'ACME',status:'draft',
      quote_data:[mkOption('Option 1')],terms_data:[...DEFAULT_TERMS],vendor_data:[]};
    A.quotes=[A.activeQuote]; A.page='editor'; A.activeOpt=0; A.view='internal'; draw();
  `);
  await wait(80); // let initTA run
  return ctx;
}

// ── Test 1: pricing formula ────────────────────────────────────────────────
async function testPricing() {
  const { run } = await freshEditor();
  run(`
    const it = sec(0).items[0];
    it.cost = 1000; it.disc = 10; it.margin = 0.2; it.qty = 2;
  `);
  // Default pricing_type is 'markup': cost after disc = 900; up = round(900 × 1.2) = 1080
  const up = run(`calcItem(sec(0).items[0]).up`);
  const tp = run(`calcItem(sec(0).items[0]).tp`);
  check('pricing (markup): unit price = ROUND(cost·(1−disc)·(1+markup))', up === 1080, `got ${up}`);
  check('pricing (markup): line total = unit price × qty', tp === 2160, `got ${tp}`);
  // Margin mode: up = round(900 / (1 − 0.2)) = 1125
  run(`A.activeQuote.pricing_type='margin';`);
  const upM = run(`calcItem(sec(0).items[0]).up`);
  const tpM = run(`calcItem(sec(0).items[0]).tp`);
  check('pricing (margin): unit price = ROUND(cost·(1−disc)/(1−margin))', upM === 1125, `got ${upM}`);
  check('pricing (margin): line total = unit price × qty', tpM === 2250, `got ${tpM}`);
}

// ── Test 1b: V.Disc% — internal vendor discount boosts GP, not the price ───
async function testVendorDisc() {
  const { run } = await freshEditor();
  run(`
    A.activeQuote.pricing_type='margin';
    const it = sec(0).items[0];
    it.cost = 13700; it.disc = 0; it.margin = 0.2; it.qty = 1;
  `);
  const before = run(`calcItem(sec(0).items[0])`);
  run(`sec(0).items[0].vdisc = 5;`);
  const after = run(`calcItem(sec(0).items[0])`);
  // Sell price must NOT move: 13700 / 0.8 = 17125
  check('vdisc: sell price unchanged by vendor discount', before.up === 17125 && after.up === 17125, `got ${before.up} → ${after.up}`);
  // Cost drops 5%: 13700 × 0.95 = 13015 ; GP rises from 3425 to 4110
  check('vdisc: cost total uses discounted vendor cost', after.tc === 13015, `got ${after.tc}`);
  check('vdisc: GP captures the vendor discount', after.tgp === 17125 - 13015, `got ${after.tgp}`);
  // Option margin improves: 4110 / 17125 ≈ 24%
  const m = run(`calcOpt(opt()).margin`);
  check('vdisc: effective margin rises above quoted margin', m > 0.239 && m < 0.241, `got ${m}`);
  // Editor renders the internal-only input; client view must not have it
  run(`draw()`);
  check('vdisc: input present in internal view', run(`!!document.querySelector('input[data-field="vdisc"]')`) === true);
  run(`A.view='client'; draw()`);
  check('vdisc: input absent from client view', run(`!!document.querySelector('input[data-field="vdisc"]')`) === false);
  run(`A.view='internal'; draw()`);
}

// ── Test 2: REGRESSION — typed text persists (the initTA bug) ─────────────
async function testTypingPersists() {
  const { run } = await freshEditor();
  run(`
    const tas = document.querySelectorAll('textarea.iinput');
    // Column order: Vendor, Model, Brand, Description
    const vals = ['VendorV','ModelY','BrandX','DescZ'];
    [0,1,2,3].forEach(i => { tas[i].value = vals[i]; tas[i].dispatchEvent(new Event('input',{bubbles:true})); });
  `);
  const vendor = run(`sec(0).items[0].vendor`);
  const brand = run(`sec(0).items[0].brand`);
  const model = run(`sec(0).items[0].model`);
  const desc  = run(`sec(0).items[0].desc`);
  check('typing: vendor reaches data model', vendor === 'VendorV', `got "${vendor}"`);
  check('typing: brand reaches data model', brand === 'BrandX', `got "${brand}"`);
  check('typing: model reaches data model', model === 'ModelY', `got "${model}"`);
  check('typing: description reaches data model', desc === 'DescZ', `got "${desc}"`);
  run(`addItem(0)`);
  const after = run(`sec(0).items[0].brand`);
  const domVal = run(`document.querySelectorAll('textarea.iinput')[2].value || document.querySelectorAll('textarea.iinput')[2].textContent`);
  check('typing: survives Add item redraw (regression)', after === 'BrandX' && domVal === 'BrandX');
  const vendAfter = run(`sec(0).items[0].vendor`);
  check('typing: vendor survives redraw and persists on item', vendAfter === 'VendorV', `got "${vendAfter}"`);
}

// ── Test 3: items / sections / options structure ──────────────────────────
async function testStructure() {
  const { run } = await freshEditor();
  run(`addItem(0); addItem(0)`);
  check('structure: add item grows list', run(`sec(0).items.length`) === 3);
  run(`delItem(0,2)`);
  check('structure: delete item shrinks list', run(`sec(0).items.length`) === 2);
  run(`addOpt()`);
  check('structure: add option', run(`opts().length`) === 2 && run(`A.activeOpt`) === 1);
  check('structure: new option has default section+item', run(`opt().sections[0].items.length`) === 1);
}

// ── Test 4: VAT math ───────────────────────────────────────────────────────
async function testVat() {
  const { run } = await freshEditor();
  // Margin mode so the subtotal is a round 1000 (800 / 0.8); VAT 5% on top.
  run(`A.activeQuote.pricing_type='margin';`);
  run(`const it=sec(0).items[0]; it.cost=800; it.disc=0; it.margin=0.2; it.qty=1;`);
  run(`opt().vatEnabled=true; opt().vatRate=5;`);
  const grand = run(`calcOpt(opt()).grand`);
  check('vat: grand total = subtotal × 1.05', grand === 1050, `got ${grand}`); // up=1000, vat 50
  run(`opt().vatEnabled=false;`);
  const grand2 = run(`calcOpt(opt()).grand`);
  check('vat: toggle off removes VAT', grand2 === 1000, `got ${grand2}`);
}

// ── Test 5: save payload carries the data ──────────────────────────────────
async function testSavePayload() {
  const ctx = await freshEditor();
  const { run, captured } = ctx;
  run(`
    const ta = document.querySelector('textarea.iinput');
    ta.value='SaveMe'; ta.dispatchEvent(new Event('input',{bubbles:true}));
  `);
  await run(`saveQuote()`);
  await wait(50);
  const put = captured.requests.find(r => r.method === 'PUT' && r.url.includes('/api/quotes/'));
  check('save: PUT request fired', !!put);
  const body = put ? JSON.parse(put.body) : {};
  check('save: payload contains typed brand', JSON.stringify(body.quote_data || '').includes('SaveMe'));
  check('save: payload contains totals', typeof body.total_sell === 'number' && typeof body.margin === 'number');
}

// ── Test 6: multi-option print layout ──────────────────────────────────────
async function testMultiOptionPrint() {
  const { run } = await freshEditor();
  run(`
    A.company={name:'Sysconic Technologies'};
    sec(0).items[0].brand='B1'; sec(0).items[0].cost=100; sec(0).items[0].qty=1;
    addOpt();
    opt().sections[0].items[0].brand='B2'; opt().sections[0].items[0].cost=200;
    A.page='print'; draw();
  `);
  await wait(60);
  const htmlOut = run(`document.getElementById('app').innerHTML`);
  // Option banner pill markup ends "...page-break-after:avoid;margin-bottom:10px">Option N";
  // totals/terms labels are sentence case since the print restyle.
  const banners = (htmlOut.match(/margin-bottom:10px">Option \d/g) || []).length;
  const grandTotals = (htmlOut.match(/>Grand total</g) || []).length;
  const headers = (htmlOut.match(/SYSCONIC TECHNOLOGIES/g) || []).length;
  const terms = (htmlOut.match(/>Terms &amp; conditions</g) || []).length;
  check('print: defaults to All options (2 option banners)', banners === 2, `got ${banners}`);
  check('print: per-option totals (2× Grand Total)', grandTotals === 2, `got ${grandTotals}`);
  check('print: company header appears once', headers === 1, `got ${headers}`);
  check('print: terms appear once', terms === 1, `got ${terms}`);
  check('print: both options items present', htmlOut.includes('B1') && htmlOut.includes('B2'));
}

// ── Test 7: single-option print unchanged ──────────────────────────────────
async function testSingleOptionPrint() {
  const { run } = await freshEditor();
  run(`A.company={name:'Sysconic Technologies'}; sec(0).items[0].brand='Solo'; sec(0).items[0].cost=100; A.page='print'; draw();`);
  await wait(60);
  const htmlOut = run(`document.getElementById('app').innerHTML`);
  const headers = (htmlOut.match(/SYSCONIC TECHNOLOGIES/g) || []).length;
  const banners = (htmlOut.match(/margin-bottom:10px">Option \d/g) || []).length;
  check('print single: header present, no option banner', headers === 1 && banners === 0, `h=${headers} b=${banners}`);
}

// ── Test 8: customer autocomplete (customer master, fed by integration sync) ──
async function testZoho() {
  const ctx = await freshEditor({
    fetchMock: (url) => url.includes('/api/customers?search=')
      ? { customers: [{ id: '1', name: 'Golden Synapse Technologies LLC', company_name: '', email: 'a@b.com', phone: '' }] }
      : null,
  });
  const { run } = ctx;
  run(`zohoSearch('gol')`);
  await wait(500); // debounce 320ms + fetch
  const drop = run(`document.getElementById('custDrop') ? document.getElementById('custDrop').innerHTML : ''`);
  check('autocomplete: dropdown renders matched customer', drop.includes('Golden Synapse'));
  check('autocomplete: dropdown labeled with source', drop.includes('Your customer master'));
}

// ── Test 9: PDF download button ────────────────────────────────────────────
async function testPdfButton() {
  const { run } = await freshEditor();
  run(`A.page='print'; draw();`);
  await wait(60);
  const hasBtn = run(`[...document.querySelectorAll('button')].some(b=>b.textContent.includes('PDF'))`);
  const hasFn = run(`typeof downloadPDF === 'function'`);
  check('pdf: download button on print page', hasBtn === true);
  check('pdf: downloadPDF function defined', hasFn === true);
}

// ── Test 10: AI System Diagram ─────────────────────────────────────────────
const DIAG_TOKEN_JS = `A.token='x.'+btoa(JSON.stringify({features:{diagrams:true}}))+'.y';`;

async function testDiagramFlag() {
  const { run } = await freshEditor();
  run(`draw()`);
  // Check rendered buttons only — body.innerHTML would also match the string
  // inside the app's own <script> source.
  const btnCheck = `[...document.querySelectorAll('button')].some(b=>b.textContent.includes('AI Diagram'))`;
  const without = run(btnCheck);
  run(DIAG_TOKEN_JS + `draw()`);
  const withFlag = run(btnCheck);
  check('diagram: toolbar button hidden without feature flag', without === false);
  check('diagram: toolbar button shown with feature flag', withFlag === true);
}

async function testDiagramGenerate() {
  const ctx = await freshEditor({
    fetchMock: (url) => url.includes('/api/diagram/generate')
      ? { mermaid: 'flowchart LR\n    A["PC"] -- HDMI --> B["Display"]' }
      : null,
  });
  const { run, captured } = ctx;
  run(DIAG_TOKEN_JS + `
    sec(0).items[0].brand='LG'; sec(0).items[0].model='98UM5K'; sec(0).items[0].desc='98in display'; sec(0).items[0].qty=1;
    sec(0).items[0].img='https://example.supabase.co/product-images/98um5k.png';
    openDiagramModal();
    generateDiagram();
  `);
  await wait(120);
  const code = run(`DIAG.code`);
  check('diagram: generate stores mermaid code', typeof code === 'string' && code.startsWith('flowchart'), `got "${String(code).slice(0, 40)}"`);
  const req = captured.requests.find(r => r.url.includes('/api/diagram/generate'));
  const body = req ? JSON.parse(req.body) : null;
  check('diagram: request carries the BOM items', !!body && body.quote.sections[0].items[0].model === '98UM5K');
  check('diagram: request carries product photo URL', !!body && body.quote.sections[0].items[0].img === 'https://example.supabase.co/product-images/98um5k.png');
  run(`DIAG.photos=false;`);
  const noPhoto = run(`diagBomPayload().sections[0].items[0].img`);
  check('diagram: photos toggle off strips image URLs', noPhoto === undefined);
  run(`DIAG.photos=true;`);
  run(`saveDiagramToQuote()`);
  const saved = run(`opts()[0].diagram && opts()[0].diagram.code`);
  const stamped = run(`!!(opts()[0].diagram && opts()[0].diagram.updated_at)`);
  check('diagram: save persists code onto the option (rides in quote_data)', typeof saved === 'string' && saved.startsWith('flowchart'));
  check('diagram: save stamps updated_at', stamped === true);

  // Quick wins: direction toggle + Mermaid Live link
  const modalHtml = run(`document.getElementById('diagModalBox').innerHTML`);
  check('diagram: direction toggle button present', modalHtml.includes('Vertical') || modalHtml.includes('Horizontal'));
  check('diagram: Mermaid Live button present', modalHtml.includes('Mermaid Live'));
  run(`diagToggleDirection()`);
  check('diagram: direction toggle flips LR to TB', run(`DIAG.code`).startsWith('flowchart TB'));
  run(`diagToggleDirection()`);
  check('diagram: direction toggle flips back to LR', run(`DIAG.code`).startsWith('flowchart LR'));
  const liveUrl = run(`diagLiveEditorUrl(DIAG.code)`);
  const decoded = Buffer.from(String(liveUrl).split('#base64:')[1], 'base64').toString('utf-8');
  check('diagram: Mermaid Live URL carries the code', String(liveUrl).startsWith('https://mermaid.live/edit#base64:') && JSON.parse(decoded).code.startsWith('flowchart'));
  run(`closeDiagramModal()`);
}

async function testDiagramAutoFill() {
  const ctx = await freshEditor({
    fetchMock: (url) => {
      if (url.includes('/api/products/image-search')) return { images: [{ url: 'https://vendor.com/pic.jpg', thumb: '' }] };
      if (url.includes('/api/products/p1/image')) return { product: { image_url: 'https://cdn.supabase.co/product-images/p1.png' } };
      return null;
    },
  });
  const { run } = ctx;
  run(DIAG_TOKEN_JS + `
    sec(0).items[0].brand='LG'; sec(0).items[0].model='98UM5K'; sec(0).items[0].product_id='p1';
    openDiagramModal();
  `);
  const offer = run(`document.getElementById('diagModalBox').innerHTML`);
  check('autofill: button offered with missing-image count', offer.includes('Auto-fill images (1)'));
  run(`diagAutoFillImages()`);
  await wait(200);
  const img = run(`sec(0).items[0].img`);
  check('autofill: top search result saved onto the item', img === 'https://cdn.supabase.co/product-images/p1.png', `got "${img}"`);
  const after = run(`document.getElementById('diagModalBox').innerHTML`);
  check('autofill: button disappears once nothing is missing', !after.includes('Auto-fill images'));
  run(`closeDiagramModal()`);
}

async function testDiagramProposalSection() {
  const { run } = await freshEditor();
  run(DIAG_TOKEN_JS + `PROP.content={title:'T'};`);
  const withFlag = run(`proposalReviewHTML().includes('System Schematic')`);
  const hint = run(`proposalReviewHTML().includes('no diagram is saved')`);
  run(`opts()[0].diagram={code:'flowchart LR\\n A-->B',updated_at:'2026-07-26T00:00:00Z'};`);
  const hintGone = run(`proposalReviewHTML().includes('no diagram is saved')`);
  run(`A.token=null;`);
  const withoutFlag = run(`proposalReviewHTML().includes('System Schematic')`);
  check('diagram: proposal offers System Schematic toggle with flag', withFlag === true);
  check('diagram: proposal hints when no diagram saved yet', hint === true);
  check('diagram: hint disappears once a diagram is saved', hintGone === false);
  check('diagram: proposal hides schematic toggle without flag', withoutFlag === false);
}

// ── Runner ──────────────────────────────────────────────────────────────────
const suites = [
  ['Pricing formula', testPricing],
  ['Vendor discount (internal)', testVendorDisc],
  ['Typing persistence (regression)', testTypingPersists],
  ['Items / sections / options', testStructure],
  ['VAT math', testVat],
  ['Save payload', testSavePayload],
  ['Multi-option print', testMultiOptionPrint],
  ['Single-option print', testSingleOptionPrint],
  ['Customer autocomplete', testZoho],
  ['PDF button', testPdfButton],
  ['AI Diagram feature flag', testDiagramFlag],
  ['AI Diagram generate & save', testDiagramGenerate],
  ['AI Diagram image auto-fill', testDiagramAutoFill],
  ['AI Diagram proposal section', testDiagramProposalSection],
];

console.log(`\nSysconic Quote Manager — automated tests\nTarget: ${INDEX}\n`);
for (const [name, fn] of suites) {
  const before = results.length;
  try { await fn(); } catch (e) { failed++; results.push(`  FAIL  ${name} crashed — ${e.message}`); }
  console.log(name + ':');
  console.log(results.slice(before).join('\n'));
}
console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
