import { RecordValue } from './types';
import { checkpointCurves, curveCoordinates, CheckpointCurve, EVALUATION_WINDOW } from './checkpointCurves';
export { checkpointCurves, curveCoordinates, EVALUATION_WINDOW, MAX_CURVES } from './checkpointCurves';

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
function comparisonDiagnostics(comparison:EvaluationComparison):RecordValue[] {return Array.isArray(comparison.diagnostics)?comparison.diagnostics.slice(0,8).filter(item=>item&&typeof item==='object'):[];}
export function comparisonLabel(comparison:EvaluationComparison):string {
  if(comparison.compatible!==true)return `Not comparable: ${comparisonDiagnostics(comparison).map(d=>text(d.message)).filter(Boolean).join('; ')||'Evaluation identities do not match.'}`;
  if(typeof comparison.delta_from_previous!=='number'||!Number.isFinite(comparison.delta_from_previous))return 'Compatible evaluation; no previous checkpoint delta.';
  const delta=comparison.delta_from_previous;
  const direction=comparison.direction;
  if(direction!=='minimize'&&direction!=='maximize')return 'Metric direction unavailable; no improvement claim.';
  const movement=delta===0?'unchanged':(direction==='minimize'?delta<0:delta>0)?'improved':'regressed';
  return `${movement} · ${delta>0?'+':''}${metricNumber(delta)} from previous compatible checkpoint`;
}

