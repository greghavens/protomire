// Approve only the repository-owned test fixture through the normal trust UI.
// Never disable workspace trust or select the parent-folder checkbox.
const {chromium}=require('../../packages/ide_feature/node_modules/playwright-core');
const debugPort=Number(process.env.MODELBUILDER_GUI_DEBUG_PORT || '9222');
if(!Number.isInteger(debugPort)||debugPort<1||debugPort>65535)throw Error('Invalid MODELBUILDER_GUI_DEBUG_PORT');
const fs=require('node:fs/promises');
const fixture=process.argv[2];
const python=process.env.MODELBUILDER_GUI_PYTHON;
const evidence=process.env.MODELBUILDER_GUI_EVIDENCE;
(async()=>{
  let browser;
  for(let i=0;i<60;i++){
    try{browser=await chromium.connectOverCDP(`http://127.0.0.1:${debugPort}`);break;}
    catch{await new Promise(r=>setTimeout(r,1000));}
  }
  if(!browser)throw Error('Cannot connect to the real IDE trust dialog');
  try{
    for(let i=0;i<570;i++){
      const report=process.env.MODELBUILDER_GUI_SUITE==='workbench'?'workbench-host-results.json':process.env.MODELBUILDER_GUI_SUITE==='execution'?'gui-execution-host-results.json':'smoke-results.json';
      const done=await fs.readFile(evidence+'/'+report,'utf8').then(JSON.parse).catch(()=>null);
      if(done && ['PASS','FAIL','BLOCKED'].includes(done.status))return;
      for(const context of browser.contexts())for(const page of context.pages()){
        const button=page.getByRole('button',{name:'Yes, I trust the authors',exact:true});
        if(await button.isVisible().catch(()=>false)){
          const dialog=page.getByRole('dialog');
          if(!(await dialog.innerText()).includes(fixture))throw Error('Trust dialog does not identify the owned test fixture');
          await button.click();console.log('Trusted only the explicitly identified test fixture through the standard UI.');
        }
        const kernelSource=page.getByText('Python Environments...', {exact:true});
        if(await kernelSource.isVisible().catch(()=>false)){await kernelSource.click();console.log('Selected the normal Python kernel source.');}
        if(python){const kernel=page.locator('.monaco-list-row').filter({hasText:python});
          if(await kernel.count()===1 && await kernel.isVisible().catch(()=>false)){await kernel.click();console.log('Selected the explicitly pinned local Python kernel.');}}
      }
      await new Promise(r=>setTimeout(r,1000));
    }
    throw Error('GUI fixture preparation timed out');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
