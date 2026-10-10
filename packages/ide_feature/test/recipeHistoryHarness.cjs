/* Native dialogs are doubles; this harness does not claim current Code-OSS GUI acceptance. */
const Module=require('node:module');
const mock={TreeItem:class{},TreeItemCollapsibleState:{None:0,Collapsed:1,Expanded:2},ThemeIcon:class{},EventEmitter:class{event=()=>{};fire(){}},workspace:{textDocuments:[]},window:{showWarningMessage:async()=> 'Restore revision',showInformationMessage(){}},commands:{async executeCommand(){}}};
const load=Module._load;Module._load=function(name){return name==='vscode'?mock:load.apply(this,arguments);};
const {Workbench}=require('../out/extension');Module._load=load;
const names=['recipe.history','recipe.get','recipe.diff','recipe.clone'];
const recipe=(revision=3)=>({project_id:'p',recipe_id:'r',revision,name:`Recipe r${revision}`,digest:`digest-${revision}`,nodes:[],edges:[]});
function host(client,current=recipe(),catalog=names.map(name=>({name,authorized:true}))) {
 const messages=[],saved=[],remote=[];let refreshes=0;
 const workbench=new Workbench({workspaceState:{get:(_key,value)=>value,update:async(key,value)=>saved.push({key,value:structuredClone(value)})}},client,{refresh(){refreshes++;}},{acceptRemote:value=>remote.push(value)});
 workbench.timer.unref();workbench.project={project_id:current.project_id,revision:1,model_name:'Model'};workbench.document.current=current;workbench.catalog=catalog;
 workbench.panel={dispose(){},webview:{postMessage:message=>{messages.push(message);return true;}}};
 const request=(action='list',extra={})=>({type:'recipeHistory',action,projectId:current.project_id,recipeId:current.recipe_id,expectedRevision:current.revision,requestId:1,...extra});
 return {workbench,messages,saved,remote,request};
}
function fixture() {
 const calls=[];let latest=recipe(),revisions=[recipe(1),recipe(2),latest];
 const client={invoke:async(operation,payload)=>{calls.push({operation,payload});if(operation==='recipe.history')return {recipe_id:'r',current_revision:latest.revision,revisions:structuredClone(revisions),legacy_recipes:[]};if(operation==='recipe.get')return revisions.find(item=>item.revision===(payload.revision??latest.revision));if(operation==='recipe.diff')return {recipe_id:'r',current_revision:latest.revision,equal:false,diff:`r${payload.from_revision} -> r${payload.to_revision}`};if(operation==='recipe.clone'){latest={...revisions.find(item=>item.revision===payload.source_revision),revision:latest.revision+1};revisions.push(latest);return latest;}if(operation==='project.open')return {project:{project_id:'p',revision:1},recipe:latest};throw Error(operation);}};
 mock.workspace.textDocuments=[];mock.window.showWarningMessage=async()=> 'Restore revision';mock.window.showInformationMessage=()=>{};
 return {...host(client),calls,client,latest:()=>latest};
}
module.exports={mock,recipe,host,fixture,names};