interface ChartView {
  root:HTMLElement;plot:HTMLElement;table:HTMLElement;caption:HTMLElement;
  node:string;metric:string;evaluationIds:Set<string>;
}
interface LiveView {
  scope:string;
  badge:HTMLElement;
  summary:HTMLElement;
  notice:HTMLElement;
  charts:HTMLElement;
  cards:HTMLElement;
  chartNodes:Map<string,ChartView>;
  cardNodes:Map<string,{root:HTMLElement;body:HTMLElement;provenance:HTMLElement}>;
}
const views=new WeakMap<HTMLElement,LiveView>();
const svgNS='http://www.w3.org/2000/svg';
function axisNumber(value:number):string {const magnitude=Math.abs(value);return magnitude!==0&&(magnitude<0.0001||magnitude>=1e7)?value.toExponential(3):metricNumber(value);}
function text(value:unknown):string {return typeof value==='string'?value.slice(0,500):'';}
function identityLabel(curve:CheckpointCurve):string {
  const record=curve.points[0].evaluation.evaluation_records?.[curve.metric];
  const dataset=record?.dataset,split=record?.split,protocol=record?.protocol;
  return [`Checkpoint node: ${curve.node||'not recorded'}`,`Dataset: ${text(dataset?.id)||'not recorded'}${dataset?.revision?` @ ${text(dataset.revision)}`:''}`,`Split: ${text(split?.id)||'not recorded'}`,`Protocol: ${text(protocol?.implementation)||'not recorded'}${protocol?.version?` @ ${text(protocol.version)}`:''}`].join(' · ');
}
/** Reconcile without detaching unchanged nodes, preserving focused disclosures. */
function reconcile(parent:HTMLElement,children:HTMLElement[]):void {
  const focused=parent.ownerDocument.activeElement;
  const restore=focused!==null&&parent.contains(focused);
  const previousIndex=restore?[...parent.children].findIndex(child=>child.contains(focused)):-1;
  for(let i=0;i<children.length;i++)if(parent.children[i]!==children[i])parent.insertBefore(children[i],parent.children[i]??null);
  const retained=new Set(children);for(const child of [...parent.children])if(!retained.has(child as HTMLElement))child.remove();
  if(restore&&focused){
    if(parent.contains(focused)){
      if(parent.ownerDocument.activeElement!==focused)(focused as HTMLElement).focus({preventScroll:true});
    }else{
      // The focused checkpoint left the bounded window. Move to a retained
      // disclosure at the nearest position instead of dropping focus to body.
      const sibling=children[Math.min(Math.max(0,previousIndex),children.length-1)];
      const fallback=sibling?.querySelector<HTMLElement>('summary')??parent;
      if(fallback===parent)parent.tabIndex=-1;
      fallback.focus({preventScroll:true});
    }
  }
}
function renderCurve(plot:HTMLElement,table:HTMLElement,curve:CheckpointCurve):void {
  const doc=plot.ownerDocument;
  const svg=(tag:string,attrs:Record<string,string|number>={},label?:string)=>{const el=doc.createElementNS(svgNS,tag);for(const [key,value]of Object.entries(attrs))el.setAttribute(key,String(value));if(label!==undefined)el.textContent=label;return el;};
  const {x,y,minStep,maxStep,minValue,maxValue}=curveCoordinates(curve);
  const graphic=svg('svg',{viewBox:'0 0 660 254',role:'img','aria-label':`${curve.metric} checkpoint evaluation by training step. ${curve.points.length} observed values. ${curve.gaps.length} checkpoints without a completed metric. Exact values follow in the data table.`,class:'checkpoint-curve'});
  graphic.append(svg('title',{},`${curve.metric}: checkpoint evaluation values`),svg('desc',{},'Dots are measured evaluations, never training loss. Lines join only service-verified compatible measurements with no missing checkpoint in between. State labels below the axis have no metric value.'));
  for(const value of minValue===maxValue?[minValue]:[minValue,maxValue])graphic.append(svg('line',{x1:72,x2:612,y1:y(value),y2:y(value),class:'curve-grid'}),svg('text',{x:64,y:y(value)+4,'text-anchor':'end',class:'curve-tick'},axisNumber(value)));
  graphic.append(svg('line',{x1:72,x2:612,y1:202,y2:202,class:'curve-axis'}));
  for(const step of minStep===maxStep?[minStep]:[minStep,maxStep])graphic.append(svg('text',{x:x(step),y:218,'text-anchor':'middle',class:'curve-tick'},String(step)));
  graphic.append(svg('text',{x:342,y:248,'text-anchor':'middle',class:'curve-tick'},'Training step (checkpoint evaluation)'));
  for(const [a,b] of curve.links)graphic.append(svg('line',{x1:x(a.evaluation.step),y1:y(a.value),x2:x(b.evaluation.step),y2:y(b.value),class:'curve-link'}));
  for(const point of curve.points){const dot=svg('circle',{cx:x(point.evaluation.step),cy:y(point.value),r:4,class:'curve-point'});dot.append(svg('title',{},`Step ${point.evaluation.step}: ${point.value} · Evaluation ${point.evaluation.evaluation_id}`));graphic.append(dot);}
  for(const gap of curve.gaps){const label=gap.status==='completed'?'missing metric':gap.status;const marker=svg('text',{x:x(gap.step),y:235,'text-anchor':'middle',class:`curve-gap curve-${gap.status}`},gap.status==='failed'?'×':gap.status==='canceled'?'−':'○');marker.append(svg('title',{},`Step ${gap.step}: ${label} · Evaluation ${gap.evaluation_id}`));graphic.append(marker);}
  plot.replaceChildren(graphic);
  const element=(tag:string,value?:string)=>{const el=doc.createElement(tag);if(value!==undefined)el.textContent=value;return el;};
  const data=element('table');data.className='curve-values';data.append(element('caption',`${curve.metric}: observed checkpoint evaluation values and gaps`));
  const header=element('tr');for(const label of ['Step','Value','State','Evaluation','Checkpoint']){const th=element('th',label);th.setAttribute('scope','col');header.append(th);}const head=element('thead');head.append(header);data.append(head);
  const body=element('tbody');
  const rows=[...curve.points.map(point=>({row:point.evaluation,value:String(point.value)})),...curve.gaps.map(row=>({row,value:'Unavailable'}))].sort((a,b)=>a.row.step-b.row.step||a.row.evaluation_id.localeCompare(b.row.evaluation_id));
  for(const {row,value}of rows){const tr=element('tr');for(const cell of [String(row.step),value,row.status==='completed'&&value==='Unavailable'?'missing metric':row.status,row.evaluation_id,row.checkpoint_id])tr.append(element('td',cell));body.append(tr);}data.append(body);table.replaceChildren(data);
}

