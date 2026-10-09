// Screenshot the actual Electron workbench, never the surrounding shell window.
const {chromium}=require('../../packages/ide_feature/node_modules/playwright-core');
(async()=>{
  const browser=await chromium.connectOverCDP('http://127.0.0.1:9222');
  try {
    const page=browser.contexts().flatMap(context=>context.pages()).find(page=>page.url().includes('/workbench/'));
    if(!page)throw Error('Real Electron workbench page not found');
    await page.screenshot({path:process.argv[2]});
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
