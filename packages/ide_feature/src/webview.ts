import { newNode, removeNode, replaceNode, readVisualSource, assertNodePanelCurrent, recipeChoiceRecords } from './document';
import { OperationSpec, Recipe, RecipeEdge, RecipeNode, RecordValue } from './types';
declare function acquireVsCodeApi(): {postMessage(message:unknown):void;setState(value:unknown):void;getState():any};
const vscode=acquireVsCodeApi();
const $=<T extends HTMLElement=HTMLElement>(id:string)=>document.getElementById(id) as T;
const send=(type:string,body:RecordValue={})=>{if(type==='saveRecipe'&&nodePanelDirty){report('Apply or explicitly discard node panel changes before saving the recipe.');return;}vscode.postMessage({type,...body});};
const pretty=(value:unknown)=>JSON.stringify(value,null,2);
const app=$('app');
app.innerHTML=`
<header><div><span class="eyebrow">MODELBUILDER</span><h1>Model workbench</h1></div><div class="connection"><span id="connection" role="status">Connecting…</span><button id="connect">Connection</button></div></header>
<div class="projectbar"><label for="project">Model project</label><select id="project"><option value="">Select project</option></select><button id="create-project">New project</button><button id="refresh">Refresh</button><span id="project-id" class="mono muted"></span></div>
<nav aria-label="Model lifecycle" id="tabs"></nav>
<div id="notice" role="status" hidden></div><div id="error" role="alert" hidden></div>
<section id="Recipes" class="page">
  <div class="toolbar"><label for="recipe-select">Recipe</label><select id="recipe-select"><option value="">Choose a recipe</option></select><button id="new-recipe">New recipe</button><button id="native-source">Open native JSON</button><button id="validate">Validate</button><button id="diff">Diff</button><button id="save" class="primary">Save revision</button><button id="reload">Reload</button></div>
  <div id="identity" class="identity">Open or create a model project, then create a recipe.</div>
  <div id="remote" class="warning" hidden></div>
  <div id="conflict" class="conflict" hidden><h2>Concurrent edit conflict</h2><p>Your draft is preserved. Compare it to the current server revision before resolving.</p><pre id="conflict-diff"></pre><div class="columns"><div><h3>Current server</h3><pre id="server-json"></pre></div><div><h3>Your draft</h3><pre id="local-json"></pre></div></div><button id="discard-conflict">Load server revision and discard draft</button></div>
  <div class="editor-grid">
    <div class="graph-pane"><div class="toolbar"><h2>Pipeline graph</h2><button id="zoom-in" aria-label="Zoom in">+</button><button id="zoom-out" aria-label="Zoom out">−</button><button id="fit">Reset view</button><span class="muted">Drag to pan · Wheel to zoom</span></div><svg id="graph" role="img" aria-label="Editable typed pipeline graph" viewBox="0 0 900 440"><g id="graph-layer"></g></svg>
      <div class="toolbar"><label for="node-kind">Component</label><select id="node-kind"></select><input id="node-id" placeholder="Unique node ID" aria-label="New node ID"><button id="add-node">Add node</button></div>
      <details id="edge-form"><summary>Add a typed edge</summary><div class="edge-controls"><select id="source-node" aria-label="Source node"></select><select id="source-port" aria-label="Source output port"></select><span>→</span><select id="target-node" aria-label="Target node"></select><select id="target-port" aria-label="Target input port"></select><button id="add-edge">Connect</button></div><p class="muted">The service checks type compatibility and cycles when validating or saving.</p></details>
      <div id="edges"></div>
      <section id="node-panel" hidden><div class="toolbar"><h3 id="node-title"></h3><button id="delete-node" class="danger">Remove node</button></div><label>Implementation reference<textarea id="node-implementation" rows="3" spellcheck="false"></textarea></label><div class="columns"><label>Typed inputs (JSON)<textarea id="node-inputs" rows="4" spellcheck="false"></textarea></label><label>Typed outputs (JSON)<textarea id="node-outputs" rows="4" spellcheck="false"></textarea></label></div><label>Parameters (JSON)<textarea id="node-parameters" rows="6" spellcheck="false"></textarea></label><button id="apply-node">Apply node changes to draft</button><button id="reset-node-panel">Discard panel edits / reload node</button></section>
    </div>
    <div class="source-pane"><div class="toolbar"><h2>Canonical recipe JSON</h2><span id="dirty" class="pill">Saved</span></div><label class="sr-only" for="source">Canonical recipe JSON source</label><textarea id="source" spellcheck="false" aria-describedby="source-help"></textarea><p id="source-help" class="muted">Graph and JSON share this draft. Saving calls the same revision-checked operation as MCP. Ctrl/Cmd+S saves the recipe.</p><span id="parse-error" role="alert"></span></div>
  </div>
</section>
<section id="Research" class="page" hidden><h2>Research, evidence and decisions</h2><p>Keep sources, propositions and measurable hypotheses separate. Preserve negative and inconclusive results.</p><div id="research-actions" class="toolbar"></div><div id="research-records" class="cards"></div></section>
<section id="Code" class="page" hidden><h2>Architecture and code</h2><p>Python is a first-class node with an explicit entrypoint and typed inputs/outputs. Edit arbitrary model code in the native editor.</p><button id="open-code">Open code explorer</button><pre id="code-info"></pre></section>
<section id="Data" class="page" hidden><h2>Dataset references</h2><p>References and immutable revisions live in your recipe. ModelBuilder does not mirror datasets.</p><pre id="data-info"></pre></section>
<section id="Runs" class="page" hidden><h2>Runs and evaluations</h2><div class="toolbar" id="run-actions"></div><div class="run-grid"><div id="run-list" class="cards"></div><div><div id="run-summary"></div><p class="warning">A cancellation request is not confirmation that the worker has stopped. Only the reconciled backend status confirms termination.</p><h3>Backend logs</h3><pre id="run-logs">Select a run.</pre><h3>Artifacts and checkpoints</h3><pre id="run-artifacts">No run selected.</pre></div></div></section>
<section id="Versions" class="page" hidden><h2>Model versions and lineage</h2><div class="toolbar" id="version-actions"></div><div id="versions" class="cards"></div></section>
<section id="Compute" class="page" hidden><h2>Local compute preview</h2><p>Inspect actual target capabilities and recipe requirements. Remote spending is disabled. No remote budget enforcement is claimed.</p><div class="toolbar" id="compute-actions"></div><pre id="compute-info"></pre></section>
<section id="operation-panel" hidden><div class="toolbar"><h2 id="operation-title"></h2><button id="close-operation">Close form</button></div><p id="operation-description"></p><form id="operation-form"></form></section>
<section id="result-panel" hidden><div class="toolbar"><h2 id="result-title">Service result</h2><button id="close-result">Close result</button></div><pre id="result"></pre></section>
<footer>Shared operation service · Native MCP parity · No assistant required</footer>`;
let catalog:OperationSpec[]=[];
let snapshot:RecordValue={};
let recipe:Recipe|undefined;
let loadedRecipe:Recipe|undefined;
let selectedNode:string|undefined;
let nodePanelDirty=false;
let nodePanelBaseline="";
let activeRun:string|undefined;
let section='Recipes';
let dirty=false;
let busy=false;
let pan={x:0,y:0,scale:1};
let selectedOperation:OperationSpec|undefined;
const layouts=new Map<string,{x:number;y:number}>();
const kinds=['dataset.import','dataset.synthetic','dataset.validate','model.initialize','model.train','model.evaluate','model.compare','model.checkpoint','model.export','training.kl_anchor','code.python'];
function node<K extends keyof HTMLElementTagNameMap>(tag:K,text?:string,className?:string):HTMLElementTagNameMap[K] {const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;}
function button(label:string,action:()=>void):HTMLButtonElement {const el=node('button',label);el.addEventListener('click',action);return el;}
function options(select:HTMLSelectElement,values:{value:string;label:string}[],current?:string) {select.replaceChildren(...values.map(value=>{const option=node('option',value.label);option.value=value.value;return option;}));if(current!==undefined)select.value=current;}
function report(message:string) {$('error').textContent=message;$('error').hidden=false;}
function markDirty(value:boolean) {dirty=value;$('dirty').textContent=value?'Unsaved draft':'Saved';$('dirty').className=`pill ${value?'unsaved':''}`;send('dirty',{dirty:value});if(value&&recipe)send('draftState',{recipeId:recipe.recipe_id,state:{source:$<HTMLTextAreaElement>('source').value,selectedNode,nodePanelDirty,nodePanelBaseline,nodeFields:Object.fromEntries(['node-implementation','node-inputs','node-outputs','node-parameters'].map(id=>[id,$<HTMLTextAreaElement>(id).value]))}});}
function showSection(value:string) {section=value;for(const name of ['Research','Code','Data','Recipes','Runs','Versions','Compute']){$(name).hidden=name!==value;document.querySelector(`[data-section="${name}"]`)?.classList.toggle('active',name===value);}vscode.setState({section,projectId:snapshot.project?.project_id,recipeId:recipe?.recipe_id});}
for(const name of ['Research','Code','Data','Recipes','Runs','Versions','Compute']){const tab=button(name,()=>showSection(name));tab.dataset.section=name;$('tabs').append(tab);}
showSection(section);
for(const kind of kinds){const option=node('option',kind);option.value=kind;$('node-kind').append(option);}
$('connect').onclick=()=>send('configure');$('create-project').onclick=()=>send('createProject');$('refresh').onclick=()=>send('refresh');
$('project').onchange=()=>send('selectProject',{projectId:$<HTMLSelectElement>('project').value});
$('recipe-select').onchange=()=>send('openRecipe',{recipeId:$<HTMLSelectElement>('recipe-select').value});
$('new-recipe').onclick=()=>send('newRecipe');$('native-source').onclick=()=>send('nativeSource');
$('validate').onclick=()=>send('validateRecipe',{source:$<HTMLTextAreaElement>('source').value});
$('diff').onclick=()=>send('diffRecipe',{source:$<HTMLTextAreaElement>('source').value});
$('save').onclick=()=>send('saveRecipe',{source:$<HTMLTextAreaElement>('source').value});
$('reload').onclick=$('discard-conflict').onclick=()=>send('reloadRecipe');
$('open-code').onclick=()=>send('openCode');
$('close-operation').onclick=()=>{$('operation-panel').hidden=true;};$('close-result').onclick=()=>{$('result-panel').hidden=true;};
document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key==='s'&&section==='Recipes'){event.preventDefault();if(!busy&&recipe)send('saveRecipe',{source:$<HTMLTextAreaElement>('source').value});}});
$<HTMLTextAreaElement>('source').addEventListener('input',()=>{
  markDirty(true);
  try{const value=JSON.parse($<HTMLTextAreaElement>('source').value);if(!Array.isArray(value.nodes)||!Array.isArray(value.edges))throw new Error('Graph requires nodes and edges arrays.');recipe=value;$('parse-error').textContent='';renderGraph();if(selectedNode&&!nodePanelDirty)fillNodePanel(selectedNode);}
  catch(error){$('parse-error').textContent=`Source parse: ${(error as Error).message}`;}
});
function updateDraft(value:Recipe):boolean {try{readVisualSource($<HTMLTextAreaElement>('source').value);}catch(error){report((error as Error).message);return false;}recipe=value;$<HTMLTextAreaElement>('source').value=pretty(value);markDirty(true);renderGraph();renderReferences();return true;}
function renderRecipe(value:Recipe|undefined) {$('error').hidden=true;$('error').textContent='';$('notice').hidden=true;loadedRecipe=value;recipe=value;selectedNode=undefined;nodePanelDirty=false;layouts.clear();markDirty(false);$('conflict').hidden=true;$('remote').hidden=true;$('parse-error').textContent='';$<HTMLTextAreaElement>('source').value=value?pretty(value):'';$('identity').textContent=value?`${value.name}  ·  ${value.recipe_id}  ·  revision ${value.revision}\nSHA256 ${value.digest}`:'Select or create a recipe.';renderRecipeChoices();renderGraph();renderReferences();}
function renderReferences() {$('code-info').textContent=pretty({project:snapshot.project?.project_id,source:recipe?.source,python_nodes:recipe?.nodes.filter(n=>n.kind==='code.python')});$('data-info').textContent=pretty({external_dependencies:recipe?.external_dependencies,dataset_nodes:recipe?.nodes.filter(n=>n.kind.startsWith('dataset.'))});$('compute-info').textContent=pretty({resources:recipe?.resources,environment:recipe?.environment,remote_spending:'disabled'});}
function renderRecipeChoices(){const records=recipeChoiceRecords(snapshot.project?.project_id,snapshot.recipes??[],loadedRecipe);options($<HTMLSelectElement>('recipe-select'),[{value:'',label:'Choose a recipe'},...records.map(r=>({value:r.recipe_id,label:`${r.name} · r${r.revision}`}))],recipe?.recipe_id);}
function renderSnapshot() {
  const project=snapshot.project;if(!project)return;
  $('project-id').textContent=`${project.project_id} · r${project.revision}`;
  const select=$<HTMLSelectElement>('project');if(![...select.options].some(o=>o.value===project.project_id)){const opt=node('option',project.model_name);opt.value=project.project_id;select.append(opt);}select.value=project.project_id;
  renderRecipeChoices();
  const records=$('research-records');records.replaceChildren();
  for(const [key,label] of [['evidence','Evidence'],['claims','Claims'],['hypotheses','Hypotheses'],['decisions','Decisions']]){const group=node('section');group.append(node('h3',label));for(const record of snapshot[key]??[]){const details=node('details');details.append(node('summary',record.title??record.proposition??record.proposed_change??`${record.outcome}: ${record.rationale}`));details.append(node('pre',pretty(record)));group.append(details);}if(!(snapshot[key]??[]).length)group.append(node('p','No records yet.','muted'));records.append(group);}
  const runs=$('run-list');runs.replaceChildren();for(const run of snapshot.runs??[]){const card=button(`${run.status??'unknown'} · ${run.operation_id}`,()=>{activeRun=run.operation_id;send('selectRun',{operationId:run.operation_id});});card.className='run-card';card.append(node('small',`Backend: ${run.backend_run_id??'Not yet assigned'}`));runs.append(card);}if(!(snapshot.runs??[]).length)runs.append(node('p','No runs yet. Preview an execution plan first.','muted'));
  renderReferences();
}
const svgNamespace='http://www.w3.org/2000/svg';
function svg(tag:string,attributes:Record<string,string>,text?:string):SVGElement {const element=document.createElementNS(svgNamespace,tag);for(const [key,value]of Object.entries(attributes))element.setAttribute(key,value);if(text!==undefined)element.textContent=text;return element;}
function positions() {if(!recipe)return;const remaining=[...recipe.nodes];const levels=new Map<string,number>();for(let i=0;i<recipe.nodes.length;i++){let progress=false;for(const n of remaining){if(levels.has(n.id))continue;const parents=recipe.edges.filter(e=>e.target_node===n.id).map(e=>e.source_node);if(parents.every(p=>levels.has(p))){levels.set(n.id,parents.length?Math.max(...parents.map(p=>levels.get(p)!))+1:0);progress=true;}}if(!progress)break;}const rows=new Map<number,number>();for(const n of recipe.nodes){const level=levels.get(n.id)??0;const row=rows.get(level)??0;rows.set(level,row+1);if(!layouts.has(n.id))layouts.set(n.id,{x:40+level*280,y:35+row*150});}}
function renderGraph() {
  const layer=document.getElementById('graph-layer')!;layer.replaceChildren();positions();
  layer.setAttribute('transform',`translate(${pan.x} ${pan.y}) scale(${pan.scale})`);
  if(!recipe){layer.append(svg('text',{x:'40',y:'50',class:'graph-label'},'Open a recipe to author its pipeline.'));return;}
  for(const edge of recipe.edges){const from=layouts.get(edge.source_node),to=layouts.get(edge.target_node);if(!from||!to)continue;layer.append(svg('path',{d:`M ${from.x+220} ${from.y+54} C ${from.x+260} ${from.y+54},${to.x-40} ${to.y+54},${to.x} ${to.y+54}`,class:'edge'}));layer.append(svg('text',{x:String((from.x+220+to.x)/2),y:String((from.y+to.y)/2+44),class:'edge-label'},`${edge.source_port} → ${edge.target_port}`));}
  for(const item of recipe.nodes){const pos=layouts.get(item.id)!;const group=svg('g',{transform:`translate(${pos.x} ${pos.y})`,class:`graph-node ${selectedNode===item.id?'selected':''}`,tabindex:'0',role:'button','aria-label':`${item.id}, ${item.kind}`});group.append(svg('rect',{width:'220',height:'112',rx:'8'}));group.append(svg('text',{x:'14',y:'25',class:'node-name'},item.id));group.append(svg('text',{x:'14',y:'45',class:'node-kind'},item.kind));group.append(svg('text',{x:'14',y:'70',class:'node-port'},`in: ${(item.inputs??[]).map(p=>`${p.name}:${p.type}`).join(', ')||'none'}`));group.append(svg('text',{x:'14',y:'91',class:'node-port'},`out: ${(item.outputs??[]).map(p=>`${p.name}:${p.type}`).join(', ')||'none'}`));group.addEventListener('click',()=>selectNode(item.id));group.addEventListener('keydown',event=>{if((event as KeyboardEvent).key==='Enter')selectNode(item.id);});layer.append(group);}
  if(!recipe.nodes.length)layer.append(svg('text',{x:'40',y:'60',class:'graph-label'},'Add a component below to start your pipeline.'));
  const source=$<HTMLSelectElement>('source-node'),target=$<HTMLSelectElement>('target-node');
  const values=recipe.nodes.map(n=>({value:n.id,label:n.id}));options(source,values,source.value);options(target,values,target.value);if(!source.value&&values.length)source.value=values[0].value;if(!target.value&&values.length)target.value=values[Math.min(1,values.length-1)].value;renderPorts();
  const edges=$('edges');edges.replaceChildren();recipe.edges.forEach((edge,index)=>{const row=node('div',undefined,'edge-row');row.append(node('code',`${edge.source_node}.${edge.source_port} → ${edge.target_node}.${edge.target_port}`),button('Remove edge',()=>{if(recipe)updateDraft({...recipe,edges:recipe.edges.filter((_,i)=>i!==index)});}));edges.append(row);});
  $('node-panel').hidden=!selectedNode;
}
function renderPorts() {for(const [nodeId,portId,side]of [['source-node','source-port','outputs'],['target-node','target-port','inputs']]){const id=$<HTMLSelectElement>(nodeId).value;const ports=recipe?.nodes.find(n=>n.id===id)?.[side]??[];options($<HTMLSelectElement>(portId),ports.map((p:RecordValue)=>({value:p.name,label:`${p.name}: ${p.type}`})));}}
$('source-node').onchange=$('target-node').onchange=renderPorts;
$('add-edge').onclick=()=>{if(!recipe)return;const edge:RecipeEdge={source_node:$<HTMLSelectElement>('source-node').value,source_port:$<HTMLSelectElement>('source-port').value,target_node:$<HTMLSelectElement>('target-node').value,target_port:$<HTMLSelectElement>('target-port').value};updateDraft({...recipe,edges:[...recipe.edges,edge]});};
$('add-node').onclick=()=>{if(!recipe)return;const id=$<HTMLInputElement>('node-id').value.trim();if(!id){report('Choose a node ID.');return;}updateDraft({...recipe,nodes:[...recipe.nodes,newNode(id,$<HTMLSelectElement>('node-kind').value,recipe.source?.commit??'')]});selectNode(id);};
function fillNodePanel(id:string){const n=recipe?.nodes.find(item=>item.id===id);if(!n)return;nodePanelBaseline=JSON.stringify(n);nodePanelDirty=false;$('node-title').textContent=`${n.id} · ${n.kind}`;$<HTMLTextAreaElement>('node-implementation').value=pretty(n.implementation_ref);$<HTMLTextAreaElement>('node-inputs').value=pretty(n.inputs);$<HTMLTextAreaElement>('node-outputs').value=pretty(n.outputs);$<HTMLTextAreaElement>('node-parameters').value=pretty(n.parameters);}
function selectNode(id:string){if(nodePanelDirty){report('Apply your panel changes or use Discard panel edits / reload node before selecting a node.');return;}selectedNode=id;renderGraph();fillNodePanel(id);}
for(const id of ['node-implementation','node-inputs','node-outputs','node-parameters'])$(id).addEventListener('input',()=>{nodePanelDirty=true;markDirty(true);});
$('reset-node-panel').onclick=()=>{if(selectedNode){fillNodePanel(selectedNode);markDirty(true);}};
$('apply-node').onclick=()=>{const n=recipe?.nodes.find(n=>n.id===selectedNode);if(!recipe||!n)return;try{readVisualSource($<HTMLTextAreaElement>('source').value);assertNodePanelCurrent(n,nodePanelBaseline);const updated:RecipeNode={...n,implementation_ref:JSON.parse($<HTMLTextAreaElement>('node-implementation').value),inputs:JSON.parse($<HTMLTextAreaElement>('node-inputs').value),outputs:JSON.parse($<HTMLTextAreaElement>('node-outputs').value),parameters:JSON.parse($<HTMLTextAreaElement>('node-parameters').value)};if(updateDraft(replaceNode(recipe,updated))){fillNodePanel(updated.id);markDirty(true);}}catch(error){report(`Node JSON: ${(error as Error).message}`);}};
$('delete-node').onclick=()=>{if(recipe&&selectedNode){if(nodePanelDirty){report('Apply or discard panel edits before removing the node.');return;}const next=removeNode(recipe,selectedNode);if(updateDraft(next)){selectedNode=undefined;renderGraph();}}};
$('zoom-in').onclick=()=>{pan.scale=Math.min(3,pan.scale*1.2);renderGraph();};$('zoom-out').onclick=()=>{pan.scale=Math.max(.25,pan.scale/1.2);renderGraph();};$('fit').onclick=()=>{pan={x:0,y:0,scale:1};renderGraph();};
const graph=document.getElementById('graph')!;let dragging:{x:number;y:number}|undefined;
graph.addEventListener('wheel',(event)=>{const e=event as WheelEvent;e.preventDefault();pan.scale=Math.max(.25,Math.min(3,pan.scale*(e.deltaY>0?.9:1.1)));renderGraph();},{passive:false});
graph.addEventListener('pointerdown',event=>{const e=event as PointerEvent;if((e.target as Element).closest('.graph-node'))return;dragging={x:e.clientX,y:e.clientY};(graph as unknown as SVGSVGElement).setPointerCapture(e.pointerId);});
graph.addEventListener('pointermove',event=>{if(!dragging)return;const e=event as PointerEvent;const factor=900/graph.getBoundingClientRect().width;pan.x+=(e.clientX-dragging.x)*factor;pan.y+=(e.clientY-dragging.y)*factor;dragging={x:e.clientX,y:e.clientY};renderGraph();});
graph.addEventListener('pointerup',()=>{dragging=undefined;});graph.addEventListener('pointercancel',()=>{dragging=undefined;});