/** Render durable evaluation evidence in Runs; preserve controls during polling. */
export function renderLiveEvaluations(container:HTMLElement, page:EvaluationPage|undefined, runStatus?:string, scope=''):void {
  const doc=container.ownerDocument;
  const element=<K extends keyof HTMLElementTagNameMap>(tag:K,value?:string,className?:string)=>{const el=doc.createElement(tag);if(value!==undefined)el.textContent=value;if(className)el.className=className;return el;};
  let view=views.get(container);
  if(!view||view.scope!==scope){
    const title=element('div',undefined,'toolbar'),badge=element('span',undefined,'pill');title.append(element('h3','Live checkpoint evaluations'),badge);
    const summary=element('div',undefined,'evaluation-counts'),notice=element('p',undefined,'muted'),charts=element('section',undefined,'evaluation-trends'),cards=element('div',undefined,'checkpoint-list');
    container.replaceChildren(title,element('p','Checkpoint evaluation metrics by training step. These are observed evaluation results, not training-loss history. Training can continue while evaluations finish.','muted'),summary,notice,charts,cards);
    view={scope,badge,summary,notice,charts,cards,chartNodes:new Map(),cardNodes:new Map()};views.set(container,view);
  }
  const active=['queued','submitted','pending','running','cancel_requested','cancel_pending'].includes(runStatus??'');
  view.badge.textContent=active?'Training active · updates every 4 seconds':'Saved evaluation history';
  const clear=()=>{view!.charts.replaceChildren();view!.cards.replaceChildren();view!.summary.replaceChildren();view!.chartNodes.clear();view!.cardNodes.clear();};
  if(!page||page.unavailable){clear();view.notice.textContent=page?.unavailable?`Evaluation stream unavailable: ${text(page.unavailable)}`:'Select a run to inspect checkpoint evaluations.';return;}
  const input=Array.isArray(page.evaluations)?page.evaluations:[],projection=checkpointCurves(input),evaluations=projection.evaluations;
  if(!evaluations.length){clear();view.notice.textContent=input.length?'No evaluation records with valid unique identities and training steps are available.':active?'Waiting for the first checkpoint. Training does not need to finish before results appear.':'No live checkpoint evaluations recorded for this run.';return;}
  const summary=element('div');for(const status of ['queued','running','completed','failed','canceled']){const count=typeof page.summary?.[status]==='number'?page.summary[status]:evaluations.filter(e=>e.status===status).length;summary.append(element('span',`${count} ${status}`,'pill'));}view.summary.replaceChildren(summary);
  view.notice.textContent=[`Dots are observed values. Lines require service-verified matching identities. Failed, canceled, pending and missing values leave gaps; no values are interpolated.`,page.truncated||projection.omittedEvaluations?`Showing at most the latest ${EVALUATION_WINDOW} evaluation records. Older durable history is retained.`:'',projection.omittedCurves?`Showing ${projection.curves.length} most recent metric series; ${projection.omittedCurves} other series are omitted from charts.`:'','Charts and cards show up to 32 metrics per evaluation. ○ queued/running/missing · × failed · − canceled'].filter(Boolean).join(' ');
  const chartNodes:HTMLElement[]=[];
  const previousCharts=[...view.chartNodes.values()],retainedCharts=new Set<ChartView>();
  const nextChartNodes=new Map<string,ChartView>();
  for(const curve of projection.curves){
    let chart=view.chartNodes.get(curve.key);
    if(chart&&retainedCharts.has(chart))chart=undefined;
    if(!chart){
      // Component roots change when the oldest observation leaves the window.
      // Reuse only a same-node/metric view with retained observation identity;
      // this preserves UI state, never changes service-verified graph edges.
      let overlap=0;
      for(const candidate of previousCharts){
        if(retainedCharts.has(candidate)||candidate.node!==curve.node||candidate.metric!==curve.metric)continue;
        const count=curve.points.filter(point=>candidate.evaluationIds.has(point.evaluation.evaluation_id)).length;
        if(count>overlap){chart=candidate;overlap=count;}
      }
    }
    if(!chart){const root=element('article',undefined,'evaluation-curve'),heading=element('h4',`${curve.metric} · ${curve.node||'unknown checkpoint node'}`),caption=element('p',undefined,'muted'),plot=element('div'),details=element('details'),table=element('div',undefined,'curve-table-scroll');details.append(element('summary','Exact values and evaluation identities'),table);root.append(heading,caption,plot,details);chart={root,caption,plot,table,node:curve.node,metric:curve.metric,evaluationIds:new Set()};}
    chart.evaluationIds=new Set(curve.points.map(point=>point.evaluation.evaluation_id));
    retainedCharts.add(chart);nextChartNodes.set(curve.key,chart);
    chart.caption.textContent=`${identityLabel(curve)}. ${curve.points.length===1?'Isolated measurement; no verified comparison line.':`${curve.points.length} measurements in this service-verified compatible series.`}`;
    renderCurve(chart.plot,chart.table,curve);chartNodes.push(chart.root);
  }
  if(!chartNodes.length)chartNodes.push(element('p','A checkpoint evaluation chart appears when a completed finite metric is recorded. Queued and failed evaluations have no plotted value.','muted'));
  reconcile(view.charts,chartNodes);view.chartNodes=nextChartNodes;
  const cards:HTMLElement[]=[];
  for(const evaluation of [...evaluations].reverse()){
    let entry=view.cardNodes.get(evaluation.evaluation_id);
    if(!entry){const root=element('article'),body=element('div'),details=element('details'),provenance=element('pre');root.dataset.evaluationId=evaluation.evaluation_id;details.append(element('summary','Evaluation identity and provenance summary'),provenance);root.append(body,details);entry={root,body,provenance};view.cardNodes.set(evaluation.evaluation_id,entry);}
    const {root:card,body,provenance}=entry;card.className=`checkpoint-card evaluation-${evaluation.status}`;
    const heading=element('div',undefined,'toolbar');heading.append(element('h4',`Step ${evaluation.step}`),element('span',evaluation.status,'pill'));body.replaceChildren(heading,element('p',`Checkpoint ${evaluation.checkpoint_id}`,'mono checkpoint-identity'),element('p',`Evaluation ${evaluation.evaluation_id}${evaluation.node_id?` · ${evaluation.node_id}`:''}`,'muted'));
    const metrics=element('dl',undefined,'evaluation-metrics');for(const [name,value]of Object.entries(evaluation.metrics??{}).slice(0,32)){if(typeof value!=='number'||!Number.isFinite(value))continue;metrics.append(element('dt',name),element('dd',metricNumber(value)));}if(metrics.childElementCount)body.append(metrics);
    const familyReport=evaluation.family_evaluation;
    if(familyReport&&typeof familyReport==='object'&&familyReport.families&&typeof familyReport.families==='object'){
      const familySection=element('section',undefined,'family-evaluation-results');
      familySection.append(element('h5','Functionality families'),element('p','Support and measured results for this checkpoint. Availability is not a quality-gate pass.','muted'));
      const families=Object.entries(familyReport.families);
      for(const [name,raw]of families.slice(0,256)){
        if(!raw||typeof raw!=='object')continue;
        const family=raw as RecordValue;
        const row=element('div',undefined,'family-evaluation-row');
        row.append(element('strong',text(name)),element('p',`${family.status==='available'?'Available':'Unavailable'} · ${metricNumber(family.support)} evaluated samples; minimum ${metricNumber(family.minimum_support)}.`));
        if(family.status!=='available')row.append(element('p','Required family evidence is missing or undersized. It must not be treated as a passing result.','evaluation-error'));
        const entries=Object.entries(family.metric_ids??{});
        for(const [metric,id]of entries.slice(0,32))row.append(element('p',`${text(metric)}: ${typeof id==='string'?metricNumber(evaluation.metrics?.[id]):'Unavailable'}`));
        if(entries.length>32)row.append(element('p',`${entries.length-32} additional family metrics are not shown in this bounded view.`,'muted'));
        familySection.append(row);
      }
      if(families.length>256)familySection.append(element('p',`${families.length-256} additional families are not shown in this bounded view.`,'muted'));
      body.append(familySection);
    }
    if(evaluation.status==='queued'||evaluation.status==='running')body.append(element('p',evaluation.status==='queued'?'Checkpoint saved. Evaluation is queued.':'Evaluation in progress. Training may continue.','muted'));
    if(evaluation.status==='failed')body.append(element('p',typeof evaluation.error==='string'?text(evaluation.error):text(evaluation.error?.message)||'Evaluation failed. Training status is tracked separately.','evaluation-error'));
    if(evaluation.status==='canceled')body.append(element('p','Evaluation canceled. No completed result is claimed.','muted'));
    const comparisons=evaluationComparisons(evaluation.comparisons).slice(0,32);for(const comparison of comparisons)body.append(element('p',`${comparison.metric}: ${comparisonLabel(comparison)}`,comparison.compatible?'comparison':'comparison warning'));
    if(evaluation.status==='completed'&&!comparisons.length)body.append(element('p','No verified checkpoint comparison yet. Metric values are preserved without an improvement claim.','muted'));
    // Bounded identity projection: do not render entire prediction/sample arrays on each poll.
    const records:RecordValue=Object.create(null);for(const name of Object.keys(evaluation.evaluation_records??{}).slice(0,32)){const record=evaluation.evaluation_records[name];records[name]={digest:text(record?.digest),dataset:{id:text(record?.dataset?.id),revision:text(record?.dataset?.revision),content_digest:text(record?.dataset?.content_digest),benchmark:text(record?.dataset?.benchmark)},split:{id:text(record?.split?.id),role:text(record?.split?.role),sample_count:Array.isArray(record?.split?.sample_ids)?record.split.sample_ids.length:record?.split?.sample_count},protocol:{implementation:text(record?.protocol?.implementation),version:text(record?.protocol?.version)},metric:{name:text(record?.metric?.name),implementation:text(record?.metric?.implementation),version:text(record?.metric?.version),aggregation:text(record?.metric?.aggregation),direction:text(record?.metric?.direction)}};}
    provenance.textContent=JSON.stringify({checkpoint_digest:text(evaluation.checkpoint_digest),evaluation_identity_summaries:records,comparisons:comparisons.map(item=>({metric:text(item.metric),compatible:item.compatible===true,previous_evaluation_id:text(item.previous_evaluation_id),direction:item.direction,delta_from_previous:item.delta_from_previous,diagnostics:comparisonDiagnostics(item).map(d=>({message:text(d.message)}))}))},null,2);cards.push(card);
  }
  reconcile(view.cards,cards);for(const [id,entry]of view.cardNodes)if(!cards.includes(entry.root))view.cardNodes.delete(id);
}
