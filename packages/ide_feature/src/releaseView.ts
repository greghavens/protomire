import {OperationSpec,RecordValue} from './types';
import {scientificDecisionFields} from './scientificWorkflow';
/** Views over the shared operation catalog and native records, never a run registry. */
export class ReleaseView {
  private snapshot:RecordValue={};private catalog:OperationSpec[]=[];private comparison?:RecordValue;private comparisonRequestKey?:string;
  private evaluationCatalogs=new Map<string,RecordValue[]>();private evaluationSelections=new Map<string,string>();
  private policyDigest='';private scientificPolicy?:RecordValue;private ruleSelections=new Map<string,string>();
  private gateRun='';private gateChoice='';private gateCatalogs=new Map<string,RecordValue[]>();
  private decisionDraft={hypothesis:'',outcome:'',rationale:''};private promotions=new Map<string,{decision:string;stage:string}>();
  private versions:RecordValue[]=[];private selected=new Set<string>();private metric='validation_loss';private direction='minimize';
  constructor(private compareRoot:HTMLElement,private versionsRoot:HTMLElement,private runExportRoot:HTMLElement,private send:(type:string,payload?:RecordValue)=>void,private report:(message:string)=>void){}
  private node<K extends keyof HTMLElementTagNameMap>(tag:K,text?:string){const node=this.compareRoot.ownerDocument.createElement(tag);if(text!==undefined)node.textContent=text;return node;}
  private button(text:string,action:()=>void,operation?:string){const button=this.node('button',text);button.type='button';button.onclick=action;if(operation&&!this.catalog.some(item=>item.name===operation&&item.authorized!==false))button.disabled=true;return button;}
  private field(text:string,input:HTMLElement){const label=this.node('label',text);label.append(input);return label;}
  private select(values:{value:string;label:string}[],value?:string){const input=this.node('select');for(const item of values){const option=this.node('option',item.label);option.value=item.value;input.append(option);}if(value!==undefined)input.value=value;return input;}
  private invoke(operation:string,payload:RecordValue){this.send('operation',{operation,payload});}
  private comparisonRequest(report=false):RecordValue|undefined{
    const fail=(message:string)=>{if(report)this.report(message);return undefined;};
    if(this.selected.size<2||!this.metric)return fail('Select two or more completed runs and a metric.');
    const selectors:RecordValue[]=[];let catalogsPresent=false;
    for(const id of this.selected){
      const values=(this.evaluationCatalogs.get(id)??[]).filter(item=>item.metric===this.metric);
      if(values.length)catalogsPresent=true;
      const chosen=this.evaluationSelections.get(id);
      const evaluation=chosen?values.find(item=>`${item.node_id}:${item.output_port}:${item.artifact_id}`===chosen):values.length===1?values[0]:undefined;
      if(values.length>1&&!evaluation)return fail('Choose one explicit evaluation artifact for each ambiguous metric.');
      if(evaluation)selectors.push(Object.fromEntries(['backend_run_id','node_id','output_port','artifact_id','metric'].map(key=>[key,evaluation[key]])));
    }
    if(catalogsPresent&&selectors.length!==this.selected.size)return fail('Load matching evaluation choices for every selected run.');
    const request:RecordValue={run_ids:[...this.selected],metric:this.metric,direction:this.direction,...(selectors.length?{evaluations:selectors}:{})};
    if(this.policyDigest){
      if(!this.scientificPolicy)return fail('Load the exact scientific policy before comparing.');
      if(this.selected.size!==2)return fail('A scientific policy compares exactly one baseline and one candidate, in the displayed order.');
      const ids=[...this.selected],bindings:RecordValue[]=[];
      for(const rule of this.scientificPolicy.rules){
        const binding:RecordValue={rule_id:rule.id};
        for(const [index,side] of ['baseline','candidate'].entries()){
          const values=(this.evaluationCatalogs.get(ids[index])??[]).filter(item=>item.metric===rule.metric);
          const chosen=this.ruleSelections.get(`${rule.id}:${ids[index]}`);
          const entry=chosen?values.find(item=>this.evaluationKey(item)===chosen):values.length===1?values[0]:undefined;
          if(!entry)return fail(`Choose one exact ${side} evaluation for policy rule ${rule.id}.`);
          binding[side]=Object.fromEntries(['backend_run_id','node_id','output_port','artifact_id','metric'].map(key=>[key,entry[key]]));
        }
        bindings.push(binding);
      }
      request.scientific_policy_digest=this.policyDigest;request.scientific_evaluations=bindings;
      if(this.scientificPolicy.evaluator){
        const artifact=(this.gateCatalogs.get(this.gateRun)??[]).find(item=>item.artifact_id===this.gateChoice);
        const descriptor=artifact?.binding_descriptor;
        if(!descriptor)return fail('Load and select the completed native scientific gate result.');
        request.scientific_gate={backend_run_id:this.gateRun,node_id:descriptor.source_node_id,output_port:descriptor.source_port,artifact_id:descriptor.artifact_id,content_sha256:descriptor.content_sha256};
      }
    }
    return request;
  }
  private evaluationKey(item:RecordValue){return `${item.node_id}:${item.output_port}:${item.artifact_id}`;}
  reset(){this.policyDigest='';this.scientificPolicy=undefined;this.ruleSelections.clear();this.gateRun='';this.gateChoice='';this.gateCatalogs.clear();this.snapshot={};this.comparison=undefined;this.comparisonRequestKey=undefined;this.versions=[];this.selected.clear();this.evaluationCatalogs.clear();this.evaluationSelections.clear();this.decisionDraft={hypothesis:'',outcome:'',rationale:''};this.promotions.clear();this.compareRoot.replaceChildren();this.versionsRoot.replaceChildren();this.runExportRoot.replaceChildren();}
  update(snapshot:RecordValue,catalog:OperationSpec[]){
    if(this.snapshot.project?.project_id!==snapshot.project?.project_id)this.reset();
    if(this.snapshot.project?.scientific_policy_digest!==snapshot.project?.scientific_policy_digest){this.policyDigest=snapshot.project?.scientific_policy_digest??'';this.scientificPolicy=undefined;this.ruleSelections.clear();this.comparison=undefined;}
    this.snapshot=snapshot;this.catalog=catalog;
    const runs=snapshot.runs??[];for(const run of runs)if(run.metrics?.evaluation_catalog)this.evaluationCatalogs.set(run.backend_run_id,run.metrics.evaluation_catalog);for(const selected of this.selected)if(!runs.some((run:RecordValue)=>run.backend_run_id===selected))this.selected.delete(selected);
    this.renderComparison();this.renderVersions();
  }
  result(operation:string,result:RecordValue,request:RecordValue={}){
    if(operation==='run.compare'){
      const current=this.comparisonRequest();if(!current||['metric','direction','run_ids','evaluations','scientific_policy_digest','scientific_evaluations','scientific_gate'].some(key=>JSON.stringify(request[key])!==JSON.stringify(current[key])))return;
      this.comparison=result;this.comparisonRequestKey=JSON.stringify(current);this.renderComparison();
    }
    if(operation==='run.status'&&result.backend_run_id){this.evaluationCatalogs.set(result.backend_run_id,result.metrics?.evaluation_catalog??result.evaluations??[]);this.renderComparison();}
    if(operation==='scientific.policy.get'&&result.project_id===this.snapshot.project?.project_id&&result.policy_digest===this.policyDigest){this.scientificPolicy=result.policy;this.comparison=undefined;this.renderComparison();}
    if(operation==='run.artifacts'&&result.backend_run_id===this.gateRun){this.gateCatalogs.set(this.gateRun,(result.artifacts??[]).filter((item:RecordValue)=>item.binding_descriptor?.producer_role==='scientific_gate'&&item.binding_descriptor?.source_port==='result'));this.renderComparison();}
    if(operation==='model.version.list'){this.versions=result.versions??[];this.renderVersions();}

  }
  run(run:RecordValue){
    this.runExportRoot.replaceChildren();const current=run.status;
    if(current?.backend_run_id&&current?.status==='succeeded')this.runExportRoot.append(this.button('Save model package…',()=>this.send('nativeSaveModel',{backendRunId:current.backend_run_id,modelVersionId:current.model_version_ref}),'model.export.list'));
  }
  private renderComparison(){
    if(this.comparison&&JSON.stringify(this.comparisonRequest())!==this.comparisonRequestKey)this.comparison=undefined;
    const root=this.compareRoot;root.replaceChildren(this.node('h3','Compare runs and record a decision'));
    const choices=this.node('fieldset');choices.append(this.node('legend','Choose at least two completed runs'));
    for(const run of this.snapshot.runs??[]){
      if(!run.backend_run_id)continue;const input=this.node('input');input.type='checkbox';input.value=run.backend_run_id;input.checked=this.selected.has(run.backend_run_id);input.disabled=run.status!=='succeeded';
      input.onchange=()=>{if(input.checked)this.selected.add(input.value);else this.selected.delete(input.value);this.comparison=undefined;this.renderComparison();};
      choices.append(this.field(`${run.status} · recipe r${run.recipe_revision} · ${run.backend_run_id}`,input));
      if(this.selected.has(run.backend_run_id)){
        const evaluations=(this.evaluationCatalogs.get(run.backend_run_id)??[]).filter(item=>item.metric===this.metric);
        if(evaluations.length){const picker=this.select([{value:'',label:evaluations.length>1?'Choose an evaluation artifact':'Use the only matching evaluation'},...evaluations.map(item=>({value:`${item.node_id}:${item.output_port}:${item.artifact_id}`,label:`${item.node_id} / ${item.output_port} · ${item.value} · ${item.artifact_id}`}))],this.evaluationSelections.get(run.backend_run_id)??'');picker.onchange=()=>{this.evaluationSelections.set(run.backend_run_id,picker.value);this.comparison=undefined;this.renderComparison();};choices.append(this.field('Evaluation for this run',picker));}
        choices.append(this.button('Load evaluation choices',()=>this.invoke('run.status',{backend_run_id:run.backend_run_id}),'run.status'));
      }
    }
    root.append(choices);
    const metric=this.node('input');metric.value=this.metric;metric.placeholder='Metric name, e.g. validation_loss';metric.oninput=()=>{this.metric=metric.value.trim();this.evaluationSelections.clear();this.comparison=undefined;root.querySelector('[data-comparison-result]')?.remove();};
    const direction=this.select([{value:'minimize',label:'Lower is better'},{value:'maximize',label:'Higher is better'}],this.direction);direction.onchange=()=>{this.direction=direction.value;this.comparison=undefined;this.renderComparison();};
    const compare=this.button('Compare selected runs',()=>{const request=this.comparisonRequest(true);if(request)this.invoke('run.compare',request);},'run.compare');
    root.append(this.field('Metric',metric),this.field('Direction',direction));this.renderScientificSelection(root);root.append(compare);
    if(!this.comparison)return;
    const result=this.comparison,details=this.node('section');details.dataset.comparisonResult='true';
    details.append(this.node('h4',result.compatible?`Compatible evidence · selected run ${result.selected_run_id}`:'Incompatible evidence · no winner'));
    for(const row of result.runs??[])details.append(this.node('p',`${row.backend_run_id}: ${row.value??'unavailable'}${row.delta_from_selected!==undefined?` (delta ${row.delta_from_selected})`:''}`));
    if(result.scientific_result)details.append(this.node('h4',`Scientific policy: ${result.scientific_result.outcome}`),this.node('pre',JSON.stringify(result.scientific_result,null,2)));
    for(const diagnostic of result.diagnostics??[])details.append(this.node('p',`${diagnostic.field}: ${diagnostic.message}`));
    const diff=this.node('details');diff.append(this.node('summary','Executed configuration differences'));
    for(const item of result.configuration_diffs??[])diff.append(this.node('h4',`${item.baseline_run_id} → ${item.backend_run_id} (r${item.recipe_revision})`),this.node('pre',item.diff||'No configuration difference.'));
    details.append(diff);
    const eligible=result.compatible&&(!(this.snapshot.project?.scientific_policy_digest||this.policyDigest)||(result.scientific_result?.passed===true&&result.scientific_result?.native_verified===true&&result.scientific_result?.policy_digest===(this.snapshot.project?.scientific_policy_digest||this.policyDigest)));
    const hypotheses=this.snapshot.hypotheses??[];
    if(!hypotheses.length)details.append(this.node('p','Create a measurable hypothesis in Research before recording a decision.'));
    else{
      const form=this.node('form'),hypothesis=this.select(hypotheses.map((item:RecordValue)=>({value:item.hypothesis_id,label:item.proposed_change??item.hypothesis_id}))),outcome=this.select((eligible?['accepted','rejected','inconclusive']:['inconclusive','rejected']).map(value=>({value,label:value}))),rationale=this.node('textarea');rationale.required=true;rationale.rows=3;
      if(hypotheses.some((item:RecordValue)=>item.hypothesis_id===this.decisionDraft.hypothesis))hypothesis.value=this.decisionDraft.hypothesis;if(['accepted','rejected','inconclusive'].includes(this.decisionDraft.outcome)&&(eligible||this.decisionDraft.outcome!=='accepted'))outcome.value=this.decisionDraft.outcome;rationale.value=this.decisionDraft.rationale;hypothesis.onchange=()=>{this.decisionDraft.hypothesis=hypothesis.value;};outcome.onchange=()=>{this.decisionDraft.outcome=outcome.value;};rationale.oninput=()=>{this.decisionDraft.rationale=rationale.value;};
      const submit=this.button('Record decision',()=>{},'decision.record');submit.type='submit';form.append(this.field('Hypothesis',hypothesis),this.field('Outcome',outcome),this.field('Rationale',rationale),submit);
      form.onsubmit=event=>{event.preventDefault();if(!rationale.value.trim())return;const executed=(result.run_provenance??[]).find((run:RecordValue)=>run.backend_run_id===result.selected_run_id)??result.run_provenance?.[0];if(!executed){this.report('The service did not return immutable run provenance. Compare again with a compatible service.');return;}this.invoke('decision.record',{recipe_id:executed.recipe_id,recipe_revision:executed.recipe_revision,hypothesis_id:hypothesis.value,run_ids:result.runs.map((run:RecordValue)=>run.backend_run_id),selection_rule:{metric:result.metric,direction:result.direction,comparison_contract:result.comparison_contract,...(result.evaluations?{evaluations:result.evaluations}:{}),...(result.comparison_digest?{comparison_digest:result.comparison_digest}:{}),...(result.scientific_result?.passed===true?scientificDecisionFields(result):{})},outcome:outcome.value,rationale:rationale.value.trim()});};details.append(form);
    }
    root.append(details);
  }
  private renderScientificSelection(root:HTMLElement){
    const box=this.node('fieldset');box.append(this.node('legend','Versioned scientific policy'));
    const digest=this.node('input');digest.value=this.policyDigest;digest.placeholder='Exact registered policy digest (optional unless active)';digest.oninput=()=>{this.policyDigest=digest.value.trim();this.scientificPolicy=undefined;this.ruleSelections.clear();this.comparison=undefined;root.querySelector('[data-comparison-result]')?.remove();};
    box.append(this.field('Policy digest',digest),this.button('Load scientific policy',()=>{if(this.policyDigest)this.invoke('scientific.policy.get',{policy_digest:this.policyDigest});else this.report('Choose an exact registered policy digest.');},'scientific.policy.get'));
    if(this.snapshot.project?.scientific_policy_digest)box.append(this.node('p',`Required active policy: ${this.snapshot.project.scientific_policy_digest}`));
    if(this.scientificPolicy){
      box.append(this.node('p',`${this.scientificPolicy.id} version ${this.scientificPolicy.version}. Baseline and candidate follow the selected run order below.`));
      for(const [index,id] of [...this.selected].entries())for(const rule of this.scientificPolicy.rules){
        const key=`${rule.id}:${id}`,values=(this.evaluationCatalogs.get(id)??[]).filter(item=>item.metric===rule.metric);
        const picker=this.select([{value:'',label:values.length===1?'Use the only matching evaluation':'Choose exact evaluation'},...values.map(item=>({value:this.evaluationKey(item),label:`${item.node_id} / ${item.output_port} · ${item.artifact_id}`}))],this.ruleSelections.get(key)??'');
        picker.onchange=()=>{this.ruleSelections.set(key,picker.value);this.comparison=undefined;this.renderComparison();};box.append(this.field(`${index===0?'Baseline':'Candidate'} ${id} · rule ${rule.id} (${rule.metric})`,picker));
      }
      if(this.scientificPolicy.evaluator){
        const run=this.select([{value:'',label:'Choose completed gate run'},...(this.snapshot.runs??[]).filter((item:RecordValue)=>item.status==='succeeded').map((item:RecordValue)=>({value:item.backend_run_id,label:item.backend_run_id}))],this.gateRun);
        run.onchange=()=>{this.gateRun=run.value;this.gateChoice='';this.comparison=undefined;this.renderComparison();};
        box.append(this.field('Native scientific gate run',run),this.button('Load gate result artifacts',()=>{if(this.gateRun)this.invoke('run.artifacts',{backend_run_id:this.gateRun,include_binding_descriptors:true});},'run.artifacts'));
        const artifacts=this.gateCatalogs.get(this.gateRun)??[];
        const picker=this.select([{value:'',label:'Choose exact verified gate artifact'},...artifacts.map(item=>({value:item.artifact_id,label:`${item.node_id} / result · ${item.artifact_id}`}))],this.gateChoice);
        picker.onchange=()=>{this.gateChoice=picker.value;this.comparison=undefined;this.renderComparison();};box.append(this.field('Native gate result',picker));
      }
    }
    root.append(box);
  }
  private renderVersions(){
    const root=this.versionsRoot;root.replaceChildren();
    if(!this.versions.length)root.append(this.node('p','Load native versions to inspect lineage, promote a measured decision, or save an existing model export.'));
    const accepted=(this.snapshot.decisions??[]).filter((item:RecordValue)=>item.outcome==='accepted');
    for(const version of this.versions){
      const id=version.model_version_id??version.model_version_ref,card=this.node('article');card.dataset.versionId=id;card.append(this.node('h3',`${version.name??id} · version ${version.number??id}`),this.node('p',`Stage: ${version.stage??'none'}${this.snapshot.project?.active_model_version_ref===id?' · active version':''}`));
      card.append(this.button('Inspect lineage',()=>this.invoke('model.version.lineage',{model_version_id:id}),'model.version.lineage'),this.button('Save model package…',()=>this.send('nativeSaveModel',{modelVersionId:id}),'model.export.list'));
      if(accepted.length){
        const decision=this.select(accepted.map((item:RecordValue)=>({value:item.decision_id,label:`${item.selection_rule.metric} ${item.selection_rule.direction} · ${item.rationale}`}))),stage=this.select([{value:'staging',label:'Staging'},{value:'production',label:'Production (local stage only)'}]);
        const remembered=this.promotions.get(id);if(accepted.some((item:RecordValue)=>item.decision_id===remembered?.decision))decision.value=remembered!.decision;if(remembered)stage.value=remembered.stage;const remember=()=>this.promotions.set(id,{decision:decision.value,stage:stage.value});decision.onchange=remember;stage.onchange=remember;
        const promote=this.button('Promote by selected decision',()=>this.invoke('model.version.promote',{model_version_id:id,decision_id:decision.value,stage:stage.value,expected_revision:this.snapshot.project.revision}),'model.version.promote');
        card.append(this.field('Accepted decision',decision),this.field('Native model stage',stage),promote,this.node('small','The service rechecks evidence and winner identity. A stale project revision must be refreshed.'));
      }else card.append(this.node('p','Record an accepted, measured decision in Runs before promotion.'));
      root.append(card);
    }
  }
}
