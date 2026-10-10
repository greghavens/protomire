import type { CheckpointEvaluation, EvaluationComparison } from './liveEvaluations';

/** UI bounds are independent of the durable evaluation history. No resampling. */
export const EVALUATION_WINDOW = 200;
export const MAX_CURVES = 12;
const MAX_METRICS = 32;
const finite = (value:unknown):value is number => typeof value==='number' && Number.isFinite(value);
export interface CurvePoint { evaluation:CheckpointEvaluation; value:number }
export interface CheckpointCurve {
  key:string;
  node:string;
  metric:string;
  points:CurvePoint[];
  links:[CurvePoint,CurvePoint][];
  gaps:CheckpointEvaluation[];
  direction?:'minimize'|'maximize';
}
export interface CurveProjection { curves:CheckpointCurve[]; evaluations:CheckpointEvaluation[]; omittedEvaluations:number; omittedCurves:number }

/** Only explicit service-verified comparison edges connect observed measurements.
 * Node identity and finite measured values are additional local fail-closed guards.
 * Missing/failed/pending intervening checkpoints break lines, even if the server
 * can compare their completed neighbours. This is not a training-loss series.
 */
export function checkpointCurves(input:CheckpointEvaluation[]):CurveProjection {
  // The service orders its window by step. Bound before any sorting or rendering.
  const recent=input.slice(-EVALUATION_WINDOW);
  const counts=new Map<string,number>();
  for(const row of recent)if(row&&typeof row.evaluation_id==='string')counts.set(row.evaluation_id,(counts.get(row.evaluation_id)??0)+1);
  const evaluations=recent.filter(row=>row&&typeof row.evaluation_id==='string'&&counts.get(row.evaluation_id)===1&&Number.isSafeInteger(row.step)&&row.step>=0).sort((a,b)=>a.step-b.step||a.evaluation_id.localeCompare(b.evaluation_id));
  const groups=new Map<string,CurvePoint[]>();
  for(const evaluation of evaluations){
    if(evaluation.status!=='completed')continue;
    let count=0;
    for(const metric in evaluation.metrics??{}){
      if(!Object.hasOwn(evaluation.metrics!,metric))continue;
      if(++count>MAX_METRICS)break;
      const value=evaluation.metrics![metric];if(!finite(value))continue;
      const node=typeof evaluation.node_id==='string'?evaluation.node_id:'';
      const key=JSON.stringify([node,metric]);const points=groups.get(key)??[];
      points.push({evaluation,value});groups.set(key,points);
    }
  }
  const curves:CheckpointCurve[]=[];
  for(const [groupKey,points] of groups){
    const node=typeof points[0].evaluation.node_id==='string'?points[0].evaluation.node_id:'';
    const metric=JSON.parse(groupKey)[1] as string;
    const pointById=new Map(points.map(point=>[point.evaluation.evaluation_id,point]));
    const parent=new Map(points.map(point=>[point.evaluation.evaluation_id,point.evaluation.evaluation_id]));
    const root=(id:string):string=>{let next=id;while(parent.get(next)!==next)next=parent.get(next)!;return next;};
    const links:[CurvePoint,CurvePoint][]=[];
    for(const point of points){
      const current=point.evaluation;const comparison=current.comparisons?.[metric] as EvaluationComparison|undefined;
      const previous=typeof comparison?.previous_evaluation_id==='string'?pointById.get(comparison.previous_evaluation_id):undefined;
      if(!node||comparison?.metric!==metric||comparison.compatible!==true||!previous||previous.evaluation.step>=current.step)continue;
      // Direction belongs to the metric identity verified by the service.
      if(comparison.direction!=='minimize'&&comparison.direction!=='maximize')continue;
      parent.set(root(current.evaluation_id),root(previous.evaluation.evaluation_id));
      const interrupted=evaluations.some(row=>row.node_id===node&&row.step>previous.evaluation.step&&row.step<=current.step&&row.evaluation_id!==current.evaluation_id);
      if(!interrupted)links.push([previous,point]);
    }
    const components=new Map<string,CurvePoint[]>();
    for(const point of points){const key=root(point.evaluation.evaluation_id),component=components.get(key)??[];component.push(point);components.set(key,component);}
    for(const [id,component] of components){
      const ids=new Set(component.map(point=>point.evaluation.evaluation_id));
      const direction=component.map(point=>point.evaluation.comparisons?.[metric]?.direction).find(value=>value==='minimize'||value==='maximize');
      curves.push({key:JSON.stringify([node,metric,id]),node,metric,points:component,links:links.filter(([a,b])=>ids.has(a.evaluation.evaluation_id)&&ids.has(b.evaluation.evaluation_id)),gaps:evaluations.filter(row=>row.node_id===node&&(row.status!=='completed'||!finite(row.metrics?.[metric]))),direction});
    }
  }
  curves.sort((a,b)=>b.points.at(-1)!.evaluation.step-a.points.at(-1)!.evaluation.step||a.key.localeCompare(b.key));
  return {curves:curves.slice(0,MAX_CURVES),evaluations,omittedEvaluations:input.length-evaluations.length,omittedCurves:Math.max(0,curves.length-MAX_CURVES)};
}

export function curveCoordinates(curve:CheckpointCurve):{x:(step:number)=>number;y:(value:number)=>number;minStep:number;maxStep:number;minValue:number;maxValue:number} {
  const steps=[...curve.points.map(point=>point.evaluation.step),...curve.gaps.map(row=>row.step)];
  const values=curve.points.map(point=>point.value);
  const minStep=Math.min(...steps),maxStep=Math.max(...steps),minValue=Math.min(...values),maxValue=Math.max(...values);
  const magnitude=Math.max(Math.abs(minValue),Math.abs(maxValue))||1;
  const lo=minValue/magnitude,hi=maxValue/magnitude;
  return {minStep,maxStep,minValue,maxValue,x:step=>maxStep===minStep?342:72+540*((step-minStep)/(maxStep-minStep)),y:value=>hi===lo?118:190-144*((value/magnitude-lo)/(hi-lo))};
}
