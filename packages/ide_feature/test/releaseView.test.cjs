const {test}=require('node:test'),assert=require('node:assert/strict');const {ReleaseView}=require('../out/releaseView');
class Element{constructor(tag,doc){this.tagName=tag;this.ownerDocument=doc;this.children=[];this.dataset={};this._text='';this.value='';this.disabled=false;this.checked=false;}set textContent(v){this._text=String(v);this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent).join(' ');}append(...children){for(const child of children){child.parent=this;this.children.push(child);if(this.tagName==='select'&&this.children.length===1)this.value=child.value;}}replaceChildren(...children){this.children=[];this._text='';this.append(...children);}querySelector(){return all(this).find(n=>n.dataset.comparisonResult);}remove(){this.parent.children=this.parent.children.filter(n=>n!==this);}}
const all=node=>[node,...node.children.flatMap(all)],button=(root,text)=>all(root).find(n=>n.tagName==='button'&&n._text===text),input=(root,value)=>all(root).find(n=>n.tagName==='input'&&n.value===value);
function harness(readonly=false){const doc={createElement:tag=>new Element(tag,doc)},compare=new Element('section',doc),versions=new Element('section',doc),exports=new Element('section',doc),sent=[],errors=[];const view=new ReleaseView(compare,versions,exports,(type,payload)=>sent.push({type,...payload}),value=>errors.push(value));const catalog=['run.compare','run.status','decision.record','model.version.list','model.version.promote','model.version.lineage','model.export.list'].map(name=>({name,authorized:!readonly||!['decision.record','model.version.promote'].includes(name)}));const snapshot={project:{project_id:'p',revision:4},runs:[{backend_run_id:'a',recipe_revision:1,status:'succeeded'},{backend_run_id:'b',recipe_revision:2,status:'succeeded'}],hypotheses:[{hypothesis_id:'h',proposed_change:'Change steps'}],decisions:[]};view.update(snapshot,catalog);const select=()=>{for(const id of ['a','b']){const el=input(compare,id);el.checked=true;el.onchange();}};return {view,compare,versions,exports,sent,errors,catalog,snapshot,select};}
const compared={metric:'validation_loss',direction:'minimize',compatible:true,selected_run_id:'b',selected_model_version_ref:'v',comparison_digest:'digest',comparison_contract:{allowed_variations:['model','checkpoint']},runs:[{backend_run_id:'a',value:.8},{backend_run_id:'b',value:.4}],run_provenance:[{backend_run_id:'a',recipe_id:'r',recipe_revision:1},{backend_run_id:'b',recipe_id:'r',recipe_revision:2}],configuration_diffs:[{baseline_run_id:'a',backend_run_id:'b',recipe_revision:2,diff:'steps 3 to 5'}]};const request={run_ids:['a','b'],metric:'validation_loss',direction:'minimize'};
test('typed selection compares actual run IDs and shows compatibility/configuration diff',()=>{const h=harness();h.select();button(h.compare,'Compare selected runs').onclick();assert.deepEqual(h.sent.at(-1),{type:'operation',operation:'run.compare',payload:request});h.view.result('run.compare',compared,request);assert.match(h.compare.textContent,/Compatible evidence/);assert.match(h.compare.textContent,/steps 3 to 5/);});
test('decision records winner recipe revision and retains rationale through polling',()=>{const h=harness();h.select();h.view.result('run.compare',compared,request);const rationale=all(h.compare).find(n=>n.tagName==='textarea');rationale.value='Measured lower loss';rationale.oninput();h.view.update(h.snapshot,h.catalog);assert.equal(all(h.compare).find(n=>n.tagName==='textarea').value,'Measured lower loss');all(h.compare).find(n=>n.tagName==='form').onsubmit({preventDefault(){}});assert.equal(h.sent.at(-1).payload.recipe_revision,2);assert.equal(h.sent.at(-1).payload.selection_rule.comparison_digest,'digest');assert.equal(h.sent.at(-1).payload.outcome,'accepted');});
test('incompatible evidence never offers accepted decision or winner',()=>{const h=harness();h.select();h.view.result('run.compare',{...compared,compatible:false,selected_run_id:null,diagnostics:[{field:'split',message:'Different sample IDs'}]},request);assert.match(h.compare.textContent,/no winner/);assert.match(h.compare.textContent,/Different sample IDs/);assert(!all(h.compare).some(n=>n.tagName==='option'&&n.value==='accepted'));});
test('readonly token retains export read controls and disables record/promotion',()=>{const h=harness(true);h.select();h.view.result('run.compare',compared,request);assert(button(h.compare,'Record decision').disabled);h.snapshot.decisions=[{decision_id:'d',outcome:'accepted',rationale:'Measured',selection_rule:{metric:'loss',direction:'minimize'}}];h.view.update(h.snapshot,h.catalog);h.view.result('model.version.list',{versions:[{model_version_id:'v'}]});assert(button(h.versions,'Promote by selected decision').disabled);assert(!button(h.versions,'Save model package…').disabled);});
test('version decisions and native Save preserve exact selected version and expected project revision',()=>{const h=harness();h.snapshot.decisions=[{decision_id:'d',outcome:'accepted',rationale:'Measured',selection_rule:{metric:'loss',direction:'minimize'}}];h.view.update(h.snapshot,h.catalog);h.view.result('model.version.list',{versions:[{model_version_id:'v'}]});button(h.versions,'Promote by selected decision').onclick();assert.deepEqual(h.sent.at(-1).payload,{model_version_id:'v',decision_id:'d',stage:'staging',expected_revision:4});button(h.versions,'Save model package…').onclick();assert.deepEqual(h.sent.at(-1),{type:'nativeSaveModel',modelVersionId:'v'});});
test('changed selection invalidates a previous comparison and late response',()=>{const h=harness();h.select();const el=input(h.compare,'b');el.checked=false;el.onchange();h.view.result('run.compare',compared,request);assert.doesNotMatch(h.compare.textContent,/Compatible evidence/);});
test('ambiguous evaluation requires an explicit selector for every metric before comparing',()=>{const h=harness();h.select();const entry=(id,node)=>({backend_run_id:id,node_id:node,output_port:'metrics',artifact_id:id+node,metric:'validation_loss',value:.4});h.view.result('run.status',{backend_run_id:'a',metrics:{evaluation_catalog:[entry('a','first'),entry('a','second')]}});h.view.result('run.status',{backend_run_id:'b',metrics:{evaluation_catalog:[entry('b','only')]}});button(h.compare,'Compare selected runs').onclick();assert.match(h.errors.at(-1),/explicit evaluation/);const picker=all(h.compare).find(n=>n.tagName==='select'&&n.children.some(c=>c.value==='first:metrics:afirst'));picker.value='first:metrics:afirst';picker.onchange();button(h.compare,'Compare selected runs').onclick();assert.deepEqual(h.sent.at(-1).payload.evaluations,[Object.fromEntries(Object.entries(entry('a','first')).filter(([k])=>k!=='value')),Object.fromEntries(Object.entries(entry('b','only')).filter(([k])=>k!=='value'))]);});
test('changing project clears version/comparison selection without creating navigation or registry',()=>{const h=harness();h.select();h.view.result('model.version.list',{versions:[{model_version_id:'old'}]});h.view.update({project:{project_id:'other',revision:1},runs:[]},h.catalog);assert(!all(h.versions).some(n=>n.dataset.versionId));assert(!all(h.compare).some(n=>n.checked));assert.equal(h.exports.children.length,0);});

