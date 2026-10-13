const {test}=require('node:test');const assert=require('node:assert/strict');
const {executionSettingsIdentity,stageParallelism,withStageParallelism}=require('../out/executionSettings');
const recipe={recipe_id:'r',project_id:'p',revision:1,nodes:[],edges:[],evaluation:{live:{enabled:true,max_concurrency:3}},environment:{device:'cpu'}};
test('missing settings remain sequential without rewriting canonical content',()=>{
  assert.equal(stageParallelism(recipe),1);assert.equal(executionSettingsIdentity(recipe),'null');
  assert.deepEqual(withStageParallelism(recipe,1,executionSettingsIdentity(recipe)),recipe);
  const explicit={...recipe,execution:{}};assert.deepEqual(withStageParallelism(explicit,1,executionSettingsIdentity(explicit)),explicit);
});
test('one canonical stage bound preserves project identity, evaluation and unrelated recipe settings',()=>{
  const before=JSON.stringify(recipe),next=withStageParallelism(recipe,4,executionSettingsIdentity(recipe));
  assert.deepEqual(next,{...recipe,execution:{max_parallel_stages:4}});assert.equal(JSON.stringify(recipe),before);
  const sequential=withStageParallelism(next,1,executionSettingsIdentity(next));assert.equal(sequential.execution.max_parallel_stages,1);
});
test('source changes block stale forms, but unrelated source edits are preserved',()=>{
  const baseline=executionSettingsIdentity(recipe),modified={...recipe,execution:{max_parallel_stages:2}};
  assert.throws(()=>withStageParallelism(modified,3,baseline),/changed in source/);assert.equal(modified.execution.max_parallel_stages,2);
  const unrelated={...recipe,name:'New source title'};assert.equal(withStageParallelism(unrelated,3,baseline).name,'New source title');
});
test('typed editor refuses invalid bounds or malformed source configuration',()=>{
  for(const value of [0,9,1.5,NaN,Infinity,'2',true,null])assert.throws(()=>withStageParallelism(recipe,value,executionSettingsIdentity(recipe)),/whole number/);
  for(const execution of [null,[],1,'parallel']){const invalid={...recipe,execution};assert.throws(()=>withStageParallelism(invalid,2,executionSettingsIdentity(invalid)),/Fix the execution object/);}
});

// These are deliberately small simulated DOM unit tests, not browser acceptance.
class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.listeners={};this.value='';this._text='';}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join(' ');}
  append(...children){this.children.push(...children);}
  addEventListener(name,callback){this.listeners[name]=callback;}
}
const flatten=element=>[element,...element.children.flatMap(flatten)];
function harness(initial=recipe){
  global.document={createElement:tag=>new Element(tag)};
  const {ExecutionSettingsEditor}=require('../out/executionSettings'),root=new Element('section'),errors=[];
  let current=structuredClone(initial),invalid=false,applyResult=true,applies=0;
  const editor=new ExecutionSettingsEditor(root,()=>{if(invalid)throw new Error('Invalid source JSON');return current;},value=>{applies++;if(applyResult)current=value;return applyResult;},()=>{},error=>errors.push(error));editor.update(current);
  const find=id=>flatten(root).find(item=>item.id===id);
  return {editor,root,errors,find,current:()=>current,applies:()=>applies,source(value){current=value;editor.update(value);},invalid(value){invalid=value;},applyResult(value){applyResult=value;},set(value){find('stage-parallelism').value=value;find('stage-parallelism').listeners.input();},submit(){find('stage-execution-form').onsubmit({preventDefault(){}});},discard(){find('reset-stage-execution').onclick();}};
}
test('settings panel applies, discards, repeats edits, and preserves incomplete restored values',()=>{
  const h=harness();h.set('2');assert(h.editor.isDirty());h.submit();assert.equal(h.current().execution.max_parallel_stages,2);assert.equal(h.editor.isDirty(),false);
  h.set('');const state=h.editor.capture();h.editor.reset(h.current());h.editor.restore(state);assert.equal(h.find('stage-parallelism').value,'');assert(h.editor.isDirty());h.submit();assert.equal(h.current().execution.max_parallel_stages,2);assert.match(h.errors.at(-1),/whole number/);
  h.discard();assert.equal(h.find('stage-parallelism').value,'2');assert.equal(h.editor.isDirty(),false);
  h.set('4');h.submit();assert.equal(h.current().execution.max_parallel_stages,4);
});
test('stale source and apply rejection keep dirty field state instead of dropping edits',()=>{
  const h=harness();h.set('3');h.source({...recipe,execution:{max_parallel_stages:5}});h.submit();assert.match(h.errors.at(-1),/changed in source/);assert.equal(h.find('stage-parallelism').value,'3');assert(h.editor.isDirty());
  h.discard();assert.equal(h.find('stage-parallelism').value,'5');h.set('4');h.applyResult(false);h.submit();assert.equal(h.find('stage-parallelism').value,'4');assert(h.editor.isDirty());assert.equal(h.current().execution.max_parallel_stages,5);
  h.invalid(true);h.discard();assert.equal(h.find('stage-parallelism').value,'4');assert(h.editor.isDirty());assert.match(h.errors.at(-1),/Invalid source/);
});
test('capability text is conservative unless both support and a bound are explicit',()=>{
  const h=harness();assert.match(h.root.textContent,/has not been checked/);
  h.editor.capabilities({control_flow:{independent_stage_concurrency:false}});assert.match(h.root.textContent,/does not advertise/);
  h.editor.capabilities({control_flow:{independent_stage_concurrency:true}});assert.match(h.root.textContent,/does not advertise/);
  h.editor.capabilities({control_flow:{independent_stage_concurrency:true},recipe_settings:{execution:{max_parallel_stages:{supported_maximum:8}}}});assert.match(h.root.textContent,/supports up to 8/);
  h.editor.capabilities({control_flow:{independent_stage_concurrency:true},recipe_settings:{execution:{max_parallel_stages:{supported_maximum:8,platforms:['linux'],devices:['cpu']}}}});assert.match(h.root.textContent,/stages on Linux CPU/);
  h.editor.capabilities();assert.match(h.root.textContent,/has not been checked/);
});
