import { ExecutionSettingsEditor } from './executionSettings';
import { IterationEditor, iterationNodeLabel, iterationRunSummary } from './iterations';
import { RecipeHistoryView } from './recipeHistory';
import { renderComputeDiagnostics } from './computeDiagnostics';
import { addLibraryComponent, renderComponentLibrary } from './componentLibrary';
import { ReleaseView } from './releaseView';
import { renderScientificWorkflow } from './scientificWorkflow';
import { conditionChoices, conditionEdges, conditionSelection, withTerminalCondition, conditionalRunSummary } from './conditions';
import { renderCostDiagnostics } from './costDiagnostics';
import { ParameterControls, parameterSchema } from './parameterControls';
import { bindRecipeSourcePane } from './sourcePane';
import { renderSourceStatus } from './sourceStatus';
import { BusyControls } from './busyControls';
import { renderLiveEvaluations } from './liveEvaluations';
import { DEFAULT_NOTIFICATION_PREFERENCES, NOTIFICATION_BOOLEAN_KEYS, notificationPreferences } from './notifications';
import { operationDefaults } from './operationDefaults';
import { liveEvaluationSettings, withLiveEvaluationSettings, liveSettingsIdentity, assertLiveSettingsCurrent } from './liveRecipe';
import { executableKindChoices, admissionMessages } from './admission';
import { renderResearchContextResult } from './researchContext';
import { renderResearchWorkflow, renderResearchTextResult, appendResearchSourceActions, ResearchCollections } from './researchView';
import { selectedCitations } from './citations';
import { renderDatasets, renderDatasetPreview, createDatasetManifestEditor } from './datasetView';
import { ComponentAuthoringControls, componentDraftProblems } from './componentAuthoring';
import { addNode, removeNode, replaceNode, readVisualSource, assertNodePanelCurrent } from './document';
import { OperationSpec, Recipe, RecipeEdge, RecipeNode, RecordValue } from './types';
declare function acquireVsCodeApi(): {postMessage(message:unknown):void;setState(value:unknown):void;getState():any};
const vscode=acquireVsCodeApi();
const $=<T extends HTMLElement=HTMLElement>(id:string)=>document.getElementById(id) as T;
const send=(type:string,body:RecordValue={})=>{if(type==='saveRecipe'&&executionEditor.isDirty()){report('Apply or discard stage execution settings before saving the recipe.');return;}if(type==='saveRecipe'&&liveSettingsDirty){report('Apply or discard live evaluation settings before saving the recipe.');return;}if(type==='saveRecipe'&&nodePanelDirty){report('Apply or explicitly discard node panel changes before saving the recipe.');return;}vscode.postMessage({type,...body});};
const pretty=(value:unknown)=>JSON.stringify(value,null,2);
const app=$('app');
const busyControls=new BusyControls(document);
app.innerHTML=`
<header><div><span class="eyebrow">MODELBUILDER</span><h1 id="workbench-heading">Model projects</h1></div><div class="connection"><span id="connection" role="status">Connecting…</span><button id="connect">Connection</button></div></header>
<div id="notice" role="status" hidden></div><div id="error" role="alert" hidden></div>
<section id="landing" aria-label="Choose a model project">
  <div class="landing-intro"><span class="eyebrow">START WITH A MODEL</span><h2>What model are you building?</h2><p>Open a model project or create one. Each project develops one model through one evolving recipe, with its research, code, data, runs and versions together.</p><div class="toolbar"><button id="create-project" class="primary">Create model project</button><button id="refresh-projects">Refresh projects</button></div></div>
  <h3>Your model projects</h3><div id="project-list" class="project-cards"></div>
</section>
<div id="workspace" class="model-workspace" hidden>
<aside class="workspace-nav"><span class="eyebrow">MODEL PROJECT</span><h2 id="model-name"></h2><div id="project-id" class="mono muted"></div><nav aria-label="Model lifecycle" id="tabs"></nav><button id="close-project">Close project</button></aside>
<main class="workspace-content">
<section id="Recipe" class="page">
  <div class="toolbar"><h2>Training &amp; evaluation recipe</h2><button id="native-source">Open native JSON</button><button id="validate">Validate</button><button id="diff">Diff</button><button id="save" class="primary">Save revision</button><button id="reload">Reload</button><span id="dirty" class="pill">Saved</span></div>
  <p class="muted">Build the dataset, initialization, training, evaluation and model-export pipeline below. Saved revisions and runs stay with this model.</p><div id="recipe-extra-actions" class="toolbar"></div><div id="identity" class="identity">Loading the model recipe…</div>
  <div id="recipe-history"></div>
  <section id="component-library" hidden></section>
  <div id="migration" class="warning" hidden></div><div id="remote" class="warning" hidden></div>
  <div id="conflict" class="conflict" hidden><h2>Concurrent edit conflict</h2><p>Your draft is preserved. Compare it to the current server revision before resolving.</p><pre id="conflict-diff"></pre><div class="columns"><div><h3>Current server</h3><pre id="server-json"></pre></div><div><h3>Your draft</h3><pre id="local-json"></pre></div></div><button id="discard-conflict">Load server revision and discard draft</button></div>
  <section id="execution-settings-editor"></section>
  <details class="run-settings" id="live-recipe-settings"><summary>Evaluate checkpoints during training</summary><p class="muted">Explicitly enable bounded checkpoint evaluation for this recipe. Connect the training checkpoint output to one evaluation component. Results appear in Runs while training continues. No external tracking is required.</p>
<form id="live-recipe-form"><div class="notification-toggles"><label><input id="live-enabled" type="checkbox"> Evaluate each published checkpoint</label></div><div class="notification-options"><label>Concurrent evaluations<input id="live-concurrency" type="number" min="1" max="4" value="1" required></label><label>Evaluation timeout (seconds)<input id="live-timeout" type="number" min="1" max="3600" value="300" required></label><div id="live-cadences"></div></div><p id="live-cadence-help" class="muted"></p><button type="submit">Apply to recipe draft</button><button type="button" id="reset-live-settings">Discard settings edits</button><span class="muted">Save the recipe revision before running.</span></form></details>
  <section id="iteration-editor"></section>
  <div id="recipe-editor" class="editor-grid">
    <div class="graph-pane"><div class="toolbar"><h2>Pipeline graph</h2><button id="toggle-source" type="button" aria-expanded="true" aria-controls="recipe-source-pane">Hide JSON</button><button id="zoom-in" aria-label="Zoom in">+</button><button id="zoom-out" aria-label="Zoom out">−</button><button id="fit">Reset view</button><span class="muted">Drag to pan · Wheel to zoom</span></div><svg id="graph" role="img" aria-label="Editable typed pipeline graph" viewBox="0 0 900 440"><g id="graph-layer"></g></svg>
      <div class="toolbar"><label for="node-kind">Component</label><select id="node-kind"></select><input id="node-id" placeholder="Unique node ID" aria-label="New node ID"><button id="add-node">Add node</button></div>
      <details id="edge-form"><summary>Add a typed edge</summary><div class="edge-controls"><select id="source-node" aria-label="Source node"></select><select id="source-port" aria-label="Source output port"></select><span>→</span><select id="target-node" aria-label="Target node"></select><select id="target-port" aria-label="Target input port"></select><button id="add-edge">Connect</button></div><p class="muted">The service checks type compatibility and cycles when validating or saving.</p></details>
      <div id="edges"></div>
      <section id="node-panel" hidden><div class="toolbar"><h3 id="node-title"></h3><button id="delete-node" class="danger">Remove node</button></div><section id="node-component-fields"></section><label id="node-implementation-field">Implementation reference<textarea id="node-implementation" rows="3" spellcheck="false"></textarea></label><div class="columns"><label>Typed inputs (JSON)<textarea id="node-inputs" rows="4" spellcheck="false"></textarea></label><label>Typed outputs (JSON)<textarea id="node-outputs" rows="4" spellcheck="false"></textarea></label></div><fieldset><legend>Branch condition</legend><label>Boolean gate<select id="node-condition-source" aria-label="Branch condition gate"></select></label><label>Run when gate equals<select id="node-condition-equals" aria-label="Branch condition value"><option value="true">true</option><option value="false">false</option></select></label><p class="muted">Select the gate explicitly when running a subgraph. False gates skip this stage and required-input descendants. Set join inputs to required:false to accept surviving branches; Python joins need defaults for omitted inputs. Skips produce no artifacts. Live evaluator gates must precede their trainer. Repeat stages through explicit recipe-level bounded iteration blocks.</p></fieldset><h4>Component parameters</h4><div id="node-typed-parameters"></div><details id="node-parameter-json"><summary>Parameter JSON escape hatch</summary><label>Canonical parameters (JSON)<textarea id="node-parameters" rows="6" spellcheck="false"></textarea></label></details><button id="apply-node">Apply node changes to draft</button><button id="reset-node-panel">Discard panel edits / reload node</button></section>
    </div>
    <div id="recipe-source-pane" class="source-pane"><div class="toolbar"><h2>Canonical recipe JSON</h2></div><label class="sr-only" for="source">Canonical recipe JSON source</label><textarea id="source" spellcheck="false" aria-describedby="source-help"></textarea><p id="source-help" class="muted">Graph and JSON share this draft. Saving calls the same revision-checked operation as MCP. Ctrl/Cmd+S saves the recipe.</p><span id="parse-error" role="alert"></span></div>
  </div>
</section>
<section id="Research" class="page" hidden><h2>Research, evidence and decisions</h2><p>Keep sources, propositions and measurable hypotheses separate. Preserve negative and inconclusive results.</p><div id="research-actions" class="toolbar"></div><div id="research-workflow-actions" class="toolbar"></div><p class="muted">Keep citations, source passages and claims distinct. Stage a proposed recipe change, review its exact diff, then explicitly apply an approved revision. Applying a proposal never starts training.</p><div id="research-workflow"></div><section id="research-reader" hidden aria-label="Local paper reader"></section><h3>Evidence, hypotheses and decisions</h3><div id="research-records" class="cards"></div></section>
<section id="Code" class="page" hidden><h2>Architecture and code</h2><p>Python is a first-class node with an explicit entrypoint and typed inputs/outputs. Edit arbitrary model code in the native editor.</p><div class="toolbar"><button id="open-code">Open source in native Explorer</button><button id="setup-assistant">Set up optional coding tool…</button></div><p class="muted">Codex and Claude Code are optional, independent tools. Setup reviews exact extension and MCP changes before applying them. It never runs paid inference or copies the IDE credential.</p><section id="source-binding" aria-label="Project source binding"></section><pre id="code-info"></pre></section>
<section id="Data" class="page" hidden><h2>Datasets, versions and splits</h2><p>Register pinned CSV/JSONL manifests using operator-approved file aliases. Dataset contents stay at their source. Immutable revisions preserve sample/split identities, rights and transformation provenance.</p><div id="dataset-actions" class="toolbar"></div><p id="dataset-error" class="warning" hidden></p><div id="dataset-records"></div><section id="dataset-preview" hidden></section><details><summary>Recipe dataset bindings</summary><p class="muted">Use a split to add or update its dataset import node in this model’s recipe. Choose an existing dict input as the optional target to connect it. The reviewed action pins the version, split, rights and source hashes without executing code or uploading data. Registration and preview alone are not proof of model consumption.</p><pre id="data-info"></pre></details></section>
<section id="Runs" class="page" hidden><h2>Runs and evaluations</h2><div class="toolbar" id="run-actions"></div>
<details class="run-settings"><summary>Evaluation notifications</summary><p class="muted">Native IDE notifications for the open model project, including when the workbench is hidden. Historical results are silent on first connection. Notifications are grouped to avoid repeated alerts; nothing is sent outside this IDE.</p>
<form id="notification-form"><div class="notification-toggles">
<label><input id="notify-enabled" type="checkbox"> Enable IDE notifications</label>
<label><input id="notify-results" type="checkbox"> Completed results</label>
<label><input id="notify-improvements" type="checkbox"> Verified improvements</label>
<label><input id="notify-regressions" type="checkbox"> Verified regressions</label>
<label><input id="notify-failures" type="checkbox"> Failed evaluations</label>
<label><input id="notify-trainingFinished" type="checkbox"> Training finished</label><label><input id="notify-trainingFailed" type="checkbox"> Training failed</label><label><input id="notify-trainingCanceled" type="checkbox"> Confirmed cancellation</label></div>
<div class="notification-options"><label>Metric filter (blank means all)<input id="notify-metric" maxlength="160"></label><label>Minimum absolute metric change<input id="notify-delta" type="number" min="0" step="any" value="0"></label><label>Minimum seconds between notifications<input id="notify-interval" type="number" min="10" max="3600" value="30"></label></div><button type="submit">Save notification preferences</button><span id="notification-saved" role="status"></span></form></details>
<details class="run-settings"><summary>Optional Weights &amp; Biases tracking</summary><p class="muted">Off by default. Offline writes local tracking files. Online sends only allowlisted metrics and sanitized run identity to the explicit W&amp;B destination, with confirmation for each run. Source code, datasets and checkpoints are never uploaded.</p>
<form id="tracking-form"><div class="notification-options"><label>Tracking mode<select id="tracking-mode"><option value="disabled">Disabled</option><option value="offline">Offline only</option><option value="online">Online, confirm each run</option></select></label><label>W&amp;B entity<input id="tracking-entity" autocomplete="off"></label><label>W&amp;B project<input id="tracking-project" autocomplete="off"></label><label>Metric allowlist (comma separated)<input id="tracking-metrics" placeholder="validation_loss, accuracy"></label><label>Artifact policy<select id="tracking-artifacts"><option value="none">No artifacts</option><option value="metrics_snapshot">Sanitized metrics JSON only</option></select></label></div><p class="muted">Online credentials must be configured by the service operator in MODELBUILDER_WANDB_API_KEY. Never paste a credential here.</p><button type="submit">Save project tracking defaults</button><span id="tracking-saved" role="status"></span></form></details>
<section id="run-admission" class="warning" hidden aria-label="Execution plan diagnostics"></section><div class="run-grid"><div id="run-list" class="cards"></div><div><div id="run-summary"></div><div id="tracking-status" class="muted"></div><section id="live-evaluations" aria-label="Live checkpoint evaluations"></section><p class="warning">A cancellation request is not confirmation that the worker has stopped. Only the reconciled backend status confirms termination.</p><h3>Backend logs</h3><pre id="run-logs">Select a run.</pre><h3>Artifacts and checkpoints</h3><div id="run-export-actions"></div><pre id="run-artifacts">No run selected.</pre></div></div><section id="scientific-workflows" aria-label="Scientific workflow actions"></section><section id="run-comparison" aria-label="Compare runs and record decisions"></section></section>
<section id="Versions" class="page" hidden><h2>Model versions and lineage</h2><div class="toolbar" id="version-actions"></div><div id="versions" class="cards"></div></section>
<section id="Compute" class="page" hidden><h2>Local compute preview</h2><p>Inspect actual target capabilities and recipe requirements. Remote spending is disabled. No remote budget enforcement is claimed.</p><div class="toolbar" id="compute-actions"></div><button id="refresh-costs" type="button">Refresh cost ledger diagnostics</button><section id="cost-diagnostics" aria-label="Read-only cost diagnostics"></section><section id="compute-diagnostics" aria-label="Service-host hardware and utilization"></section><details><summary>Recipe requirements / raw service result</summary><pre id="compute-info"></pre></details></section>
<section id="operation-panel" hidden><div class="toolbar"><h2 id="operation-title"></h2><button id="close-operation">Close form</button></div><p id="operation-description"></p><form id="operation-form"></form></section>
<section id="result-panel" hidden><div class="toolbar"><h2 id="result-title">Service result</h2><button id="close-result">Close result</button></div><pre id="result"></pre></section>
</main></div>
<footer>Shared operation service · Native MCP parity · No assistant required</footer>`;
bindRecipeSourcePane($('recipe-editor'), $('recipe-source-pane'), $<HTMLButtonElement>('toggle-source'));
const recipeHistory = new RecipeHistoryView($('recipe-history'), send);
let savedRecipe:Recipe|undefined;
let catalog:OperationSpec[]=[];
let snapshot:RecordValue={};
let research:ResearchCollections={};
let datasets:RecordValue[]=[];
let datasetSchema:RecordValue={};
let recipe:Recipe|undefined;
let selectedNode:string|undefined;
let nodePanelDirty=false;
let parameterControls:ParameterControls|undefined;
let componentControls:ComponentAuthoringControls|undefined;
let liveSettingsDirty=false;
let liveSettingsBaseline='';
let nodePanelBaseline="";
let activeRun:string|undefined;
let section='Recipe';
let dirty=false;
let busy=false;
let pan={x:0,y:0,scale:1};
let selectedOperation:OperationSpec|undefined;
const layouts=new Map<string,{x:number;y:number}>();
let kinds=executableKindChoices();
function node<K extends keyof HTMLElementTagNameMap>(tag:K,text?:string,className?:string):HTMLElementTagNameMap[K] {const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;}
function button(label:string,action:()=>void):HTMLButtonElement {const el=node('button',label);el.addEventListener('click',action);return el;}
function options(select:HTMLSelectElement,values:{value:string;label:string}[],current?:string) {select.replaceChildren(...values.map(value=>{const option=node('option',value.label);option.value=value.value;return option;}));if(current!==undefined)select.value=current;}
function report(message:string) {$('error').textContent=message;$('error').hidden=false;}
function markDirty(value:boolean) {dirty=value;$('dirty').textContent=value?'Unsaved draft':'Saved';$('dirty').className=`pill ${value?'unsaved':''}`;send('dirty',{dirty:value});if(value&&recipe)send('draftState',{recipeId:recipe.recipe_id,state:{source:$<HTMLTextAreaElement>('source').value,selectedNode,nodePanelDirty,nodePanelBaseline,parameterDraft:parameterControls?.capture(),componentDraft:componentControls?.capture(),iterationDraft:iterationEditor.capture(),executionSettingsDraft:executionEditor.capture(),liveSettingsDirty,liveSettingsBaseline,liveFields:liveFormValues(),nodeFields:Object.fromEntries(['node-implementation','node-inputs','node-outputs','node-parameters','node-condition-source','node-condition-equals'].map(id=>[id,$<HTMLTextAreaElement>(id).value]))}});}
function showSection(value:string) {section=value;for(const name of ['Research','Code','Data','Recipe','Runs','Versions','Compute']){$(name).hidden=name!==value;document.querySelector(`[data-section="${name}"]`)?.classList.toggle('active',name===value);}vscode.setState({section,projectId:snapshot.project?.project_id,recipeId:recipe?.recipe_id});}
for(const name of ['Research','Code','Data','Recipe','Runs','Versions','Compute']){const tab=button(name,()=>showSection(name));tab.dataset.section=name;$('tabs').append(tab);}
showSection(section);
for(const kind of kinds){const option=node('option',kind);option.value=kind;$('node-kind').append(option);}
$('connect').onclick=()=>send('configure');$('create-project').onclick=()=>send('createProject');$('refresh-projects').onclick=()=>send('refresh');
$('close-project').onclick=()=>send('closeProject');$('native-source').onclick=()=>send('nativeSource');
$('validate').onclick=()=>send('validateRecipe',{source:$<HTMLTextAreaElement>('source').value});
$('diff').onclick=()=>send('diffRecipe',{source:$<HTMLTextAreaElement>('source').value});
$('save').onclick=()=>send('saveRecipe',{source:$<HTMLTextAreaElement>('source').value});
$('reload').onclick=$('discard-conflict').onclick=()=>send('reloadRecipe');
$('refresh-costs').onclick=()=>send('refreshCosts');
$('open-code').onclick=()=>send('openCode');$('setup-assistant').onclick=()=>send('setupAssistant');
$('close-operation').onclick=()=>{$('operation-panel').hidden=true;};$('close-result').onclick=()=>{$('result-panel').hidden=true;};
document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key==='s'&&section==='Recipe'){event.preventDefault();if(!busy&&recipe)send('saveRecipe',{source:$<HTMLTextAreaElement>('source').value});}});
$<HTMLTextAreaElement>('source').addEventListener('input',()=>{
  markDirty(true);
  try{const value=JSON.parse($<HTMLTextAreaElement>('source').value);if(!Array.isArray(value.nodes)||!Array.isArray(value.edges))throw new Error('Graph requires nodes and edges arrays.');recipe=value;$('parse-error').textContent='';renderGraph();renderReferences();if(selectedNode&&!nodePanelDirty)fillNodePanel(selectedNode);}
  catch(error){$('parse-error').textContent=`Source parse: ${(error as Error).message}`;}
});
function updateDraft(value:Recipe):boolean {try{readVisualSource($<HTMLTextAreaElement>('source').value);}catch(error){report((error as Error).message);return false;}recipe=value;$<HTMLTextAreaElement>('source').value=pretty(value);markDirty(true);renderGraph();renderReferences();return true;}
function renderRecipe(value:Recipe|undefined) {executionEditor.reset(value);iterationEditor.reset(value);savedRecipe=value;recipeHistory.update(value,catalog);$('error').hidden=true;$('error').textContent='';$('notice').hidden=true;recipe=value;selectedNode=undefined;nodePanelDirty=false;liveSettingsDirty=false;layouts.clear();markDirty(false);$('conflict').hidden=true;$('remote').hidden=true;$('parse-error').textContent='';$<HTMLTextAreaElement>('source').value=value?pretty(value):'';$('identity').textContent=value?`${value.name}  ·  ${value.recipe_id}  ·  revision ${value.revision}\nSHA256 ${value.digest}`:'Loading the model recipe…';renderGraph();renderReferences();renderScientificPanel();}
const executionEditor=new ExecutionSettingsEditor($('execution-settings-editor'),()=>readVisualSource($<HTMLTextAreaElement>('source').value),updateDraft,()=>markDirty(true),report);
const iterationEditor=new IterationEditor($('iteration-editor'),()=>readVisualSource($<HTMLTextAreaElement>('source').value),updateDraft,()=>markDirty(true),report);
function renderReferences() {executionEditor.update(recipe);iterationEditor.update(recipe);renderSourceBinding();renderLiveRecipeSettings();$('code-info').textContent=pretty({project:snapshot.project?.project_id,source:recipe?.source,python_nodes:recipe?.nodes.filter(n=>n.kind==='code.python')});$('data-info').textContent=pretty({external_dependencies:recipe?.external_dependencies,dataset_nodes:recipe?.nodes.filter(n=>n.kind.startsWith('dataset.'))});$('compute-info').textContent=pretty({resources:recipe?.resources,environment:recipe?.environment,execution:recipe?.execution??{max_parallel_stages:1},remote_spending:'disabled'});}
function renderProjects(projects:RecordValue[]) {
  const list=$('project-list');list.replaceChildren();
  for(const project of projects){const card=node('article',undefined,'project-card');card.append(node('h3',project.model_name));if(project.description)card.append(node('p',project.description,'muted'));card.append(node('p',`Project ${project.slug??project.project_id}`,'muted'));const open=button('Open model project',()=>send('selectProject',{projectId:project.project_id}));open.dataset.projectId=project.project_id;card.append(open);list.append(card);}
  if(!projects.length)list.append(node('p','No model projects yet. Create one to begin.','muted'));
}
function showLanding(){executionEditor.capabilities();releaseView.reset();$('compute-diagnostics').replaceChildren();datasets=[];renderDatasetRecords();$('dataset-error').hidden=true;$('dataset-error').textContent='';$('dataset-preview').hidden=true;$('dataset-preview').replaceChildren();research={};renderResearch();$('research-reader').replaceChildren();$('research-reader').hidden=true;trackingDirty=false;$('run-admission').replaceChildren();$('run-admission').hidden=true;snapshot={};activeRun=undefined;resetRun();renderRecipe(undefined);$('workspace').hidden=true;$('landing').hidden=false;$('workbench-heading').textContent='Model projects';$('operation-panel').hidden=true;$('result-panel').hidden=true;vscode.setState({});}
function renderSourceBinding(){
  const root=$('source-binding'),status=snapshot.source;
  const canPinGit=renderSourceStatus(root,status,recipe?.source?.commit);
  if(canPinGit&&['recipe.source.pin.preview','recipe.source.pin'].every(name=>catalog.some(item=>item.name===name))){
    const pin=button('Review and pin source files…',()=>{
      if(busy)return;if(dirty||nodePanelDirty||liveSettingsDirty||executionEditor.isDirty()){report('Save or explicitly discard the recipe draft before pinning source. Your edits are preserved.');return;}
      if(!recipe||!snapshot.project||!status.binding)return;
      send('pinRecipeSource',{projectId:snapshot.project.project_id,recipeId:recipe.recipe_id,expectedRevision:recipe.revision,expectedProjectRevision:snapshot.project.revision,sourceRepositoryId:status.binding.repository_id});
    });pin.id='pin-source';pin.disabled=!status.bound||!status.available||!recipe;
    root.append(pin,node('p','Choose files in the bound native workspace and an explicit full Git commit, then review their paths, hashes and next recipe revision. Required implementation and environment lock files are included in that review. Pinning does not commit Git changes or execute code.','muted'));
  }
}
const releaseView=new ReleaseView($('run-comparison'),$('versions'),$('run-export-actions'),send,report);
let scientificPanelKey='';
function renderScientificPanel(){
  const project=snapshot.project,root=$('scientific-workflows');
  if(!project||!savedRecipe||savedRecipe.project_id!==project.project_id){scientificPanelKey='';root.replaceChildren();return;}
  const authorized=catalog.filter(item=>item.authorized!==false).map(item=>item.name);
  const key=JSON.stringify([project.project_id,project.revision,savedRecipe.recipe_id,savedRecipe.revision,savedRecipe.digest,authorized]);
  if(key===scientificPanelKey)return;scientificPanelKey=key;
  renderScientificWorkflow(root,{project,recipe:savedRecipe,runs:snapshot.runs??[],authorizedOperations:authorized},(operation,payload)=>send('operation',{operation,payload}));
}
function renderSnapshot() {renderSourceBinding();renderScientificPanel();
  const project=snapshot.project;if(!project)return;
  $('landing').hidden=true;$('workspace').hidden=false;$('workbench-heading').textContent=project.model_name;
  $('model-name').textContent=project.model_name;$('project-id').textContent=project.project_id;
  $('migration').hidden=!project.recipe_migration?.review_required;$('migration').textContent=project.recipe_migration?.review_required?'This project was migrated from multiple recipe identities. Review the current recipe and save a revision before execution; previous recipe manifests and run references remain preserved.':'';
  const records=$('research-records');records.replaceChildren();
  for(const [key,label] of [['evidence','Evidence'],['claims','Claims'],['hypotheses','Hypotheses'],['decisions','Decisions']]){const group=node('section');group.append(node('h3',label));for(const record of snapshot[key]??[]){const details=node('details');details.append(node('summary',record.title??record.proposition??record.proposed_change??`${record.outcome}: ${record.rationale}`));details.append(node('pre',pretty(record)));if(key==='evidence')appendResearchSourceActions(details,record,new Set(catalog.map(item=>item.name)),openOperation);group.append(details);}if(!(snapshot[key]??[]).length)group.append(node('p','No records yet.','muted'));records.append(group);}
  releaseView.update(snapshot,catalog);
  renderTrackingDefaults();
  const runs=$('run-list');runs.replaceChildren();for(const run of snapshot.runs??[]){const card=button(`${run.status??'unknown'} · ${run.operation_id}`,()=>{activeRun=run.operation_id;resetRun();send('selectRun',{operationId:run.operation_id});});card.className='run-card';card.append(node('small',`Backend: ${run.backend_run_id??'Not yet assigned'}`));runs.append(card);}if(!(snapshot.runs??[]).length)runs.append(node('p','No runs yet. Preview an execution plan first.','muted'));
  renderReferences();
}
const svgNamespace='http://www.w3.org/2000/svg';
function svg(tag:string,attributes:Record<string,string>,text?:string):SVGElement {const element=document.createElementNS(svgNamespace,tag);for(const [key,value]of Object.entries(attributes))element.setAttribute(key,value);if(text!==undefined)element.textContent=text;return element;}
function positions() {if(!recipe)return;const remaining=[...recipe.nodes];const levels=new Map<string,number>();for(let i=0;i<recipe.nodes.length;i++){let progress=false;for(const n of remaining){if(levels.has(n.id))continue;const parents=[...recipe.edges,...conditionEdges(recipe)].filter(e=>e.target_node===n.id).map(e=>e.source_node);if(parents.every(p=>levels.has(p))){levels.set(n.id,parents.length?Math.max(...parents.map(p=>levels.get(p)!))+1:0);progress=true;}}if(!progress)break;}const rows=new Map<number,number>();for(const n of recipe.nodes){const level=levels.get(n.id)??0;const row=rows.get(level)??0;rows.set(level,row+1);if(!layouts.has(n.id))layouts.set(n.id,{x:40+level*280,y:35+row*150});}}
function renderGraph() {
  const layer=document.getElementById('graph-layer')!;layer.replaceChildren();positions();
  layer.setAttribute('transform',`translate(${pan.x} ${pan.y}) scale(${pan.scale})`);
  if(!recipe){layer.append(svg('text',{x:'40',y:'50',class:'graph-label'},'Open a recipe to author its pipeline.'));return;}
  for(const edge of recipe.edges){const from=layouts.get(edge.source_node),to=layouts.get(edge.target_node);if(!from||!to)continue;layer.append(svg('path',{d:`M ${from.x+220} ${from.y+54} C ${from.x+260} ${from.y+54},${to.x-40} ${to.y+54},${to.x} ${to.y+54}`,class:'edge'}));layer.append(svg('text',{x:String((from.x+220+to.x)/2),y:String((from.y+to.y)/2+44),class:'edge-label'},`${edge.source_port} → ${edge.target_port}`));}
  for(const edge of conditionEdges(recipe)){const from=layouts.get(edge.source_node),to=layouts.get(edge.target_node);if(!from||!to)continue;layer.append(svg('path',{d:`M ${from.x+220} ${from.y+54} C ${from.x+260} ${from.y+54},${to.x-40} ${to.y+54},${to.x} ${to.y+54}`,class:'edge conditional-edge','stroke-dasharray':'6 4'}));layer.append(svg('text',{x:String((from.x+220+to.x)/2),y:String((from.y+to.y)/2+26),class:'edge-label'},`if ${edge.source_port} == ${String(edge.equals)}`));}
  for(const item of recipe.nodes){const pos=layouts.get(item.id)!;const group=svg('g',{transform:`translate(${pos.x} ${pos.y})`,class:`graph-node ${selectedNode===item.id?'selected':''}`,tabindex:'0',role:'button','aria-label':`${item.id}, ${item.kind}`});group.append(svg('rect',{width:'220',height:'112',rx:'8'}));group.append(svg('text',{x:'14',y:'25',class:'node-name'},item.id));group.append(svg('text',{x:'14',y:'45',class:'node-kind'},item.kind+(iterationNodeLabel(recipe,item.id)?' · ↻':'')+(componentDraftProblems(recipe,item).length?' · incomplete':'')));group.append(svg('text',{x:'14',y:'70',class:'node-port'},`in: ${(item.inputs??[]).map(p=>`${p.name}:${p.type}`).join(', ')||'none'}`));group.append(svg('text',{x:'14',y:'91',class:'node-port'},`out: ${(item.outputs??[]).map(p=>`${p.name}:${p.type}`).join(', ')||'none'}`));group.addEventListener('click',()=>selectNode(item.id));group.addEventListener('keydown',event=>{if((event as KeyboardEvent).key==='Enter')selectNode(item.id);});layer.append(group);}
  if(!recipe.nodes.length)layer.append(svg('text',{x:'40',y:'60',class:'graph-label'},'Add a component below to start your pipeline.'));
  const source=$<HTMLSelectElement>('source-node'),target=$<HTMLSelectElement>('target-node');
  const values=recipe.nodes.map(n=>({value:n.id,label:n.id}));options(source,values,source.value);options(target,values,target.value);if(!source.value&&values.length)source.value=values[0].value;if(!target.value&&values.length)target.value=values[Math.min(1,values.length-1)].value;renderPorts();
  const edges=$('edges');edges.replaceChildren();recipe.edges.forEach((edge,index)=>{const row=node('div',undefined,'edge-row');row.append(node('code',`${edge.source_node}.${edge.source_port} → ${edge.target_node}.${edge.target_port}`),button('Remove edge',()=>{if(recipe)updateDraft({...recipe,edges:recipe.edges.filter((_,i)=>i!==index)});}));edges.append(row);});
  $('node-panel').hidden=!selectedNode;
}
function renderPorts() {for(const [nodeId,portId,side]of [['source-node','source-port','outputs'],['target-node','target-port','inputs']]){const id=$<HTMLSelectElement>(nodeId).value;const ports=recipe?.nodes.find(n=>n.id===id)?.[side]??[];options($<HTMLSelectElement>(portId),ports.map((p:RecordValue)=>({value:p.name,label:`${p.name}: ${p.type}`})));}}
$('source-node').onchange=$('target-node').onchange=renderPorts;
$('add-edge').onclick=()=>{if(!recipe)return;const edge:RecipeEdge={source_node:$<HTMLSelectElement>('source-node').value,source_port:$<HTMLSelectElement>('source-port').value,target_node:$<HTMLSelectElement>('target-node').value,target_port:$<HTMLSelectElement>('target-port').value};updateDraft({...recipe,edges:[...recipe.edges,edge]});};
$('add-node').onclick=()=>{if(!recipe)return;if(nodePanelDirty){report('Apply or discard node panel changes before adding a node.');return;}const id=$<HTMLInputElement>('node-id').value.trim();try{const current=readVisualSource($<HTMLTextAreaElement>('source').value);if(updateDraft(addNode(current,id,$<HTMLSelectElement>('node-kind').value)))selectNode(id);}catch(error){report((error as Error).message);}};
function fillNodePanel(id:string){const n=recipe?.nodes.find(item=>item.id===id);if(!n)return;nodePanelBaseline=JSON.stringify(n);nodePanelDirty=false;$('node-title').textContent=`${n.id} · ${n.kind}`;$<HTMLTextAreaElement>('node-implementation').value=pretty(n.implementation_ref);$<HTMLTextAreaElement>('node-inputs').value=pretty(n.inputs);$<HTMLTextAreaElement>('node-outputs').value=pretty(n.outputs);$<HTMLTextAreaElement>('node-parameters').value=pretty(n.parameters);fillConditionPanel(n);renderParameterControls(n);$('node-implementation-field').hidden=n.kind==='component.run';componentControls=recipe&&n.kind==='component.run'?new ComponentAuthoringControls($('node-component-fields'),recipe,n,()=>{nodePanelDirty=true;markDirty(true);}):undefined;$('node-component-fields').hidden=n.kind!=='component.run';}
function fillConditionPanel(n:RecipeNode){if(!recipe)return;const selected=conditionSelection(n),choices=conditionChoices(recipe,n.id);if(selected&&!choices.some(choice=>choice.value===selected))choices.push({value:selected,label:'Unresolved or unsupported gate; choose a bool output'});options($<HTMLSelectElement>('node-condition-source'),choices,selected);$<HTMLSelectElement>('node-condition-equals').value=n.condition?String(n.condition.equals):'true';}
function renderParameterControls(n:RecipeNode){parameterControls=undefined;try{parameterControls=new ParameterControls($('node-typed-parameters'),$<HTMLTextAreaElement>('node-parameters'),parameterSchema(n),()=>{nodePanelDirty=true;markDirty(true);},report);}catch(error){$('node-typed-parameters').textContent=(error as Error).message;}}
function selectNode(id:string){if(nodePanelDirty){report('Apply your panel changes or use Discard panel edits / reload node before selecting a node.');return;}selectedNode=id;renderGraph();fillNodePanel(id);}
for(const id of ['node-implementation','node-inputs','node-outputs','node-parameters','node-condition-source','node-condition-equals'])$(id).addEventListener('input',()=>{if(id==='node-parameters')parameterControls?.sourceChanged();nodePanelDirty=true;markDirty(true);});
$('reset-node-panel').onclick=()=>{if(selectedNode){fillNodePanel(selectedNode);markDirty(true);}};
$('apply-node').onclick=()=>{const n=recipe?.nodes.find(n=>n.id===selectedNode);if(!recipe||!n)return;try{const current=readVisualSource($<HTMLTextAreaElement>('source').value);const currentNode=current.nodes.find(item=>item.id===selectedNode);if(!currentNode)throw new Error('This node was removed from source. Your panel draft is preserved.');assertNodePanelCurrent(currentNode,nodePanelBaseline);let updated:RecipeNode={...currentNode,implementation_ref:n.kind==='component.run'?currentNode.implementation_ref:JSON.parse($<HTMLTextAreaElement>('node-implementation').value),inputs:JSON.parse($<HTMLTextAreaElement>('node-inputs').value),outputs:JSON.parse($<HTMLTextAreaElement>('node-outputs').value),parameters:parameterControls?.read()??JSON.parse($<HTMLTextAreaElement>('node-parameters').value)};if(updated.kind==='component.run'){if(!componentControls)throw new Error('Reload the component panel before applying.');updated=componentControls.read(current,updated);}updated=withTerminalCondition(current,updated,$<HTMLSelectElement>('node-condition-source').value,$<HTMLSelectElement>('node-condition-equals').value);if(updateDraft(replaceNode(current,updated))){fillNodePanel(updated.id);markDirty(true);}}catch(error){report(`Node JSON: ${(error as Error).message}`);}};
$('delete-node').onclick=()=>{if(recipe&&selectedNode){if(nodePanelDirty){report('Apply or discard panel edits before removing the node.');return;}try{const next=removeNode(recipe,selectedNode);if(updateDraft(next)){selectedNode=undefined;renderGraph();}}catch(error){report((error as Error).message);}}};
$('zoom-in').onclick=()=>{pan.scale=Math.min(3,pan.scale*1.2);renderGraph();};$('zoom-out').onclick=()=>{pan.scale=Math.max(.25,pan.scale/1.2);renderGraph();};$('fit').onclick=()=>{pan={x:0,y:0,scale:1};renderGraph();};
const graph=document.getElementById('graph')!;let dragging:{x:number;y:number}|undefined;
graph.addEventListener('wheel',(event)=>{const e=event as WheelEvent;e.preventDefault();pan.scale=Math.max(.25,Math.min(3,pan.scale*(e.deltaY>0?.9:1.1)));renderGraph();},{passive:false});
graph.addEventListener('pointerdown',event=>{const e=event as PointerEvent;if((e.target as Element).closest('.graph-node'))return;dragging={x:e.clientX,y:e.clientY};(graph as unknown as SVGSVGElement).setPointerCapture(e.pointerId);});
graph.addEventListener('pointermove',event=>{if(!dragging)return;const e=event as PointerEvent;const factor=900/graph.getBoundingClientRect().width;pan.x+=(e.clientX-dragging.x)*factor;pan.y+=(e.clientY-dragging.y)*factor;dragging={x:e.clientX,y:e.clientY};renderGraph();});
graph.addEventListener('pointerup',()=>{dragging=undefined;});graph.addEventListener('pointercancel',()=>{dragging=undefined;});

