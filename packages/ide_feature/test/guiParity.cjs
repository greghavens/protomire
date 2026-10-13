/* Run only inside the real Code - OSS Extension Host, not node or a mock browser. */
const vscode=require('vscode');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const os=require('node:os');
const crypto=require('node:crypto');
const cp=require('node:child_process');
const net=require('node:net');
const {promisify}=require('node:util');
exports.run=async function(){
 const root=path.resolve(__dirname,'../../..');
 const evidence=process.env.MODELBUILDER_GUI_EVIDENCE||path.join(root,'evidence/gui/workbench');
 await fs.mkdir(evidence,{recursive:true});
 const tmp=await fs.mkdtemp(path.join(os.tmpdir(),'modelbuilder-gui-'));
 const python=process.env.MODELBUILDER_GUI_PYTHON||path.join(root,'.venv/bin/python');
 const workspace=vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;assert(workspace,'Owned GUI fixture workspace missing');
 const fixtureMarker=path.join(workspace,'modelbuilder-gui-fixture.txt');await fs.writeFile(fixtureMarker,'Disposable Modelbuilder GUI source fixture\n');
 await promisify(cp.execFile)('git',['-C',workspace,'add','--','modelbuilder-gui-fixture.txt']);
 const head=await promisify(cp.execFile)('git',['-C',workspace,'rev-parse','--verify','HEAD']).then(()=>true,()=>false);
 if(!head)await promisify(cp.execFile)('git',['-C',workspace,'-c','user.name=Modelbuilder Test','-c','user.email=fixture@example.invalid','commit','-m','Initialize disposable GUI source fixture']);
 const operatorConfig=path.join(tmp,'operator.json');await fs.writeFile(operatorConfig,JSON.stringify({source_root:workspace,source_roots:{'gui-fixture':{path:workspace,trusted:false}}}));
 const token=crypto.randomBytes(32).toString('hex');
 const server=net.createServer();await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const port=server.address().port;await new Promise(resolve=>server.close(resolve));
 const url=`http://127.0.0.1:${port}`;
 const env={...process.env,MODELBUILDER_OPERATOR_CONFIG:operatorConfig,MODELBUILDER_GUI_IMPORT_BUNDLE:path.join(tmp,'shipping-import-bundle.json'),PYTHONPATH:path.join(root,'src'),MODELBUILDER_DB_PATH:path.join(tmp,'model.sqlite3'),MODELBUILDER_PORT:String(port),MODELBUILDER_HOST:'127.0.0.1',MODELBUILDER_BACKEND:'none',MODELBUILDER_TOKEN:token,MODELBUILDER_API_URL:url,MODELBUILDER_GUI_PYTHON:python,MODELBUILDER_GUI_EVIDENCE:evidence};
 delete env.MODELBUILDER_TOKENS_JSON;
 const api=cp.spawn(python,['-m','modelbuilder.api'],{cwd:root,env,stdio:['ignore','pipe','pipe']});let apiLog='';api.stdout.on('data',b=>apiLog+=b);api.stderr.on('data',b=>apiLog+=b);
 let exported;
 const dialogConfig=vscode.workspace.getConfiguration('window');const previousDialogStyle=dialogConfig.inspect('dialogStyle')?.globalValue;
 const fileConfig=vscode.workspace.getConfiguration('files');const previousSimpleDialog=fileConfig.inspect('simpleDialog.enable')?.globalValue;
 const report={app:vscode.env.appName,editor:vscode.version,status:'RUNNING',tests:[]};
 const save=()=>fs.writeFile(path.join(evidence,'workbench-host-results.json'),JSON.stringify(report,null,2));
 const wait=async(fn,timeout=30000)=>{const end=Date.now()+timeout;while(Date.now()<end){if(await fn())return;await new Promise(r=>setTimeout(r,150));}throw Error('Timed out waiting for real runtime state');};
 const check=async(name,fn)=>{const result=await fn();report.tests.push({name,status:'PASS',evidence:result});await save();};
 try{
  await wait(async()=>fetch(url+'/health').then(r=>r.ok).catch(()=>false));
  const preparation=await promisify(cp.execFile)(python,[path.join(__dirname,'prepareGuiBundle.py'),root,path.join(root,'examples/custom-pytorch/recipe.json'),env.MODELBUILDER_GUI_IMPORT_BUNDLE],{cwd:root,env});await fs.writeFile(path.join(evidence,'import-fixture.json'),preparation.stdout);
  assert(vscode.workspace.isTrusted,'Test workspace must be trusted through normal Code - OSS workspace trust UI.');
  await dialogConfig.update('dialogStyle','custom',vscode.ConfigurationTarget.Global);
  await fileConfig.update('simpleDialog.enable',true,vscode.ConfigurationTarget.Global);
  await vscode.commands.executeCommand('workbench.action.closeAllEditors');
  await vscode.commands.executeCommand('workbench.action.joinAllGroups');
  const extension=vscode.extensions.getExtension('modelbuilder.modelbuilder-workbench');assert(extension,'Built-in workbench missing');
  exported=await extension.activate();
  await vscode.commands.executeCommand('modelbuilder.configureConnection',{url,token});
  const sources=await exported.client.invoke('project.source.list',{});const source=sources.sources.find(item=>item.alias==='gui-fixture');assert(source?.available,'Configured GUI fixture source missing');
  const project=await exported.client.invoke('project.create',{model_name:'GUI parity model',slug:'gui-'+crypto.randomUUID().slice(0,8),source_alias:source.alias,source_repository_id:source.repository_id,trusted:true,idempotency_key:crypto.randomUUID()});
  let recipe=(await exported.client.invoke('project.open',{project_id:project.project_id})).recipe;assert(recipe,'Project creation must provide exactly one current recipe');
  await check('native-monaco-source-save',async()=>{
   const uri=await exported.files.open(project.project_id,recipe.recipe_id);
   const doc=await vscode.workspace.openTextDocument(uri);const editor=await vscode.window.showTextDocument(doc);
   const draft=JSON.parse(doc.getText());assert.equal(draft.digest,undefined,'Computed digest must not violate canonical body JSON Schema');draft.name='Native JSON authored recipe';
   assert(await editor.edit(edit=>edit.replace(new vscode.Range(doc.positionAt(0),doc.positionAt(doc.getText().length)),JSON.stringify(draft,null,2))));assert(await doc.save());
   recipe=await exported.client.invoke('recipe.get',{project_id:project.project_id,recipe_id:recipe.recipe_id});
   assert.equal(recipe.name,draft.name);assert.equal(recipe.revision,2);assert.equal(doc.languageId,'json');
   return {project_id:recipe.project_id,recipe_id:recipe.recipe_id,revision:recipe.revision,digest:recipe.digest,scheme:doc.uri.scheme,language:doc.languageId};
  });
  await vscode.commands.executeCommand('workbench.action.closePanel');
  await vscode.commands.executeCommand('workbench.action.closeSidebar');
  await vscode.commands.executeCommand('workbench.action.closeAllEditors');
  await vscode.commands.executeCommand('modelbuilder.openWorkbench');
  await new Promise(resolve=>setTimeout(resolve,2500));await vscode.commands.executeCommand('notifications.clearAll');
  const fixture={project_id:project.project_id,model_name:project.model_name,recipe_id:recipe.recipe_id,initial_revision:recipe.revision};
  await fs.writeFile(path.join(evidence,'fixture-identities.json'),JSON.stringify(fixture,null,2));
  await check('real-webview-graph-mcp-parity',async()=>{
   const result=await promisify(cp.execFile)(process.env.MODELBUILDER_GUI_NODE||'node',[path.join(__dirname,'guiDom.cjs'),JSON.stringify(fixture)],{cwd:root,env,timeout:process.platform==='darwin'?900000:180000,maxBuffer:8*1024*1024});
   await fs.writeFile(path.join(evidence,'dom-driver.log'),result.stdout+result.stderr);
   const dom=JSON.parse(await fs.readFile(path.join(evidence,'workbench-dom-results.json'),'utf8'));assert.equal(dom.status,'PASS');return dom;
  });
  report.status='PASS';await save();
 }catch(error){report.status='FAIL';report.error=String(error.stack||error);await save();throw error;}
 finally{await fileConfig.update('simpleDialog.enable',previousSimpleDialog,vscode.ConfigurationTarget.Global);await dialogConfig.update('dialogStyle',previousDialogStyle,vscode.ConfigurationTarget.Global);await exported?.clearConnection();api.kill('SIGTERM');await fs.writeFile(path.join(evidence,'operation-service.log'),apiLog);await fs.rm(tmp,{recursive:true,force:true});}
};
