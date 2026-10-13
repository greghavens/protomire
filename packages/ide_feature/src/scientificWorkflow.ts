import {Recipe, RecordValue} from './types';

export type ScientificWorkflowAction=(operation:string,payload:RecordValue)=>void;
export interface ScientificWorkflowState {
  project:RecordValue;
  recipe:Recipe;
  runs:RecordValue[];
  comparison?:RecordValue;
  authorizedOperations?:string[];
}

/** The exact service payload also goes over MCP. No thresholds are inferred here. */
export function smokeRequest(recipe:Recipe,limits:RecordValue[]):RecordValue {
  if(!limits.length)throw new Error('Choose at least one declared component budget.');
  const seen=new Set<string>();
  for(const item of limits){
    const key=`${item.node_id}:${item.limit_id}`;
    if(seen.has(key)||!Number.isSafeInteger(item.maximum)||item.maximum<1)throw new Error('Smoke limits must be distinct positive integers.');
    seen.add(key);
  }
  return {project_id:recipe.project_id,recipe_id:recipe.recipe_id,expected_revision:recipe.revision,limits:structuredClone(limits)};
}

export function scientificDecisionFields(comparison:RecordValue):RecordValue {
  const gate=comparison.scientific_result;
  if(comparison.compatible!==true||gate?.passed!==true||gate.native_verified!==true||!gate.digest||!comparison.comparison_digest)
    throw new Error('A decision requires a compatible comparison and passing native scientific evidence.');
  return {metric:comparison.metric,direction:comparison.direction,comparison_digest:comparison.comparison_digest,
    comparison_contract:structuredClone(comparison.comparison_contract),evaluations:structuredClone(comparison.evaluations),
    scientific_policy_digest:gate.policy_digest,scientific_result_digest:gate.digest,
    ...(gate.scientific_gate?{scientific_gate:structuredClone(gate.scientific_gate)}:{}),
    scientific_evaluations:gate.rules.map((rule:RecordValue)=>({rule_id:rule.rule_id,
      baseline:structuredClone(rule.evidence.baseline.selector),candidate:structuredClone(rule.evidence.candidate.selector)}))};
}