function renderDatasetRecords(){renderDatasets($('dataset-records'),datasets,openOperation,dataset=>send('prepareDatasetVersion',{datasetId:dataset.dataset_id,revision:dataset.revision}));}
function renderResearch(){renderResearchWorkflow($('research-workflow'),research,new Set(catalog.map(item=>item.name)),openOperation);}
function renderLiveRecipeSettings(){
  if(liveSettingsDirty)return;
  liveSettingsBaseline=liveSettingsIdentity(recipe);
  const settings=liveEvaluationSettings(recipe);$<HTMLInputElement>('live-enabled').checked=settings.enabled;$<HTMLInputElement>('live-concurrency').value=String(settings.max_concurrency);$<HTMLInputElement>('live-timeout').value=String(settings.timeout_seconds);
  const cadences=$('live-cadences');cadences.replaceChildren();
  for(const item of recipe?.nodes??[]){if(item.kind!=='model.train'||item.implementation_ref!=='builtin:model.train')continue;const label=node('label',`${item.id}: checkpoint every (steps)`);const input=node('input');input.type='number';input.min='1';input.max='100000';input.required=true;input.value=String(item.parameters.checkpoint_interval_steps??1);input.dataset.cadenceNode=item.id;label.append(input);cadences.append(label);}
  $('live-cadence-help').textContent='Fixture cadence is configurable above. Other training harnesses control checkpoint timing in their own component parameters; live evaluation starts when the harness reports a complete checkpoint.';
}
function liveFormValues(){return {enabled:$<HTMLInputElement>('live-enabled').checked,concurrency:$<HTMLInputElement>('live-concurrency').value,timeout:$<HTMLInputElement>('live-timeout').value,cadences:Object.fromEntries(Array.from(document.querySelectorAll<HTMLInputElement>('[data-cadence-node]')).map(input=>[input.dataset.cadenceNode!,input.value]))};}
$('live-recipe-form').addEventListener('input',()=>{liveSettingsDirty=true;markDirty(true);});
$('reset-live-settings').onclick=()=>{try{recipe=readVisualSource($<HTMLTextAreaElement>('source').value);liveSettingsDirty=false;renderLiveRecipeSettings();markDirty(true);}catch(error){report((error as Error).message);}};
$('live-recipe-form').onsubmit=event=>{event.preventDefault();if(!recipe)return;if(nodePanelDirty){report('Apply or discard node panel changes before changing live evaluation settings.');return;}try{const current=readVisualSource($<HTMLTextAreaElement>('source').value);assertLiveSettingsCurrent(current,liveSettingsBaseline);const cadences:Record<string,number>={};for(const input of document.querySelectorAll<HTMLInputElement>('[data-cadence-node]'))cadences[input.dataset.cadenceNode!]=Number(input.value);const updated=withLiveEvaluationSettings(current,{enabled:$<HTMLInputElement>('live-enabled').checked,max_concurrency:Number($<HTMLInputElement>('live-concurrency').value),timeout_seconds:Number($<HTMLInputElement>('live-timeout').value)},cadences);liveSettingsDirty=false;updateDraft(updated);}catch(error){report((error as Error).message);}};
function renderAdmission(plan:RecordValue){
  if(plan.capabilities)executionEditor.capabilities(plan.capabilities);
  const container=$('run-admission');const messages=admissionMessages(plan);container.replaceChildren();container.hidden=!messages.length&&plan.executable!==false;
  if(!container.hidden){container.append(node('h3',plan.executable===false?'Execution plan blocked':'Local execution capabilities'));for(const message of messages)container.append(node('p',message));}
}
function resetRun(){
  $('run-export-actions').replaceChildren();$('run-summary').replaceChildren();$('run-logs').textContent='Select a run.';$('run-artifacts').textContent='No run selected.';$('tracking-status').textContent='';renderLiveEvaluations($('live-evaluations'),undefined);
}
function renderNotificationPreferences(raw:unknown){
  const prefs=notificationPreferences(raw??DEFAULT_NOTIFICATION_PREFERENCES);
  for(const key of NOTIFICATION_BOOLEAN_KEYS)$<HTMLInputElement>(`notify-${key}`).checked=prefs[key];
  $<HTMLInputElement>('notify-metric').value=prefs.metric;$<HTMLInputElement>('notify-delta').value=String(prefs.minimumDelta);$<HTMLInputElement>('notify-interval').value=String(prefs.minimumIntervalSeconds);
}
$('notification-form').onsubmit=event=>{event.preventDefault();const preferences:RecordValue={metric:$<HTMLInputElement>('notify-metric').value,minimumDelta:Number($<HTMLInputElement>('notify-delta').value),minimumIntervalSeconds:Number($<HTMLInputElement>('notify-interval').value)};for(const key of NOTIFICATION_BOOLEAN_KEYS)preferences[key]=$<HTMLInputElement>(`notify-${key}`).checked;send('notificationPreferences',{preferences});};
let trackingDirty=false;
$('tracking-form').addEventListener('input',()=>{trackingDirty=true;$('tracking-saved').textContent='Unsaved tracking preferences';});
function renderTrackingDefaults(){
  if(trackingDirty)return;
  const config=snapshot.project?.tracking??{};
  $<HTMLSelectElement>('tracking-mode').value=config.mode??'disabled';$<HTMLInputElement>('tracking-entity').value=config.entity??'';$<HTMLInputElement>('tracking-project').value=config.project??'';
  $<HTMLInputElement>('tracking-metrics').value=(config.metric_names??[]).join(', ');$<HTMLSelectElement>('tracking-artifacts').value=config.artifact_policy??'none';
}
$('tracking-form').onsubmit=event=>{event.preventDefault();const mode=$<HTMLSelectElement>('tracking-mode').value;const entity=$<HTMLInputElement>('tracking-entity').value.trim(),project=$<HTMLInputElement>('tracking-project').value.trim();if(mode!=='disabled'&&(!entity||!project)){report('Choose an explicit W&B entity and project for enabled tracking.');return;}send('operation',{operation:'project.update',payload:{expected_revision:snapshot.project.revision,changes:{tracking:{mode,entity:entity||null,project:project||null,credential_env:'MODELBUILDER_WANDB_API_KEY',metric_names:$<HTMLInputElement>('tracking-metrics').value.split(',').map(item=>item.trim()).filter(Boolean),artifact_policy:$<HTMLSelectElement>('tracking-artifacts').value}}}});};
function renderTrackingStatus(tracking:RecordValue|undefined){$('tracking-status').textContent=tracking?`W&B: ${tracking.mode??'disabled'} · ${tracking.status??'unknown'}${tracking.url?` · ${tracking.url}`:''}${tracking.error_code?` · ${tracking.error_code}`:''}${tracking.remote_may_be_partial?' · Remote upload may be partial.':''}`:'W&B: disabled unless explicitly configured for this run.';}
function openOperation(name:string,overrides:RecordValue={}){
  if(name==='research.document.import'){
    if(!snapshot.project?.project_id||!overrides.evidence_id){report('Choose Import authorized PDF on a saved evidence citation.');return;}
    send('importResearchDocument',{projectId:snapshot.project.project_id,evidenceId:overrides.evidence_id,expectedEvidenceRevision:overrides.expected_evidence_revision});return;
  }
  if(['research.proposal.create','dataset.bind'].includes(name)&&(nodePanelDirty||liveSettingsDirty||executionEditor.isDirty())){report('Apply or discard recipe panel changes before staging a recipe proposal.');return;}
  const spec=catalog.find(o=>o.name===name);if(!spec){report(`Operation unavailable: ${name}`);return;}selectedOperation=spec;
  $('operation-panel').hidden=false;$('operation-title').textContent=name;$('operation-description').textContent=`${spec.description} Required scope: ${spec.scope}.`;
  const form=$<HTMLFormElement>('operation-form');form.replaceChildren();const defaults=operationDefaults(recipe,activeRun);if(!(name.startsWith('recipe.')||['run.plan','run.submit','run.resume','dataset.bind'].includes(name)))delete defaults.expected_revision;
  if(name==='research.watch.create')Object.assign(defaults,{provider:'openalex',enabled:true,limit:5,interval_seconds:86400});
  if(name==='research.text.fetch')Object.assign(defaults,{provider:'europe_pmc'});
  if(name==='dataset.preview')Object.assign(defaults,{offset:0,limit:20});
  if(name==='dataset.bind')defaults.expected_revision=recipe?.revision;
  if(name==='dataset.register'||(name==='dataset.validate'&&!overrides.dataset_id))defaults.manifest={name:'',benchmark:'',license:'',provenance:{source:''},sources:[{id:'',root_id:'',path:'',format:'jsonl',sha256:''}],id_column:'id',row_schema:{type:'object',properties:{id:{type:'string'}},required:['id']},splits:[],transformations:[]};
  if(name==='research.proposal.create'){try{defaults.recipe=readVisualSource($<HTMLTextAreaElement>('source').value);}catch(error){report((error as Error).message);return;}defaults.citations=[];}
  Object.assign(defaults,overrides);
  for(const [key,schema]of Object.entries(spec.input_schema.properties)){
    if(['project_id','idempotency_key','request_id'].includes(key)||((name.startsWith('recipe.')||['run.plan','run.submit','run.resume','dataset.bind'].includes(name))&&['recipe_id','expected_revision','expected_recipe_revision'].includes(key))||(name==='run.submit'&&['trusted','target'].includes(key))||(['research.proposal.create','research.proposal.apply'].includes(name)&&['expected_recipe_revision','confirm_apply'].includes(key)))continue;
    if(name==='dataset.validate'&&((overrides.dataset_id&&key==='manifest')||(!overrides.dataset_id&&['dataset_id','revision'].includes(key))))continue;
    if(key==='manifest'&&name.startsWith('dataset.')){form.append(createDatasetManifestEditor(document,datasetSchema,defaults.manifest??{},report));continue;}
    const required=spec.input_schema.required.includes(key);const label=node('label',`${key.replaceAll('_',' ')}${required?' *':''}`);let input:HTMLInputElement|HTMLTextAreaElement|HTMLSelectElement;
    const types=Array.isArray(schema.type)?schema.type:[schema.type,...(schema.anyOf??[]).map((option:RecordValue)=>option.type),...(schema.const===undefined?[]:[typeof schema.const])];
    if(name==='dataset.bind'&&key==='target_node'){input=node('select');options(input,[{value:'',label:'Leave import unconnected'},...(recipe?.nodes??[]).filter((item:RecordValue)=>(item.inputs??[]).some((port:RecordValue)=>port.type==='dict')).map((item:RecordValue)=>({value:item.id,label:item.id}))]);}
    else if(name==='dataset.bind'&&key==='target_port'){input=node('select');options(input,[{value:'',label:'Choose a target node first'}]);}
    else if(key==='hypothesis_id'&&(snapshot.hypotheses??[]).length){input=node('select');options(input,[{value:'',label:'Choose a saved hypothesis'},...snapshot.hypotheses.map((item:RecordValue)=>({value:item.hypothesis_id,label:item.proposed_change??item.hypothesis_id}))]);}
    else if(key==='evidence_id'&&(snapshot.evidence??[]).length){input=node('select');options(input,[{value:'',label:'Choose a saved citation'},...snapshot.evidence.map((item:RecordValue)=>({value:item.evidence_id,label:item.title??item.evidence_id}))]);}
    else if(schema.enum){input=node('select');options(input,schema.enum.map((value:string)=>({value,label:value})));}
    else if(types.includes('object')||types.includes('array')){input=node('textarea');input.rows=3;input.spellcheck=false;input.dataset.json='true';}
    else{input=node('input');input.type=types.includes('boolean')?'checkbox':types.includes('integer')||types.includes('number')?'number':'text';}
    input.name=key;input.id=`op-${key}`;input.required=required;
    if(input instanceof HTMLInputElement&&input.type==='number'){if(schema.minimum!==undefined)input.min=String(schema.minimum);if(schema.maximum!==undefined)input.max=String(schema.maximum);input.step=types.includes('integer')?'1':'any';}
    if(schema.maxLength!==undefined&&(input instanceof HTMLInputElement||input instanceof HTMLTextAreaElement))input.maxLength=schema.maxLength;
    if(key==='text'&&input instanceof HTMLInputElement){const textarea=node('textarea');textarea.rows=8;textarea.name=input.name;textarea.id=input.id;textarea.required=input.required;input=textarea;}
    if(defaults[key]===undefined&&schema.default!==undefined)defaults[key]=schema.default;
    if(defaults[key]!==undefined){if(input instanceof HTMLInputElement&&input.type==='checkbox')input.checked=defaults[key];else input.value=input.dataset.json?pretty(defaults[key]):String(defaults[key]);}
    label.append(input);
    if(key==='citations'&&input instanceof HTMLTextAreaElement){
      const source=input,picker=node('select');picker.id='citation-picker';picker.multiple=true;picker.size=Math.min(6,Math.max(2,(snapshot.evidence??[]).length));picker.setAttribute('aria-label','Choose saved citations');
      options(picker,(snapshot.evidence??[]).map((item:RecordValue)=>({value:item.evidence_id,label:item.title??item.evidence_id})));
      const syncSelection=()=>{try{const references=JSON.parse(source.value||'[]');if(Array.isArray(references))for(const option of picker.options)option.selected=references.some(item=>item.evidence_id===option.value);}catch{}};
      source.addEventListener('input',syncSelection);syncSelection();
      picker.onchange=()=>{try{source.value=pretty(selectedCitations(source.value,Array.from(picker.selectedOptions).map(option=>option.value)));}catch(error){syncSelection();report((error as Error).message);}};
      label.insertBefore(picker,input);label.append(node('small','Choose citations above. Advanced JSON below supports exact imported-text IDs, quotes and offsets; the service verifies those passages.','muted'));
    }
    form.append(label);
  }
  if(name==='dataset.bind'){
    const target=$<HTMLSelectElement>('op-target_node'),port=$<HTMLSelectElement>('op-target_port');
    const updatePorts=()=>{const selected=port.value;const targetNode=(recipe?.nodes??[]).find((item:RecordValue)=>item.id===target.value);options(port,[{value:'',label:target.value?'Choose dict input':'Choose a target node first'},...(targetNode?.inputs??[]).filter((item:RecordValue)=>item.type==='dict').map((item:RecordValue)=>({value:item.name,label:item.name}))]);if(Array.from(port.options).some(option=>option.value===selected))port.value=selected;port.required=!!target.value;};
    target.addEventListener('change',updatePorts);updatePorts();
  }
  const evidencePicker=$<HTMLSelectElement>('op-evidence_id');if(evidencePicker instanceof HTMLSelectElement)evidencePicker.addEventListener('change',()=>{const current=(snapshot.evidence??[]).find((item:RecordValue)=>item.evidence_id===evidencePicker.value);const revision=$<HTMLInputElement>('op-expected_evidence_revision');if(revision&&current)revision.value=String(current.revision);});
  const submit=node('button',name==='dataset.bind'?'Review recipe dataset binding':name==='research.proposal.apply'?'Review and apply approved proposal':name==='research.proposal.create'?'Stage proposal for review':name==='research.proposal.review'?'Record explicit review':spec.mutating?'Submit operation':'Read / preview','primary');submit.type='submit';form.append(submit);
  form.onsubmit=event=>{event.preventDefault();const payload:RecordValue={};try{if(form.querySelector('[data-manifest-dirty="true"]'))throw new Error('Apply the manifest fields to JSON before submitting, so the service uses your visible changes.');for(const input of Array.from(form.elements)){if(!(input instanceof HTMLInputElement||input instanceof HTMLTextAreaElement||input instanceof HTMLSelectElement)||!input.name)continue;if(input instanceof HTMLInputElement&&input.type==='checkbox')payload[input.name]=input.checked;else if(input.value!=='')payload[input.name]=input.dataset.json?JSON.parse(input.value):(input instanceof HTMLInputElement&&input.type==='number'?Number(input.value):input.value);}send('operation',{operation:name,payload});}catch(error){report(`Form JSON: ${(error as Error).message}`);}};
  $('operation-panel').scrollIntoView({block:'nearest'});
}
function buildActions(){
  const groups:Record<string,[string,string][]>={
    'research-actions':[['evidence.add','Add evidence'],['evidence.search','Search saved evidence'],['claim.create','Add claim'],['claim.update','Edit claim'],['hypothesis.create','New hypothesis'],['hypothesis.update','Edit hypothesis'],['decision.record','Record decision']],
    'dataset-actions':[['dataset.register','Register dataset version'],['dataset.validate','Validate a manifest'],['dataset.get','Inspect historical revision'],['dataset.bind','Bind historical dataset revision']],
    'research-workflow-actions':[['research.context.build','Build project context'],['research.context.capabilities','Context capabilities'],['research.watch.create','New research watch'],['research.watch.refresh_due','Refresh due watches now'],['research.text.import','Import licensed source text'],['research.text.fetch','Retrieve licensed PMC paper'],['research.proposal.create','Stage recipe proposal']],
    'run-actions':[['run.plan','Preview run'],['run.submit','Run recipe'],['run.cancel','Request stop'],['run.resume','Resume checkpoint']],
    'version-actions':[['model.version.list','Load versions']],
    'compute-actions':[['execution.targets','Refresh hardware and targets'],['run.plan','Preview recipe requirements'],['policy.get','Read policy'],['cost.estimate','Inspect cost availability']]
  };
  for(const [id,actions]of Object.entries(groups)){$(id).replaceChildren(...actions.filter(([name])=>catalog.some(o=>o.name===name)).map(([name,label])=>button(label,()=>['model.version.list','execution.targets'].includes(name)?send('operation',{operation:name,payload:{}}):openOperation(name))));}
  $('dataset-actions').append(button('Reload datasets',()=>send('refreshDatasets')));
  $('research-workflow-actions').append(button('Reload saved research',()=>send('refreshResearch')));
  const recipeActions=$('recipe-extra-actions');recipeActions.replaceChildren();for(const [name,label]of [['recipe.resolve_dependencies','Inspect dependencies']])if(catalog.some(o=>o.name===name)){const action=button(label,()=>openOperation(name));action.dataset.operation=name;recipeActions.append(action);}
  if(catalog.some(item=>item.name==='component.list')){const action=button('Browse reusable components',()=>send('operation',{operation:'component.list',payload:{}}));action.dataset.operation='component.list';recipeActions.append(action);}
}
window.addEventListener('message',event=>{
  const message=event.data;if(!message||typeof message.type!=='string')return;
  if(message.type==='landing'||(message.type==='project'&&snapshot.project?.project_id!==message.snapshot?.project?.project_id)){$('component-library').replaceChildren();$('component-library').hidden=true;}
  if(message.type==='result'&&message.operation==='component.list'&&recipe?.recipe_id===message.result.recipe_id){
    renderComponentLibrary($('component-library'),message.result,(entry,id)=>{try{
      if(nodePanelDirty)throw new Error('Apply or discard node panel changes before adding a library component.');
      const current=readVisualSource($<HTMLTextAreaElement>('source').value);
      const next=addLibraryComponent(current,entry,id,message.result.recipe_id,message.result.recipe_revision);
      if(updateDraft(next))selectNode(id);
    }catch(error){report((error as Error).message);}});
  }
  switch(message.type){
    case 'init':datasetSchema=message.datasetSchema??{};catalog=message.catalog;recipeHistory.update(savedRecipe,catalog);renderNotificationPreferences(message.notificationPreferences);renderProjects(message.projects);renderScientificPanel();$('connection').textContent=message.trusted?'Service connected':'Read/author mode · Workspace untrusted';buildActions();break;
    case 'landing':savedRecipe=undefined;renderScientificPanel();recipeHistory.update(undefined,catalog);showLanding();renderProjects(message.projects??[]);break;
    case 'project':if(snapshot.project?.project_id!==message.snapshot?.project?.project_id){executionEditor.capabilities();savedRecipe=undefined;recipeHistory.update(undefined,catalog);trackingDirty=false;$('cost-diagnostics').replaceChildren();$('compute-diagnostics').replaceChildren();research={};renderResearch();$('research-reader').replaceChildren();$('research-reader').hidden=true;datasets=[];renderDatasetRecords();$('dataset-error').hidden=true;$('dataset-error').textContent='';$('dataset-preview').hidden=true;$('dataset-preview').replaceChildren();}snapshot=message.snapshot;activeRun=message.runId;resetRun();renderSnapshot();renderNotificationPreferences(message.notificationPreferences);showSection(message.section??'Recipe');break;
    case 'snapshot':if(message.snapshot?.project?.project_id!==snapshot.project?.project_id)break;snapshot=message.snapshot;renderSnapshot();break;
    case 'costDiagnostics':if(message.projectId===snapshot.project?.project_id)renderCostDiagnostics($('cost-diagnostics'),message.result);break;
    case 'datasets':if(message.projectId===snapshot.project?.project_id){datasets=message.datasets;renderDatasetRecords();$('dataset-error').hidden=!message.error;$('dataset-error').textContent=message.error??'';}break;
    case 'datasetVersion':if(message.projectId===snapshot.project?.project_id)openOperation('dataset.version',{dataset_id:message.dataset.dataset_id,expected_revision:message.dataset.revision,manifest:message.dataset.manifest});break;
    case 'research':if(message.projectId===snapshot.project?.project_id){research=message.research;renderResearch();}break;
    case 'capabilities':if(message.projectId===snapshot.project?.project_id){renderComputeDiagnostics($('compute-diagnostics'),message.result);kinds=executableKindChoices(message.result?.capabilities);options($<HTMLSelectElement>('node-kind'),kinds.map(kind=>({value:kind,label:kind})),$<HTMLSelectElement>('node-kind').value);renderAdmission(message.result);}break;
    case 'runList':if(message.projectId===snapshot.project?.project_id){snapshot.runs=message.runs;renderSnapshot();}break;
    case 'notificationPreferences':renderNotificationPreferences(message.preferences);$('notification-saved').textContent='Saved in this IDE workspace.';break;
    case 'recipe':renderRecipe(message.recipe??undefined);break;
    case 'savedRetained':savedRecipe=message.recipe;recipeHistory.update(savedRecipe,catalog);$('identity').textContent=`${message.recipe.name} · ${message.recipe.recipe_id} · saved revision ${message.recipe.revision}\nSHA256 ${message.recipe.digest}`;$('notice').hidden=false;$('notice').textContent='Saved the submitted revision. Your newer edits are still unsaved and have been preserved.';markDirty(true);break;
    case 'restoreDraft':{const state=message.state;$<HTMLTextAreaElement>('source').value=state.source;try{recipe=readVisualSource(state.source);$('parse-error').textContent='';}catch(error){$('parse-error').textContent=(error as Error).message;}executionEditor.reset(recipe);executionEditor.restore(state.executionSettingsDraft);iterationEditor.reset(recipe);iterationEditor.restore(state.iterationDraft);selectedNode=state.selectedNode;nodePanelDirty=Boolean(state.nodePanelDirty);nodePanelBaseline=state.nodePanelBaseline??'';renderGraph();if(selectedNode){fillNodePanel(selectedNode);nodePanelDirty=Boolean(state.nodePanelDirty);if(nodePanelDirty){nodePanelBaseline=state.nodePanelBaseline??nodePanelBaseline;for(const [id,value]of Object.entries(state.nodeFields??{}))if(['node-implementation','node-inputs','node-outputs','node-parameters','node-condition-source','node-condition-equals'].includes(id)){if(id==='node-condition-source'&&String(value)&&!Array.from($<HTMLSelectElement>(id).options).some(option=>option.value===String(value))){const saved=node('option','Unsaved gate selection; source changed');saved.value=String(value);$(id).append(saved);}$<HTMLTextAreaElement>(id).value=String(value);}const restoredNode=recipe?.nodes.find(item=>item.id===selectedNode);if(restoredNode){renderParameterControls(restoredNode);parameterControls?.restore(state.parameterDraft);componentControls?.restore(state.componentDraft);}}}liveSettingsDirty=false;renderLiveRecipeSettings();if(state.liveSettingsDirty){liveSettingsDirty=true;liveSettingsBaseline=state.liveSettingsBaseline??liveSettingsBaseline;const fields=state.liveFields??{};$<HTMLInputElement>('live-enabled').checked=fields.enabled===true;$<HTMLInputElement>('live-concurrency').value=String(fields.concurrency??1);$<HTMLInputElement>('live-timeout').value=String(fields.timeout??300);for(const input of document.querySelectorAll<HTMLInputElement>('[data-cadence-node]'))if(fields.cadences?.[input.dataset.cadenceNode!]!==undefined)input.value=String(fields.cadences[input.dataset.cadenceNode!]);}markDirty(true);$('notice').hidden=false;$('notice').textContent='Recovered your unsaved recipe draft. The original base revision is retained for conflict checking.';break;}
    case 'recipeHistoryResult':case 'recipeHistorySettled':recipeHistory.receive(message);break;
    case 'busy':busy=message.busy;busyControls.set(busy);break;
    case 'error':report(`${message.code?message.code+': ':''}${message.message}${message.details?'\n'+pretty(message.details):''}`);break;
    case 'connection':$('connection').textContent=message.message;break;
    case 'remoteChanged':$('remote').hidden=false;$('remote').textContent=`Another client saved revision ${message.revision}. Your unsaved draft is preserved. Use Diff, then Reload to discard or Save to inspect the conflict.`;break;
    case 'conflict':$('conflict').hidden=false;$('conflict-diff').textContent=pretty(message.diff);$('server-json').textContent=pretty(message.server);$('local-json').textContent=pretty(message.local);$('conflict').scrollIntoView({block:'nearest'});break;
    case 'result':if(['research.context.build','research.context.inspect'].includes(message.operation)){if(message.result.project_id!==snapshot.project?.project_id)break;renderResearchContextResult($('research-reader'),message.operation,message.result,openOperation);}if(message.operation==='execution.targets')renderComputeDiagnostics($('compute-diagnostics'),message.result);if(['research.text.inspect','research.text.search','research.text.fetch','research.document.import'].includes(message.operation)){if(message.result.project_id!==snapshot.project?.project_id)break;renderResearchTextResult($('research-reader'),message.operation,message.result,openOperation);$('research-reader').hidden=false;}releaseView.result(message.operation,message.result,message.request);if(message.operation==='dataset.preview'){renderDatasetPreview($('dataset-preview'),message.result,message.request??{},payload=>send('operation',{operation:'dataset.preview',payload}));$('dataset-preview').hidden=false;}if(message.operation==='project.update'){trackingDirty=false;$('tracking-saved').textContent='Project tracking defaults saved.';}if(['run.submit','run.resume'].includes(message.operation)&&message.result.operation_id){activeRun=message.result.operation_id;resetRun();} $('result-panel').hidden=false;$('result-title').textContent=message.operation;$('result').textContent=pretty(message.result);if(['execution.targets','cost.estimate','run.plan','policy.get'].includes(message.operation))$('compute-info').textContent=pretty(message.result);if(['run.plan','execution.targets'].includes(message.operation))renderAdmission(message.result);break;
    case 'run':if(!snapshot.project||message.projectId!==snapshot.project.project_id||(activeRun&&activeRun!==message.run.operation_id))break;activeRun=message.run.operation_id;releaseView.run(message.run);renderLiveEvaluations($('live-evaluations'),message.run.evaluations,message.run.status?.status,JSON.stringify([message.projectId,activeRun]));renderTrackingStatus(message.run.status?.tracking);$('run-summary').replaceChildren(node('h3',`Operation ${activeRun}`),node('p',conditionalRunSummary(message.run.status)),node('p',iterationRunSummary(message.run.status)),node('pre',pretty(message.run.status)));$('run-logs').textContent=pretty(message.run.logs);$('run-artifacts').textContent=pretty(message.run.artifacts);break;
  }
  busyControls.refresh();
});
send('ready');
