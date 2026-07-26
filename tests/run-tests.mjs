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
  const up = run(`calcItem(sec(0).items[0]).up`);
  const tp = run(`calcItem(sec(0).items[0]).tp`);
  // cost after disc = 900; unit price = round(900 / 0.8) = 1125; total = 2250
  check('pricing: unit price = ROUND(cost·(1−disc)/(1−margin))', up === 1125, `got ${up}`);
  check('pricing: line total = unit price × qty', tp === 2250, `got ${tp}`);
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
    sec(0).items[0].brand='B1'; sec(0).items[0].cost=100; sec(0).items[0].qty=1;
    addOpt();
    opt().sections[0].items[0].brand='B2'; opt().sections[0].items[0].cost=200;
    A.page='print'; draw();
  `);
  await wait(60);
  const htmlOut = run(`document.getElementById('app').innerHTML`);
  const banners = (htmlOut.match(/page-break-after:avoid">Option \d/g) || []).length;
  const grandTotals = (htmlOut.match(/Grand Total/g) || []).length;
  const headers = (htmlOut.match(/SYSCONIC TECHNOLOGIES/g) || []).length;
  const terms = (htmlOut.match(/Terms &amp; Conditions/g) || []).length;
  check('print: defaults to All options (2 option banners)', banners === 2, `got ${banners}`);
  check('print: per-option totals (2× Grand Total)', grandTotals === 2, `got ${grandTotals}`);
  check('print: company header appears once', headers === 1, `got ${headers}`);
  check('print: terms appear once', terms === 1, `got ${terms}`);
  check('print: both options items present', htmlOut.includes('B1') && htmlOut.includes('B2'));
}

// ── Test 7: single-option print unchanged ──────────────────────────────────
async function testSingleOptionPrint() {
  const { run } = await freshEditor();
  run(`sec(0).items[0].brand='Solo'; sec(0).items[0].cost=100; A.page='print'; draw();`);
  await wait(60);
  const htmlOut = run(`document.getElementById('app').innerHTML`);
  const headers = (htmlOut.match(/SYSCONIC TECHNOLOGIES/g) || []).length;
  const banners = (htmlOut.match(/page-break-after:avoid">Option \d/g) || []).length;
  check('print single: header present, no option banner', headers === 1 && banners === 0, `h=${headers} b=${banners}`);
}

// ── Test 8: Zoho customer autocomplete ─────────────────────────────────────
async function testZoho() {
  const ctx = await freshEditor({
    fetchMock: (url) => url.includes('/api/zoho/customers')
      ? { customers: [{ id: '1', name: 'Golden Synapse Technologies LLC', company: '', email: 'a@b.com', phone: '' }] }
      : null,
  });
  const { run } = ctx;
  run(`zohoSearch('gol')`);
  await wait(500); // debounce 320ms + fetch
  const drop = run(`document.getElementById('custDrop') ? document.getElementById('custDrop').innerHTML : ''`);
  check('zoho: dropdown renders matched customer', drop.includes('Golden Synapse'));
  check('zoho: dropdown labeled as Zoho source', drop.includes('From Zoho Books'));
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
    openDiagramModal();
    generateDiagram();
  `);
  await wait(120);
  const code = run(`DIAG.code`);
  check('diagram: generate stores mermaid code', typeof code === 'string' && code.startsWith('flowchart'), `got "${String(code).slice(0, 40)}"`);
  const req = captured.requests.find(r => r.url.includes('/api/diagram/generate'));
  const body = req ? JSON.parse(req.body) : null;
  check('diagram: request carries the BOM items', !!body && body.quote.sections[0].items[0].model === '98UM5K');
  run(`saveDiagramToQuote()`);
  const saved = run(`opts()[0].diagram && opts()[0].diagram.code`);
  const stamped = run(`!!(opts()[0].diagram && opts()[0].diagram.updated_at)`);
  check('diagram: save persists code onto the option (rides in quote_data)', typeof saved === 'string' && saved.startsWith('flowchart'));
  check('diagram: save stamps updated_at', stamped === true);
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
  ['Typing persistence (regression)', testTypingPersists],
  ['Items / sections / options', testStructure],
  ['VAT math', testVat],
  ['Save payload', testSavePayload],
  ['Multi-option print', testMultiOptionPrint],
  ['Single-option print', testSingleOptionPrint],
  ['Zoho autocomplete', testZoho],
  ['PDF button', testPdfButton],
  ['AI Diagram feature flag', testDiagramFlag],
  ['AI Diagram generate & save', testDiagramGenerate],
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
