// Screenshot the actual Electron workbench, never the surrounding shell window.
const {chromium}=require('../../packages/ide_feature/node_modules/playwright-core');
const debugPort=Number(process.env.MODELBUILDER_GUI_DEBUG_PORT || '9222');
if(!Number.isInteger(debugPort)||debugPort<1||debugPort>65535)throw Error('Invalid MODELBUILDER_GUI_DEBUG_PORT');
(async()=>{
  const browser=await chromium.connectOverCDP(`http://127.0.0.1:${debugPort}`);
  try {
    const page=browser.contexts().flatMap(context=>context.pages()).find(page=>page.url().includes('/workbench/'));
    if(!page)throw Error('Real Electron workbench page not found');
    await page.screenshot({path:process.argv[2]});
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
