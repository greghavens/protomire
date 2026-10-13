/* Real HTTP + external MCP contract check for the compiled TypeScript adapter. */
const assert=require('node:assert/strict');const cp=require('node:child_process');const fs=require('node:fs/promises');const os=require('node:os');const path=require('node:path');const crypto=require('node:crypto');const net=require('node:net');const {promisify}=require('node:util');
const {OperationClient}=require('../out/client');const {RecipeDocument,newNode}=require('../out/document');
const {withStageParallelism,executionSettingsIdentity}=require('../out/executionSettings');
(async()=>{
 const root=path.resolve(__dirname,'../../..'),tmp=await fs.mkdtemp(path.join(os.tmpdir(),'modelbuilder-ts-'));
 const listener=net.createServer();await new Promise(resolve=>listener.listen(0,'127.0.0.1',resolve));const port=listener.address().port;await new Promise(resolve=>listener.close(resolve));
 const token=crypto.randomBytes(32).toString('hex'),reader=crypto.randomBytes(32).toString('hex'),url=`http://127.0.0.1:${port}`;
 const python=process.env.MODELBUILDER_GUI_PYTHON||path.join(root,'.venv/bin/python');
 const env={...process.env,PYTHONPATH:path.join(root,'src'),MODELBUILDER_PORT:String(port),MODELBUILDER_HOST:'127.0.0.1',MODELBUILDER_BACKEND:'none',MODELBUILDER_DB_PATH:path.join(tmp,'service.sqlite3'),MODELBUILDER_TOKENS_JSON:JSON.stringify({[token]:{id:'ts-owner',scopes:['read','modify','execute']},[reader]:{id:'ts-reader',scopes:['read']}}),MODELBUILDER_API_URL:url,MODELBUILDER_TOKEN:token};
 const service=cp.spawn(python,['-m','modelbuilder.api'],{cwd:root,env,stdio:['ignore','pipe','pipe']});let log='';service.stderr.on('data',b=>log+=b);service.stdout.on('data',b=>log+=b);
 try{
  let ready=false;for(let i=0;i<150;i++){ready=await fetch(url+'/health').then(r=>r.ok).catch(()=>false);if(ready)break;await new Promise(r=>setTimeout(r,100));}assert(ready,log);
  const client=new OperationClient(()=>url,async()=>token);const catalog=await client.catalog();assert(catalog.some(o=>o.name==='recipe.update'));
  const project=await client.invoke('project.create',{slug:'typescript-parity',model_name:'TypeScript parity',idempotency_key:crypto.randomUUID()});
  const recipe=(await client.invoke('project.open',{project_id:project.project_id})).recipe;assert(recipe,'Project must own its one current recipe');
  const document=new RecipeDocument(client);await document.load(project.project_id,recipe.recipe_id);
  const data=newNode('data','dataset.synthetic',recipe.source.commit);data.outputs=[{name:'tensor',type:'tensor'}];
  const pythonNode=newNode('custom','code.python',recipe.source.commit);pythonNode.inputs=[{name:'data',type:'tensor'}];pythonNode.outputs=[{name:'checkpoint',type:'checkpoint'}];
  const graph={...recipe,nodes:[data,pythonNode],edges:[{source_node:'data',source_port:'tensor',target_node:'custom',target_port:'data'}]};
  const draft=withStageParallelism(graph,2,executionSettingsIdentity(graph));
  const saved=await document.save(draft,crypto.randomUUID());assert.equal(saved.revision,2);
  const callMcp=async(input)=>{const {stdout}=await promisify(cp.execFile)(python,[path.join(__dirname,'mcpEdit.py'),JSON.stringify({project_id:project.project_id,recipe_id:recipe.recipe_id,...input})],{env,timeout:30000});return JSON.parse(stdout);};
  const mcpRead=await callMcp({});assert.equal(mcpRead.digest,saved.digest);assert.deepEqual(mcpRead.nodes,saved.nodes);assert.deepEqual(mcpRead.execution,{max_parallel_stages:2});
  const changed=await callMcp({name:'MCP edit',idempotency_key:crypto.randomUUID()});assert.equal(changed.revision,3);
  await assert.rejects(document.save({...saved,name:'stale TypeScript edit'},crypto.randomUUID()),e=>e.status===409);
  const diff=await document.diff(saved);assert(diff);assert.equal(document.current.revision,2);
  const reloaded=await document.load(project.project_id,recipe.recipe_id);assert.equal(reloaded.digest,changed.digest);assert.deepEqual(reloaded.execution,{max_parallel_stages:2});
  const denied=new OperationClient(()=>url,async()=>reader);await assert.rejects(denied.invoke('recipe.update',{project_id:project.project_id,recipe_id:recipe.recipe_id,recipe:changed,expected_revision:3,idempotency_key:crypto.randomUUID()}),e=>e.status===403);
  console.log(JSON.stringify({status:'PASS',tests:8,project_id:project.project_id,recipe_id:recipe.recipe_id,revision:changed.revision,digest:changed.digest,checks:['catalog','typed-graph-python-save','typescript-to-mcp-equality','mcp-to-typescript-equality','canonical-stage-settings-parity','stale-409-preserves-base','server-diff','read-only-denied']},null,2));
 }finally{service.kill('SIGTERM');await new Promise(resolve=>service.once('exit',resolve));await fs.rm(tmp,{recursive:true,force:true});}
})().catch(error=>{console.error(error);process.exitCode=1;});