/** Native webview action asset. The host supplies its ordinary authorized operation dispatcher. */
export function renderScientificWorkflow(root:HTMLElement,state:ScientificWorkflowState,invoke:ScientificWorkflowAction):void {
  const doc=root.ownerDocument;root.replaceChildren();
  const allowed=(op:string)=>state.authorizedOperations===undefined||state.authorizedOperations.includes(op);
  const guarded=(op:string,payload:RecordValue)=>{if(!allowed(op))throw new Error('This operation is not authorized.');invoke(op,payload);};
  const heading=doc.createElement('h3');heading.textContent='Scientific workflows';root.append(heading);
  const notice=doc.createElement('p');notice.textContent='Smoke runs are non-production. Policies need explicit metrics, thresholds, uncertainty methods and evidence. Registering a policy does not activate it.';root.append(notice);
  const budgets:RecordValue[]=[];
  for(const node of state.recipe.nodes){
    const component=(node as RecordValue).component;
    for(const limit of component?.capabilities?.smoke_budget?.limits??[]){
      const row=doc.createElement('label');row.textContent=`${node.id}: ${limit.id} (${limit.unit}) `;
      const selected=doc.createElement('input');selected.type='checkbox';selected.setAttribute('aria-label',`Use ${node.id} ${limit.id}`);
      const maximum=doc.createElement('input');maximum.type='number';maximum.min=String(limit.minimum);maximum.step='1';maximum.setAttribute('aria-label',`Maximum ${node.id} ${limit.id}`);
      row.append(selected,maximum);root.append(row);budgets.push({node_id:node.id,limit_id:limit.id,unit:limit.unit,selected,maximum});
    }
  }
  const error=doc.createElement('p');error.setAttribute('role','alert');
  const smoke=doc.createElement('button');smoke.type='button';smoke.textContent='Plan smoke run';smoke.disabled=budgets.length===0||!allowed('run.smoke.plan');
  smoke.addEventListener('click',()=>{try{const limits=budgets.filter(b=>b.selected.checked).map(b=>({node_id:b.node_id,limit_id:b.limit_id,unit:b.unit,maximum:Number(b.maximum.value)}));guarded('run.smoke.plan',smokeRequest(state.recipe,limits));error.textContent='';}catch(e){error.textContent=e instanceof Error?e.message:String(e);}});root.append(smoke);
  const save=doc.createElement('button');save.type='button';save.textContent='Save non-production smoke revision';save.disabled=budgets.length===0||!allowed('recipe.smoke.prepare');save.addEventListener('click',()=>{try{const limits=budgets.filter(b=>b.selected.checked).map(b=>({node_id:b.node_id,limit_id:b.limit_id,unit:b.unit,maximum:Number(b.maximum.value)}));guarded('recipe.smoke.prepare',smokeRequest(state.recipe,limits));error.textContent='';}catch(e){error.textContent=e instanceof Error?e.message:String(e);}});root.append(save);
  if(!budgets.length){const text=doc.createElement('p');text.textContent='No component declares a supported smoke budget. Add a reviewed budget adapter or declaration before planning.';root.append(text);}
  const methods=doc.createElement('button');methods.type='button';methods.textContent='Inspect scientific methods';methods.disabled=!allowed('scientific.methods');methods.addEventListener('click',()=>guarded('scientific.methods',{project_id:state.project.project_id}));root.append(methods);
  const policy=doc.createElement('textarea');policy.setAttribute('aria-label','Versioned scientific policy JSON');root.append(policy);
  const register=doc.createElement('button');register.type='button';register.textContent='Register policy';register.disabled=!allowed('scientific.policy.register');register.addEventListener('click',()=>{try{const value=JSON.parse(policy.value);guarded('scientific.policy.register',{project_id:state.project.project_id,policy:value});error.textContent='';}catch(e){error.textContent=e instanceof Error?e.message:String(e);}});root.append(register);
  const active=doc.createElement('input');active.setAttribute('aria-label','Policy digest to activate');root.append(active);
  const activate=doc.createElement('button');activate.type='button';activate.textContent='Require this policy for decisions';activate.disabled=!allowed('scientific.policy.activate');activate.addEventListener('click',()=>{try{if(!/^[0-9a-f]{64}$/.test(active.value.trim()))throw new Error('Enter an exact registered policy digest.');guarded('scientific.policy.activate',{project_id:state.project.project_id,policy_digest:active.value.trim(),expected_revision:state.project.revision});error.textContent='';}catch(e){error.textContent=e instanceof Error?e.message:String(e);}});root.append(activate);
  const params=doc.createElement('textarea');params.setAttribute('aria-label','Known-SD sample design parameters JSON');root.append(params);
  const alpha=doc.createElement('input');alpha.type='number';alpha.step='any';alpha.setAttribute('aria-label','Per-rule one-sided error probability');root.append(alpha);
  const count=doc.createElement('input');count.type='number';count.min='0';count.step='1';count.setAttribute('aria-label','Available independent pairs');root.append(count);
  const sample=doc.createElement('button');sample.type='button';sample.textContent='Plan known-SD paired sample adequacy';sample.disabled=!allowed('scientific.sample.plan');sample.addEventListener('click',()=>{try{if(!alpha.value||!count.value)throw new Error('Declare the error probability and available independent pairs.');guarded('scientific.sample.plan',{project_id:state.project.project_id,method:'normal-known-sd-one-sided/v1',parameters:JSON.parse(params.value),alpha:Number(alpha.value),available_pairs:Number(count.value)});error.textContent='';}catch(e){error.textContent=e instanceof Error?e.message:String(e);}});root.append(sample);
  const assumptions=doc.createElement('p');assumptions.textContent='Sample planning requires explicit effect size, known difference SD, target power, variance source and independence/normality assumptions. It does not validate those assumptions or repeated holdout use.';root.append(assumptions);
  if(state.comparison){
    const result=state.comparison.scientific_result;
    const outcome=doc.createElement('p');outcome.textContent=result?`Scientific outcome: ${result.outcome}. Policy ${result.policy.id} version ${result.policy.version}.`:'Comparison has no scientific policy result.';root.append(outcome);
    const details=doc.createElement('pre');details.textContent=JSON.stringify({run_provenance:state.comparison.run_provenance,configuration_diffs:state.comparison.configuration_diffs,scientific_result:result},null,2);root.append(details);
  }
  root.append(error);
}
