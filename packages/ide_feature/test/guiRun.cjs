/* Real GUI execution: a shipping pinned recipe, actual button and confirmation. */
const vscode=require('vscode');const assert=require('node:assert/strict');const fs=require('node:fs/promises');const path=require('node:path');const crypto=require('node:crypto');const cp=require('node:child_process');const net=require('node:net');const {promisify}=require('node:util');
exports.run=async function(){
 const root=path.resolve(__dirname,'../../..');const evidence=process.env.MODELBUILDER_GUI_EVIDENCE||path.join(root,'evidence/gui/execution');await fs.mkdir(evidence,{recursive:true});
 const runtime=path.join(root,'.runtime','gui-execution-'+crypto.randomUUID());await fs.mkdir(runtime,{recursive:true});
 const python=process.env.MODELBUILDER_GUI_PYTHON||path.join(root,'.venv/bin/python');const token=crypto.randomBytes(32).toString('hex');
 const listener=net.createServer();await new Promise(resolve=>listener.listen(0,'127.0.0.1',resolve));const port=listener.address().port;await new Promise(resolve=>listener.close(resolve));const url=`http://127.0.0.1:${port}`;
 const env={...process.env,PYTHONPATH:path.join(root,'src'),MODELBUILDER_DB_PATH:path.join(runtime,'service.sqlite3'),MODELBUILDER_PORT:String(port),MODELBUILDER_HOST:'127.0.0.1',MODELBUILDER_BACKEND:'zenml',MODELBUILDER_SOURCE_ROOT:root,MODELBUILDER_BACKEND_PYTHON:process.env.MODELBUILDER_BACKEND_PYTHON||path.join(root,'.venv-backend/bin/python'),MODELBUILDER_TOKEN:token,MODELBUILDER_API_URL:url,MODELBUILDER_GUI_EVIDENCE:evidence};delete env.MODELBUILDER_TOKENS_JSON;
 const api=cp.spawn(python,['-m','modelbuilder.api'],{cwd:root,env,stdio:['ignore','pipe','pipe']});let logs='';api.stdout.on('data',b=>logs+=b);api.stderr.on('data',b=>logs+=b);
 const report={app:vscode.env.appName,editor:vscode.version,status:'RUNNING',tests:[],runtime_directory:runtime};const save=()=>fs.writeFile(path.join(evidence,'gui-execution-host-results.json'),JSON.stringify(report,null,2));
 const config=vscode.workspace.getConfiguration('window'),previous=config.inspect('dialogStyle')?.globalValue;let exported,project;
 try{
  let ready=false;for(let i=0;i<150;i++){ready=await fetch(url+'/health').then(r=>r.ok).catch(()=>false);if(ready)break;await new Promise(r=>setTimeout(r,200));}assert(ready,'Real service did not start');assert(vscode.workspace.isTrusted,'Use normal trusted acceptance workspace');
  await config.update('dialogStyle','custom',vscode.ConfigurationTarget.Global);
  for(const command of ['workbench.action.closeAllEditors','workbench.action.joinAllGroups','workbench.action.closePanel','workbench.action.closeSidebar'])await vscode.commands.executeCommand(command);
  const extension=vscode.extensions.getExtension('modelbuilder.modelbuilder-workbench');assert(extension);exported=await extension.activate();await vscode.commands.executeCommand('modelbuilder.configureConnection',{url,token});
  project=await exported.client.invoke('project.create',{slug:'gui-run-'+crypto.randomUUID().slice(0,8),model_name:'GUI real ZenML CPU run',idempotency_key:crypto.randomUUID()});
  const manifest=JSON.parse(await fs.readFile(path.join(root,'examples/custom-pytorch/recipe.json'),'utf8'));
  const recipe=await exported.client.invoke('recipe.create',{project_id:project.project_id,recipe:manifest,idempotency_key:crypto.randomUUID()});
  const fixture={project_id:project.project_id,recipe_id:recipe.recipe_id,recipe_revision:recipe.revision,recipe_digest:recipe.digest,source_commit:recipe.source.commit};await fs.writeFile(path.join(evidence,'execution-fixture.json'),JSON.stringify(fixture,null,2));
  await vscode.commands.executeCommand('modelbuilder.openWorkbench',{projectId:project.project_id,recipeId:recipe.recipe_id,section:'Runs'});await new Promise(r=>setTimeout(r,2000));await vscode.commands.executeCommand('notifications.clearAll');
  const result=await promisify(cp.execFile)(process.env.MODELBUILDER_GUI_NODE||'node',[path.join(__dirname,'guiRunDom.cjs'),JSON.stringify(fixture)],{cwd:root,env,timeout:300000,maxBuffer:8*1024*1024});await fs.writeFile(path.join(evidence,'execution-dom-driver.log'),result.stdout+result.stderr);
  const dom=JSON.parse(await fs.readFile(path.join(evidence,'gui-execution-dom-results.json'),'utf8'));assert.equal(dom.status,'PASS');report.tests.push({name:'real-gui-submit-state-logs-artifacts',status:'PASS',evidence:dom});report.status='PASS';await save();
 }catch(error){report.status='FAIL';report.error=String(error.stack||error);await save();throw error;}
 finally{
  // Teardown only: never leave an unfinished test worker running after a failure.
  if(exported&&project){try{const {runs}=await exported.client.invoke('run.list',{project_id:project.project_id});for(const run of runs??[])if(!['succeeded','failed','cancelled'].includes(run.status))await exported.client.invoke('run.cancel',{project_id:project.project_id,operation_id:run.operation_id,idempotency_key:crypto.randomUUID()});}catch(error){await fs.writeFile(path.join(evidence,'cleanup-warning.json'),JSON.stringify({message:String(error)}));}}
  await exported?.clearConnection();await config.update('dialogStyle',previous,vscode.ConfigurationTarget.Global);api.kill('SIGTERM');await fs.writeFile(path.join(evidence,'execution-service.log'),logs);
 }
};
