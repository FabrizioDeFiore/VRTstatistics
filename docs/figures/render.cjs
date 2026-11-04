// Render every figure with one palette, font and canvas. Uses a locally installed Chrome.
const fs = require('node:fs');
const path = require('node:path');

async function main() {
  const puppeteer = await import(require('node:url').pathToFileURL(require.resolve('puppeteer')).href);
  const browser = await puppeteer.launch({
    executablePath: process.env.PUPPETEER_EXECUTABLE_PATH,
    headless: true,
  });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 960, height: 900, deviceScaleFactor: 2 });
    await page.setContent('<html><body style="margin:0;background:white"></body></html>');
    await page.addScriptTag({ path: path.join(path.dirname(require.resolve('mermaid')), 'mermaid.min.js') });
    await page.evaluate(() => mermaid.initialize({
      startOnLoad: false, theme: 'base', securityLevel: 'strict',
      themeVariables: {
        fontFamily: 'Arial, sans-serif', fontSize: '18px', primaryTextColor: '#172033',
        lineColor: '#64748b', clusterBkg: '#ffffff', clusterBorder: '#cbd5e1',
        edgeLabelBackground: '#ffffff',
      },
      flowchart: { htmlLabels: false, curve: 'linear', nodeSpacing: 24, rankSpacing: 36, padding: 14, useMaxWidth: false, wrappingWidth: 340 },
    }));
    const palette = fs.readFileSync(path.join(__dirname, 'palette.mmd'), 'utf8');
    for (const file of fs.readdirSync(path.join(__dirname, 'src')).filter(f => f.endsWith('.mmd')).sort()) {
      const source = fs.readFileSync(path.join(__dirname, 'src', file), 'utf8') + '\n' + palette;
      const rendered = await page.evaluate(async source => {
        const { svg } = await mermaid.render('figure', source);
        document.body.innerHTML = svg;
        const element = document.querySelector('svg');
        // SVG text labels have no background of their own, so an edge would run through its label.
        for (const label of element.querySelectorAll('g.edgeLabel g.label')) {
          const box = label.getBBox();
          if (!box.width) continue;
          const back = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
          back.setAttribute('x', box.x - 6); back.setAttribute('y', box.y - 2);
          back.setAttribute('width', box.width + 12); back.setAttribute('height', box.height + 4);
          back.setAttribute('style', 'fill:#ffffff;opacity:1');   // Mermaid's own CSS makes label rects half transparent
          label.insertBefore(back, label.firstChild);
        }
        const [x, y, width, height] = element.getAttribute('viewBox').split(/\s+/).map(Number);
        const canvasWidth = Math.max(960, width + 48);
        const canvasHeight = height + 48;
        element.setAttribute('viewBox', `${x - (canvasWidth - width) / 2} ${y - 24} ${canvasWidth} ${canvasHeight}`);
        element.setAttribute('width', '960');
        element.setAttribute('height', String(Math.ceil(canvasHeight * 960 / canvasWidth)));
        element.setAttribute('style', 'display:block;background:#fff;max-width:none');
        return { svg: element.outerHTML, width, height, fontSize: 18 * 960 / canvasWidth };
      }, source);
      // Every figure shares one scale, so text is the same size in all of them.
      if (rendered.fontSize < 18) throw new Error(`${file}: wider than the shared 960-pixel canvas (${Math.round(rendered.width)} px of content); shorten labels or simplify the source`);
      const stem = path.join(__dirname, file.replace(/\.mmd$/, ''));
      fs.writeFileSync(stem + '.svg', rendered.svg + '\n');
      await (await page.$('svg')).screenshot({ path: stem + '.png' });
      console.log(`${file}: content ${Math.round(rendered.width)} x ${Math.round(rendered.height)}, inline font ${rendered.fontSize.toFixed(1)}px`);
    }
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
