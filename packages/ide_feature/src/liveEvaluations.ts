import { RecordValue } from './types';

export interface EvaluationComparison extends RecordValue {
  metric:string;
  compatible:boolean;
  direction?:'minimize'|'maximize';
  delta_from_previous?:number;
  previous_evaluation_id?:string;
  diagnostics?:RecordValue[];
}
export interface CheckpointEvaluation extends RecordValue {
  evaluation_id:string;
  checkpoint_id:string;
  step:number;
  status:'queued'|'running'|'completed'|'failed'|'canceled';
  metrics?:Record<string,number>;
  comparisons?:Record<string,EvaluationComparison>;
}
export interface EvaluationEvent extends RecordValue {
  cursor:number;
  event_id:string;
  kind:string;
  evaluation_id?:string;
  checkpoint_id?:string;
  step?:number;
  comparisons?:Record<string,EvaluationComparison>;
}
export interface EvaluationPage extends RecordValue {
  evaluations:CheckpointEvaluation[];
  checkpoints?:RecordValue[];
  summary?:Record<string,number>;
}

export function evaluationComparisons(value:unknown):EvaluationComparison[] {
  if(!value||typeof value!=='object'||Array.isArray(value))return [];
  return Object.values(value).filter((entry):entry is EvaluationComparison=>Boolean(entry)&&typeof entry==='object'&&typeof (entry as EvaluationComparison).metric==='string');
}

export function metricNumber(value:unknown):string {
  if(typeof value!=='number'||!Number.isFinite(value))return 'Unavailable';
  return new Intl.NumberFormat('en',{maximumSignificantDigits:6}).format(value);
}
export function comparisonLabel(comparison:EvaluationComparison):string {
  if(comparison.compatible!==true)return `Not comparable: ${(comparison.diagnostics??[]).map(d=>d.message).filter(Boolean).join('; ')||'Evaluation identities do not match.'}`;
  if(typeof comparison.delta_from_previous!=='number'||!Number.isFinite(comparison.delta_from_previous))return 'Compatible evaluation; no previous checkpoint delta.';
  const delta=comparison.delta_from_previous;
  const direction=comparison.direction;
  if(direction!=='minimize'&&direction!=='maximize')return 'Metric direction unavailable; no improvement claim.';
  const movement=delta===0?'unchanged':(direction==='minimize'?delta<0:delta>0)?'improved':'regressed';
  return `${movement} · ${delta>0?'+':''}${metricNumber(delta)} from previous compatible checkpoint`;
}

/** Render server-owned evaluation/comparison projections. No identity is inferred here. */
export function renderLiveEvaluations(container:HTMLElement, page:EvaluationPage|undefined, runStatus?:string):void {
  const doc=container.ownerDocument;
  const element=<K extends keyof HTMLElementTagNameMap>(tag:K,text?:string,className?:string)=>{const el=doc.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;};
  container.replaceChildren();
  const title=element('div',undefined,'toolbar');title.append(element('h3','Live checkpoint evaluations'));
  const active=['queued','submitted','pending','running','cancel_requested','cancel_pending'].includes(runStatus??'');
  title.append(element('span',active?'Training active · updates every 4 seconds':'Saved evaluation history','pill'));
  container.append(title,element('p','Results are recorded as checkpoints finish evaluating, while training can continue. Comparisons and trends use only service-verified compatible evidence.','muted'));
  if(!page||page.unavailable){container.append(element('p',page?.unavailable?`Evaluation stream unavailable: ${page.unavailable}`:'Select a run to inspect checkpoint evaluations.','muted'));return;}
  const evaluations=Array.isArray(page.evaluations)?page.evaluations:[];
  if(!evaluations.length){container.append(element('p',active?'Waiting for the first checkpoint. Training does not need to finish before results appear.':'No live checkpoint evaluations recorded for this run.','muted'));return;}
  const summary=element('div',undefined,'evaluation-counts');for(const status of ['queued','running','completed','failed','canceled']){const count=evaluations.filter(e=>e.status===status).length;summary.append(element('span',`${count} ${status}`,'pill'));}container.append(summary);
  const list=element('div',undefined,'checkpoint-list');
  for(const evaluation of [...evaluations].sort((a,b)=>b.step-a.step||a.evaluation_id.localeCompare(b.evaluation_id))){
    const card=element('article',undefined,`checkpoint-card evaluation-${evaluation.status}`);card.dataset.evaluationId=evaluation.evaluation_id;
    const heading=element('div',undefined,'toolbar');heading.append(element('h4',`Step ${evaluation.step}`),element('span',evaluation.status,'pill'));card.append(heading);
    card.append(element('p',`Checkpoint ${evaluation.checkpoint_id}`,'mono checkpoint-identity'));
    const meta=element('p',`Evaluation ${evaluation.evaluation_id}${evaluation.node_id?` · ${evaluation.node_id}`:''}`,'muted');card.append(meta);
    const metrics=element('dl',undefined,'evaluation-metrics');
    for(const [name,value]of Object.entries(evaluation.metrics??{})){if(typeof value!=='number'||!Number.isFinite(value))continue;metrics.append(element('dt',name),element('dd',metricNumber(value)));}
    if(metrics.childElementCount)card.append(metrics);
    if(evaluation.status==='queued'||evaluation.status==='running')card.append(element('p',evaluation.status==='queued'?'Checkpoint saved. Evaluation is queued.':'Evaluation in progress. Training may continue.','muted'));
    if(evaluation.status==='failed')card.append(element('p',typeof evaluation.error==='string'?evaluation.error:JSON.stringify(evaluation.error??'Evaluation failed. Training status is tracked separately.'),'evaluation-error'));
    if(evaluation.status==='canceled')card.append(element('p','Evaluation canceled. No completed result is claimed.','muted'));
    const comparisons=evaluationComparisons(evaluation.comparisons);
    for(const comparison of comparisons)card.append(element('p',`${comparison.metric}: ${comparisonLabel(comparison)}`,comparison.compatible?'comparison':'comparison warning'));
    if(evaluation.status==='completed'&&!comparisons.length)card.append(element('p','No verified checkpoint comparison yet. Metric values are preserved without an improvement claim.','muted'));
    const details=element('details');details.append(element('summary','Evaluation identity and provenance'),element('pre',JSON.stringify({checkpoint_digest:evaluation.checkpoint_digest,evaluation_records:evaluation.evaluation_records,comparisons},null,2)));card.append(details);list.append(card);
  }
  container.append(list);
  const trends=element('section',undefined,'evaluation-trends');trends.append(element('h4','Compatible checkpoint trends'));
  let pairs=0;
  const ordered=[...evaluations].sort((a,b)=>a.step-b.step);
  for(const current of ordered){
    if(current.status!=='completed')continue;
    for(const comparison of evaluationComparisons(current.comparisons)){
      if(comparison.compatible!==true)continue;
      const previous=evaluations.find(e=>e.evaluation_id===comparison.previous_evaluation_id&&e.status==='completed');
      if(!previous)continue;
      const metric=comparison.metric;
      const row=element('p');row.append(element('strong',`${metric}: `));
      row.append(doc.createTextNode(`step ${previous.step} = ${metricNumber(previous.metrics?.[metric])} → step ${current.step} = ${metricNumber(current.metrics?.[metric])}`));
      trends.append(row);pairs++;
    }
  }
  if(!pairs)trends.append(element('p','A trend appears after the service verifies matching evaluation identities. Incompatible checkpoints are never joined.','muted'));
  container.append(trends);
}