function operationDefaults():RecordValue{return {recipe_id:recipe?.recipe_id,expected_revision:recipe?.revision,recipe_revision:recipe?.revision,operation_id:activeRun,target:'local-cpu',trusted:true,mode:'exact_state',acceptance_metric:{metric:'validation_loss',direction:'minimize'},selection_rule:{metric:'validation_loss',direction:'minimize'},outcome:'inconclusive',evidence_ids:[],claim_ids:[],run_ids:activeRun?[activeRun]:[],changes:{}};}
function openOperation(name:string){
  const spec=catalog.find(o=>o.name===name);if(!spec){report(`Operation unavailable: ${name}`);return;}selectedOperation=spec;
  $('operation-panel').hidden=false;$('operation-title').textContent=name;$('operation-description').textContent=`${spec.description} Required scope: ${spec.scope}.`;
  const form=$<HTMLFormElement>('operation-form');form.replaceChildren();const defaults=operationDefaults();
  for(const [key,schema]of Object.entries(spec.input_schema.properties)){
    if(['project_id','idempotency_key','request_id'].includes(key)||(name==='run.submit'&&['recipe_id','expected_revision','trusted','target'].includes(key)))continue;
    const required=spec.input_schema.required.includes(key);const label=node('label',`${key.replaceAll('_',' ')}${required?' *':''}`);let input:HTMLInputElement|HTMLTextAreaElement|HTMLSelectElement;
    const types=Array.isArray(schema.type)?schema.type:[schema.type];
    if(schema.enum){input=node('select');options(input,schema.enum.map((value:string)=>({value,label:value})));}
    else if(types.includes('object')||types.includes('array')){input=node('textarea');input.rows=3;input.spellcheck=false;input.dataset.json='true';}
    else{input=node('input');input.type=types.includes('boolean')?'checkbox':types.includes('integer')||types.includes('number')?'number':'text';}
    input.name=key;input.id=`op-${key}`;input.required=required;
    if(defaults[key]!==undefined){if(input instanceof HTMLInputElement&&input.type==='checkbox')input.checked=defaults[key];else input.value=input.dataset.json?pretty(defaults[key]):String(defaults[key]);}
    label.append(input);form.append(label);
  }
  const submit=node('button',spec.mutating?'Submit operation':'Read / preview','primary');submit.type='submit';form.append(submit);
  form.onsubmit=event=>{event.preventDefault();const payload:RecordValue={};try{for(const input of Array.from(form.elements)){if(!(input instanceof HTMLInputElement||input instanceof HTMLTextAreaElement||input instanceof HTMLSelectElement)||!input.name)continue;if(input instanceof HTMLInputElement&&input.type==='checkbox')payload[input.name]=input.checked;else if(input.value!=='')payload[input.name]=input.dataset.json?JSON.parse(input.value):(input instanceof HTMLInputElement&&input.type==='number'?Number(input.value):input.value);}send('operation',{operation:name,payload});}catch(error){report(`Form JSON: ${(error as Error).message}`);}};
  $('operation-panel').scrollIntoView({block:'nearest'});
}
function buildActions(){
  const groups:Record<string,[string,string][]>={
    'research-actions':[['evidence.add','Add evidence'],['evidence.search','Search saved evidence'],['claim.create','Add claim'],['claim.update','Edit claim'],['hypothesis.create','New hypothesis'],['hypothesis.update','Edit hypothesis'],['decision.record','Record decision']],
    'run-actions':[['run.plan','Preview run'],['run.submit','Start local fixture'],['run.cancel','Request stop'],['run.resume','Resume checkpoint'],['run.compare','Compare runs']],
    'version-actions':[['model.version.list','Load versions'],['model.version.get','Version details'],['model.version.lineage','Inspect lineage'],['model.version.promote','Promote by decision'],['model.checkpoint.get','Inspect checkpoint']],
    'compute-actions':[['execution.targets','Inspect targets'],['run.plan','Preview recipe requirements'],['policy.get','Read policy'],['cost.estimate','Inspect cost availability']]
  };
  for(const [id,actions]of Object.entries(groups)){$(id).replaceChildren(...actions.filter(([name])=>catalog.some(o=>o.name===name)).map(([name,label])=>button(label,()=>openOperation(name))));}
  const recipeActions=node('div',undefined,'toolbar');recipeActions.id='recipe-extra-actions';for(const [name,label]of [['recipe.clone','Clone recipe'],['recipe.export','Export bundle'],['recipe.import','Import bundle'],['recipe.resolve_dependencies','Inspect dependencies']])if(catalog.some(o=>o.name===name))recipeActions.append(button(label,()=>openOperation(name)));$('recipe-extra-actions')?.remove();$('Recipes').append(recipeActions);
}
window.addEventListener('message',event=>{
  const message=event.data;if(!message||typeof message.type!=='string')return;
  switch(message.type){
    case 'init':catalog=message.catalog;options($<HTMLSelectElement>('project'),[{value:'',label:'Select project'},...message.projects.map((p:RecordValue)=>({value:p.project_id,label:p.model_name}))],snapshot.project?.project_id);$('connection').textContent=message.trusted?'Service connected':'Read/author mode · Workspace untrusted';buildActions();break;
    case 'project':snapshot=message.snapshot;renderSnapshot();showSection(message.section??'Recipes');if(message.runId)activeRun=message.runId;break;
    case 'snapshot':snapshot=message.snapshot;renderSnapshot();break;
    case 'recipe':renderRecipe(message.recipe??undefined);break;
    case 'savedRetained':loadedRecipe=message.recipe;renderRecipeChoices();$('identity').textContent=`${message.recipe.name} · ${message.recipe.recipe_id} · saved revision ${message.recipe.revision}\nSHA256 ${message.recipe.digest}`;$('notice').hidden=false;$('notice').textContent='Saved the submitted revision. Your newer edits are still unsaved and have been preserved.';markDirty(true);break;
    case 'restoreDraft':{const state=message.state;$<HTMLTextAreaElement>('source').value=state.source;try{recipe=readVisualSource(state.source);$('parse-error').textContent='';}catch(error){$('parse-error').textContent=(error as Error).message;}selectedNode=state.selectedNode;nodePanelDirty=Boolean(state.nodePanelDirty);nodePanelBaseline=state.nodePanelBaseline??'';renderGraph();if(selectedNode){fillNodePanel(selectedNode);nodePanelDirty=Boolean(state.nodePanelDirty);if(nodePanelDirty){nodePanelBaseline=state.nodePanelBaseline??nodePanelBaseline;for(const [id,value]of Object.entries(state.nodeFields??{}))if(['node-implementation','node-inputs','node-outputs','node-parameters'].includes(id))$<HTMLTextAreaElement>(id).value=String(value);}}markDirty(true);$('notice').hidden=false;$('notice').textContent='Recovered your unsaved recipe draft. The original base revision is retained for conflict checking.';break;}
    case 'busy':busy=message.busy;for(const control of document.querySelectorAll<HTMLButtonElement|HTMLInputElement|HTMLTextAreaElement|HTMLSelectElement>('button,input,textarea,select'))control.disabled=busy;break;
    case 'error':report(`${message.code?message.code+': ':''}${message.message}${message.details?'\n'+pretty(message.details):''}`);break;
    case 'connection':$('connection').textContent=message.message;break;
    case 'remoteChanged':$('remote').hidden=false;$('remote').textContent=`Another client saved revision ${message.revision}. Your unsaved draft is preserved. Use Diff, then Reload to discard or Save to inspect the conflict.`;break;
    case 'conflict':$('conflict').hidden=false;$('conflict-diff').textContent=pretty(message.diff);$('server-json').textContent=pretty(message.server);$('local-json').textContent=pretty(message.local);$('conflict').scrollIntoView({block:'nearest'});break;
    case 'result':$('result-panel').hidden=false;$('result-title').textContent=message.operation;$('result').textContent=pretty(message.result);if(message.operation==='model.version.list'){$('versions').replaceChildren(node('pre',pretty(message.result)));}if(['execution.targets','cost.estimate','run.plan','policy.get'].includes(message.operation))$('compute-info').textContent=pretty(message.result);break;
    case 'run':activeRun=message.run.operation_id;$('run-summary').replaceChildren(node('h3',`Operation ${activeRun}`),node('pre',pretty(message.run.status)));$('run-logs').textContent=pretty(message.run.logs);$('run-artifacts').textContent=pretty(message.run.artifacts);break;
  }
});
send('ready');
