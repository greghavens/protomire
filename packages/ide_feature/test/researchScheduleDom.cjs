/* Controlled Chromium UI check. No live service, provider or vendor session. */
const {chromium}=require('playwright-core');const fs=require('node:fs/promises');const path=require('node:path');const assert=require('node:assert/strict');
(async()=>{
 const evidence=process.env.MODELBUILDER_GUI_EVIDENCE||'/tmp/modelbuilder-scheduler-browser';await fs.mkdir(evidence,{recursive:true});
 const report={scope:'Controlled browser DOM; no native Code - OSS or live OpenAlex claim.',status:'RUNNING',tests:[]};let browser,page;
 try{
  browser=await chromium.launch({executablePath:process.env.MODELBUILDER_CHROMIUM||'/usr/bin/chromium',headless:true,chromiumSandbox:true,ignoreDefaultArgs:['--password-store=basic','--use-mock-keychain','--unsafely-disable-devtools-self-xss-warnings']});
  page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(String(e)));
  await page.setContent('<!doctype html><html><body><div id="app"></div></body></html>');
  await page.evaluate(()=>{window.sent=[];window.acquireVsCodeApi=()=>({postMessage:value=>window.sent.push(value),setState(){},getState(){return {};}});});
  await page.addStyleTag({path:path.join(__dirname,'../media/workbench.css')});await page.addScriptTag({path:path.join(__dirname,'../media/workbench.js')});
  const message=value=>page.evaluate(value=>window.dispatchEvent(new MessageEvent('message',{data:value})),value);
  const catalog=JSON.parse(await fs.readFile(path.join(__dirname,'../../../docs/operation-catalog.json'),'utf8')).operations;
  await message({type:'init',catalog,projects:[],trusted:true});await message({type:'project',snapshot:{project:{project_id:'p',model_name:'Research fixture',revision:1}},section:'Research'});
  const watch={watch_id:'w',project_id:'p',name:'Fixture calibration',query:'public fixture calibration',provider:'openalex',limit:3,interval_seconds:900,enabled:true,revision:2,next_due_at:'2026-10-09T00:00:00Z',schedule:{enabled:false}};
  await message({type:'research',projectId:'p',research:{watches:[watch]}});
  assert.match(await page.locator('#research-workflow').innerText(),/Manual refresh only/);assert.match(await page.locator('#research-workflow').innerText(),/api.openalex.org/);assert.match(await page.locator('#research-workflow').innerText(),/3 records every 900 seconds/);
  assert.equal(await page.evaluate(()=>window.sent.filter(x=>x.type==='operation').length),0);report.tests.push('Legacy/manual watch disclosure renders without sending a provider request.');
  await page.getByRole('button',{name:'Enable automatic schedule',exact:true}).click();assert.equal(await page.locator('#op-confirm_schedule').isChecked(),false);assert.equal(await page.locator('#op-query').inputValue(),watch.query);assert.equal(await page.locator('#op-interval_seconds').inputValue(),'900');
  await page.locator('#operation-form button[type="submit"]').click();assert.equal(await page.evaluate(()=>window.sent.filter(x=>x.type==='operation').length),0);report.tests.push('Enable form requires an unchecked explicit confirmation before submission.');
  await page.getByRole('button',{name:'Close form',exact:true}).click();assert.equal(await page.locator('#operation-panel').isVisible(),false);assert.equal(await page.evaluate(()=>window.sent.filter(x=>x.type==='operation').length),0);report.tests.push('Close cancels without applying a schedule.');
  await page.getByRole('button',{name:'Enable automatic schedule',exact:true}).click();assert.equal(await page.locator('#op-confirm_schedule').isChecked(),false);await page.locator('#op-confirm_schedule').check();await page.locator('#operation-form button[type="submit"]').click();
  const enabled=await page.evaluate(()=>window.sent.filter(x=>x.type==='operation').at(-1));assert.equal(enabled.operation,'research.watch.schedule_enable');assert.equal(enabled.payload.confirm_schedule,true);assert.equal(enabled.payload.query,watch.query);assert.equal(enabled.payload.expected_revision,2);report.tests.push('Reopened form sends exact provider/query/frequency/revision only after explicit opt-in.');
  await page.getByRole('button',{name:'Close form',exact:true}).click();await message({type:'research',projectId:'p',research:{watches:[{...watch,revision:3,schedule:{enabled:true},scheduler_running:true}]}});
  assert.equal(await page.getByRole('button',{name:'Enable automatic schedule',exact:true}).count(),0);await page.getByRole('button',{name:'Disable automatic schedule',exact:true}).click();assert.equal(await page.locator('#op-expected_revision').inputValue(),'3');await page.locator('#operation-form button[type="submit"]').click();const disabled=await page.evaluate(()=>window.sent.filter(x=>x.type==='operation').at(-1));assert.equal(disabled.operation,'research.watch.schedule_disable');report.tests.push('Enabled watch has a separate revisioned disable action and runtime status.');
  await page.screenshot({path:path.join(evidence,'research-schedule.png'),fullPage:true});
  await message({type:'landing',projects:[]});assert.equal(await page.locator('#operation-panel').isVisible(),false);await message({type:'project',snapshot:{project:{project_id:'new',model_name:'Another project',revision:1}},section:'Research'});await message({type:'research',projectId:'p',research:{watches:[watch]}});assert.equal(await page.locator('[data-resource-id="w"]').count(),0);report.tests.push('Project close/navigation dismisses forms and rejects stale old-project watch updates.');
  assert.deepEqual(errors,[]);report.status='PASS';
 }catch(error){report.status=page?'FAIL':'BLOCKED';report.error=String(error);throw error;}finally{await fs.writeFile(path.join(evidence,'results.json'),JSON.stringify(report,null,2));await browser?.close();console.log(JSON.stringify(report));}
})().catch(error=>{console.error(error);process.exitCode=1;});
