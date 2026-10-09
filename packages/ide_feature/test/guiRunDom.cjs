const {chromium}=require('playwright-core');const {discoverWebview}=require('./cdpWebview.cjs');const assert=require('node:assert/strict');const fs=require('node:fs/promises');const path=require('node:path');
(async()=>{
 const fixture=JSON.parse(process.argv[2]),evidence=process.env.MODELBUILDER_GUI_EVIDENCE;const endpoint=process.env.MODELBUILDER_GUI_CDP||'http://127.0.0.1:9222';
 const browser=await chromium.connectOverCDP(endpoint);let raw,page;const report={status:'RUNNING',tests:[],fixture};const save=()=>fs.writeFile(path.join(evidence,'gui-execution-dom-results.json'),JSON.stringify(report,null,2));
 const wait=async(fn,timeout=180000)=>{const end=Date.now()+timeout;while(Date.now()<end){const result=await fn();if(result)return result;await new Promise(r=>setTimeout(r,500));}throw Error('Timed out waiting for real GUI run state');};
 const api=async(operation,payload)=>{const response=await fetch(process.env.MODELBUILDER_API_URL+'/v1/operations/'+operation,{method:'POST',headers:{Authorization:'Bearer '+process.env.MODELBUILDER_TOKEN,'Content-Type':'application/json'},body:JSON.stringify(payload)});const result=await response.json();assert(response.ok,JSON.stringify(result));return result;};
 try{
  page=browser.contexts().flatMap(c=>c.pages()).find(p=>p.url().includes('workbench'));
  await wait(async()=>{raw=await discoverWebview(endpoint,inventory=>fs.writeFile(path.join(evidence,'execution-cdp-targets.json'),JSON.stringify(inventory,null,2)));return raw;},30000);const frame=raw.frame;assert(page);
  await wait(async()=>JSON.parse(await frame.locator('#source').inputValue()||'{}').recipe_id===fixture.recipe_id,30000);
  await frame.locator('[data-section="Runs"]').click();await frame.locator('#run-actions button:nth-child(2)').click();assert.equal(await frame.locator('#operation-title').innerText(),'run.submit');
  await frame.locator('#operation-form button[type="submit"]').click();await page.getByRole('button',{name:'Continue',exact:true}).click();
  const observed=[];const final=await wait(async()=>{
   const text=await frame.locator('#run-summary').innerText();const first=text.indexOf('{');if(first<0)return false;let state;try{state=JSON.parse(text.slice(first));}catch{return false;}
   if(state.status&&!observed.some(item=>item.status===state.status))observed.push({status:state.status,backend_run_id:state.backend_run_id??null,last_reconciled_at:state.last_reconciled_at});
   if(['failed','cancelled'].includes(state.status))throw Error('Actual GUI run failed: '+JSON.stringify(state));return state.status==='succeeded'?state:false;
  });
  assert.match(final.backend_run_id,/^[0-9a-f]{8}-[0-9a-f-]{27}$/i);assert.equal(final.backend_status,'completed');assert(final.last_reconciled_at);assert(final.model_version_ref);
  const logs=JSON.parse(await frame.locator('#run-logs').innerText());const artifacts=JSON.parse(await frame.locator('#run-artifacts').innerText());assert(typeof logs.logs==='string'&&logs.logs.length>0);assert(artifacts.artifacts.length>=4);assert(artifacts.artifacts.every(a=>a.backend_run_id===final.backend_run_id&&a.artifact_id));
  const server=await api('run.status',{project_id:fixture.project_id,operation_id:final.operation_id});assert.equal(server.backend_run_id,final.backend_run_id);assert.equal(server.status,final.status);
  report.tests.push({name:'actual-button-and-confirmation-submit',status:'PASS',evidence:{operation_id:final.operation_id,backend_run_id:final.backend_run_id,observed_states:observed}},{name:'actual-gui-reconciled-status-metrics-model-version',status:'PASS',evidence:{status:final.status,backend_status:final.backend_status,last_reconciled_at:final.last_reconciled_at,metrics:final.metrics,model_version_ref:final.model_version_ref}},{name:'actual-gui-logs-artifacts',status:'PASS',evidence:{log_characters:logs.logs.length,artifacts:artifacts.artifacts}});
  await fs.writeFile(path.join(evidence,'visible-run-status.json'),JSON.stringify(final,null,2));await fs.writeFile(path.join(evidence,'visible-run-logs.txt'),logs.logs);await fs.writeFile(path.join(evidence,'visible-run-artifacts.json'),JSON.stringify(artifacts,null,2));
  await raw.frame.scrollTop();await page.screenshot({path:path.join(evidence,'real-gui-zenml-run.png')});report.status='PASS';await save();
 }catch(error){report.status='FAIL';report.error=String(error.stack||error);await save();if(page)await page.screenshot({path:path.join(evidence,'execution-failure.png')});throw error;}
 finally{raw?.cdp.close();await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