test('changing only the native evaluation selector rejects a late comparison response',()=>{
 const h=harness();h.select();const entry=(id,node)=>({backend_run_id:id,node_id:node,output_port:'metrics',artifact_id:id+node,metric:'validation_loss',value:.4});
 h.view.result('run.status',{backend_run_id:'a',metrics:{evaluation_catalog:[entry('a','first'),entry('a','second')]}});
 h.view.result('run.status',{backend_run_id:'b',metrics:{evaluation_catalog:[entry('b','only')]}});
 const choose=value=>{const picker=all(h.compare).find(n=>n.tagName==='select'&&n.children.some(c=>c.value==='first:metrics:afirst'));picker.value=value;picker.onchange();};
 choose('first:metrics:afirst');button(h.compare,'Compare selected runs').onclick();const oldRequest=h.sent.at(-1).payload;
 choose('second:metrics:asecond');h.view.result('run.compare',{...compared,evaluations:oldRequest.evaluations},oldRequest);
 assert.doesNotMatch(h.compare.textContent,/Compatible evidence/);assert(!button(h.compare,'Record decision'));
 button(h.compare,'Compare selected runs').onclick();const newRequest=h.sent.at(-1).payload;
 h.view.result('run.compare',{...compared,evaluations:newRequest.evaluations},newRequest);assert.match(h.compare.textContent,/Compatible evidence/);
 const rationale=all(h.compare).find(n=>n.tagName==='textarea');rationale.value='Exact selected evaluation';rationale.oninput();all(h.compare).find(n=>n.tagName==='form').onsubmit({preventDefault(){}});
 assert.deepEqual(h.sent.at(-1).payload.selection_rule.evaluations,newRequest.evaluations);
});
test('a stale ambiguous or missing selector is never accepted after catalog refresh',()=>{
 const h=harness();h.select();button(h.compare,'Compare selected runs').onclick();
 const entry=node=>({backend_run_id:'a',node_id:node,output_port:'metrics',artifact_id:node,metric:'validation_loss',value:.4});
 h.view.result('run.status',{backend_run_id:'a',metrics:{evaluation_catalog:[entry('first'),entry('second')]}});
 h.view.result('run.compare',compared,request);assert.doesNotMatch(h.compare.textContent,/Compatible evidence/);
 assert(!button(h.compare,'Record decision'));
});

