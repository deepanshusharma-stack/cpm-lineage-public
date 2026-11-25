// Render paper.html to cpm-lineage-paper.pdf with Playwright (Chromium), offline.
//   node docs/paper/render_pdf.js [--shots <dir>]
const path = require('path');
const { chromium } = require('playwright');

(async () => {
  const here = __dirname;
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.route(/^https?:/, (route) => route.abort());   // the paper must render without network access
  await page.goto('file://' + path.join(here, 'paper.html'), { waitUntil: 'load' });
  await page.emulateMedia({ media: 'print' });
  await page.pdf({
    path: path.join(here, 'cpm-lineage-paper.pdf'),
    format: 'A4',
    printBackground: true,
    preferCSSPageSize: true,
    margin: { top: '19mm', bottom: '20mm', left: '19mm', right: '19mm' },
    displayHeaderFooter: true,
    headerTemplate: '<span></span>',
    footerTemplate: '<div style="width:100%;text-align:center;font-family:Liberation Serif,serif;font-size:8pt;color:#52514e;">' +
                    '<span class="pageNumber"></span></div>',
  });
  await browser.close();
  console.log('wrote', path.join(here, 'cpm-lineage-paper.pdf'));
})().catch((e) => { console.error(e); process.exit(1); });