test('a changed evaluator catalog invalidates an already displayed comparison',()=>{
 const h=harness();h.select();h.view.result('run.compare',compared,request);assert.match(h.compare.textContent,/Compatible evidence/);
 const entry=node=>({backend_run_id:'a',node_id:node,output_port:'metrics',artifact_id:node,metric:'validation_loss',value:.4});
 h.view.result('run.status',{backend_run_id:'a',metrics:{evaluation_catalog:[entry('first'),entry('second')]}});
 assert.doesNotMatch(h.compare.textContent,/Compatible evidence/);assert(!button(h.compare,'Record decision'));
});

function policyHarness(custom=false){
 const h=harness();h.catalog.push(...['scientific.policy.get','run.artifacts'].map(name=>({name,authorized:true})));
 h.snapshot.project.scientific_policy_digest='policy-digest';h.view.update({...h.snapshot,project:{...h.snapshot.project}},h.catalog);h.select();
 // Snapshot is a shared mutable fixture: explicitly choose the pinned digest.
 const digest=all(h.compare).find(n=>n.tagName==='input'&&n.placeholder?.startsWith('Exact registered'));digest.value='policy-digest';digest.oninput();
 button(h.compare,'Load scientific policy').onclick();assert.equal(h.sent.at(-1).operation,'scientific.policy.get');
 const p={id:'retention',version:'1',rules:[{id:'quality',metric:'validation_loss'},{id:'retention',metric:'retention'}],...(custom?{evaluator:{component_digest:'pin'}}:{})};
 h.view.result('scientific.policy.get',{project_id:'p',policy_digest:'policy-digest',policy:p});
 for(const id of ['a','b'])h.view.result('run.status',{backend_run_id:id,evaluations:p.rules.map(r=>({backend_run_id:id,node_id:'eval-'+r.id,output_port:'metrics',artifact_id:id+'-'+r.id,metric:r.metric}))});
 return h;
}
test('scientific comparison uses exact loaded policy and all actual per-rule evaluation selectors',()=>{
 const h=policyHarness();button(h.compare,'Compare selected runs').onclick();const p=h.sent.at(-1).payload;
 assert.equal(p.scientific_policy_digest,'policy-digest');assert.equal(p.scientific_evaluations.length,2);
 assert.equal(p.scientific_evaluations[1].baseline.artifact_id,'a-retention');assert.equal(p.scientific_evaluations[1].candidate.artifact_id,'b-retention');
 assert.match(h.compare.textContent,/Baseline a/);assert.match(h.compare.textContent,/Candidate b/);
});
test('custom policy selects exact actual native gate artifact and rejects its stale response',()=>{
 const h=policyHarness(true);button(h.compare,'Compare selected runs').onclick();assert.match(h.errors.at(-1),/native scientific gate/);
 const gateRun=all(h.compare).find(n=>n.tagName==='select'&&n.children.some(c=>c._text==='Choose completed gate run'));gateRun.value='b';gateRun.onchange();button(h.compare,'Load gate result artifacts').onclick();assert.equal(h.sent.at(-1).payload.include_binding_descriptors,true);
 const artifact={artifact_id:'gate-result',node_id:'gate',binding_descriptor:{producer_role:'scientific_gate',backend_run_id:'b',source_node_id:'gate',source_port:'result',artifact_id:'gate-result',content_sha256:'original'}};
 h.view.result('run.artifacts',{backend_run_id:'b',artifacts:[artifact]});let choice=all(h.compare).find(n=>n.tagName==='select'&&n.children.some(c=>c.value==='gate-result'));choice.value='gate-result';choice.onchange();button(h.compare,'Compare selected runs').onclick();const old=h.sent.at(-1).payload;
 assert.deepEqual(old.scientific_gate,{backend_run_id:'b',node_id:'gate',output_port:'result',artifact_id:'gate-result',content_sha256:'original'});
 h.view.result('run.artifacts',{backend_run_id:'b',artifacts:[{...artifact,binding_descriptor:{...artifact.binding_descriptor,content_sha256:'changed'}}]});h.view.result('run.compare',compared,old);assert.doesNotMatch(h.compare.textContent,/Compatible evidence/);
});
test('accepted scientific decision preserves policy, all rule evidence and custom gate selector',()=>{
 const h=policyHarness();button(h.compare,'Compare selected runs').onclick();const req=h.sent.at(-1).payload;
 const scientific_result={policy_digest:req.scientific_policy_digest,digest:'scientific-result',passed:true,native_verified:true,outcome:'passed',rules:req.scientific_evaluations.map(r=>({rule_id:r.rule_id,evidence:{baseline:{selector:r.baseline},candidate:{selector:r.candidate}}}))};
 h.view.result('run.compare',{...compared,evaluations:req.evaluations,scientific_result},req);
 const rationale=all(h.compare).find(n=>n.tagName==='textarea');rationale.value='All declared criteria passed';rationale.oninput();all(h.compare).find(n=>n.tagName==='form').onsubmit({preventDefault(){}});
 const rule=h.sent.at(-1).payload.selection_rule;assert.equal(rule.scientific_result_digest,'scientific-result');assert.equal(rule.scientific_policy_digest,'policy-digest');assert.deepEqual(rule.scientific_evaluations,req.scientific_evaluations);
});
test('policy changes invalidate previous comparison, and a late policy from another project is ignored',()=>{
 const h=policyHarness();button(h.compare,'Compare selected runs').onclick();const req=h.sent.at(-1).payload;
 const digest=all(h.compare).find(n=>n.tagName==='input'&&n.placeholder?.startsWith('Exact registered'));digest.value='replacement';digest.oninput();h.view.result('run.compare',compared,req);assert.doesNotMatch(h.compare.textContent,/Compatible evidence/);
 h.view.result('scientific.policy.get',{project_id:'other',policy_digest:'replacement',policy:{id:'foreign',version:'1',rules:[]}});assert.doesNotMatch(h.compare.textContent,/foreign/);
 button(h.compare,'Compare selected runs').onclick();assert.match(h.errors.at(-1),/Load the exact scientific policy/);
});
