import { EvaluationEvent, EvaluationComparison, evaluationComparisons, metricNumber } from './liveEvaluations';

export interface NotificationPreferences {
  enabled:boolean;
  results:boolean;
  improvements:boolean;
  regressions:boolean;
  failures:boolean;
  trainingFinished:boolean;
  trainingFailed:boolean;
  trainingCanceled:boolean;
  minimumIntervalSeconds:number;
  minimumDelta:number;
  metric:string;
}
export const DEFAULT_NOTIFICATION_PREFERENCES:NotificationPreferences={enabled:true,results:false,improvements:false,regressions:true,failures:true,trainingFinished:true,trainingFailed:true,trainingCanceled:false,minimumIntervalSeconds:30,minimumDelta:0,metric:''};
export const NOTIFICATION_BOOLEAN_KEYS=['enabled','results','improvements','regressions','failures','trainingFinished','trainingFailed','trainingCanceled'] as const;
export function notificationPreferences(raw:unknown):NotificationPreferences {
  const value=raw&&typeof raw==='object'?raw as Partial<NotificationPreferences>:{};
  const result={...DEFAULT_NOTIFICATION_PREFERENCES};
  for(const key of NOTIFICATION_BOOLEAN_KEYS)if(typeof value[key]==='boolean')result[key]=value[key]!;
  if(typeof value.minimumIntervalSeconds==='number'&&Number.isFinite(value.minimumIntervalSeconds))result.minimumIntervalSeconds=Math.min(3600,Math.max(10,Math.round(value.minimumIntervalSeconds)));
  if(typeof value.minimumDelta==='number'&&Number.isFinite(value.minimumDelta))result.minimumDelta=Math.max(0,value.minimumDelta);
  if(typeof value.metric==='string')result.metric=value.metric.trim().slice(0,160);
  return result;
}
export interface NotificationCursor {cursor:number;initialized:boolean;eventIds:string[];terminal:boolean}
export interface NotificationNotice {kind:'failure'|'regression'|'improvement'|'result';message:string;count:number;projectId?:string;runId?:string}
export interface NotificationState {runs:Record<string,NotificationCursor>;lastNotifiedAt:number;pending?:NotificationNotice}
export function notificationState(raw:unknown):NotificationState {
  const value=raw&&typeof raw==='object'?raw as Partial<NotificationState>:{};
  const result:NotificationState={runs:{},lastNotifiedAt:typeof value.lastNotifiedAt==='number'&&Number.isFinite(value.lastNotifiedAt)?value.lastNotifiedAt:0};
  for(const [key,entry]of Object.entries(value.runs??{}))if(entry&&Number.isSafeInteger(entry.cursor)&&entry.cursor>=0)result.runs[key]={cursor:entry.cursor,initialized:entry.initialized===true,eventIds:Array.isArray(entry.eventIds)?entry.eventIds.filter(id=>typeof id==='string').slice(-256):[],terminal:entry.terminal===true};
  const pending=value.pending;
  if(pending&&['failure','regression','improvement','result'].includes(pending.kind)&&typeof pending.message==='string'&&Number.isSafeInteger(pending.count)&&pending.count>0&&typeof pending.projectId==='string')result.pending={...pending,message:pending.message.slice(0,4000)};
  return result;
}
const priorities={failure:4,regression:3,improvement:2,result:1};
function movement(comparison:EvaluationComparison):'improvement'|'regression'|undefined {
  if(comparison.compatible!==true||typeof comparison.delta_from_previous!=='number'||!Number.isFinite(comparison.delta_from_previous)||comparison.delta_from_previous===0)return;
  if(comparison.direction!=='minimize'&&comparison.direction!=='maximize')return;
  return (comparison.direction==='minimize'?comparison.delta_from_previous<0:comparison.delta_from_previous>0)?'improvement':'regression';
}
export class EvaluationNotifications {
  readonly state:NotificationState;
  preferences:NotificationPreferences;
  constructor(savedState?:unknown,savedPreferences?:unknown){this.state=notificationState(savedState);this.preferences=notificationPreferences(savedPreferences);}
  runKey(projectId:string,runId:string){return JSON.stringify([projectId,runId]);}
  cursor(projectId:string,runId:string){return this.state.runs[this.runKey(projectId,runId)]?.cursor??0;}
  hasTerminal(projectId:string,runId:string){return this.state.runs[this.runKey(projectId,runId)]?.terminal===true;}
  startRun(projectId:string,runId:string){this.state.runs[this.runKey(projectId,runId)]??={cursor:0,initialized:true,eventIds:[],terminal:false};}
  updatePreferences(value:unknown){this.preferences=notificationPreferences(value);this.state.pending=undefined;}
  consume(projectId:string,runId:string,events:EvaluationEvent[],nextCursor:number,hasMore:boolean):void {
    const key=this.runKey(projectId,runId);
    const current=this.state.runs[key]??={cursor:0,initialized:false,eventIds:[],terminal:false};
    const seen=new Set(current.eventIds);
    for(const event of [...events].sort((a,b)=>a.cursor-b.cursor)){
      if(!Number.isSafeInteger(event.cursor)||event.cursor<=current.cursor||typeof event.event_id!=='string'||seen.has(event.event_id))continue;
      seen.add(event.event_id);current.eventIds.push(event.event_id);current.cursor=event.cursor;
      if(['run.succeeded','run.failed','run.cancelled'].includes(event.kind))current.terminal=true;
      if(!current.initialized||!this.preferences.enabled)continue;
      const notice=this.notice(event);
      if(notice){notice.projectId=projectId;notice.runId=runId;const pending=this.state.pending?.projectId===projectId?this.state.pending:undefined;this.state.pending=pending?{...(priorities[notice.kind]>=priorities[pending.kind]?notice:pending),count:pending.count+notice.count}:notice;}
    }
    if(Number.isSafeInteger(nextCursor)&&nextCursor>=current.cursor)current.cursor=nextCursor;
    current.eventIds=current.eventIds.slice(-256);
    if(!hasMore)current.initialized=true;
  }
  /** One coalesced native notification per cooldown; event cursors survive reconnects. */
  takeNotice(now=Date.now(),projectId?:string):NotificationNotice|undefined {
    if(!this.preferences.enabled||!this.state.pending||(projectId!==undefined&&this.state.pending.projectId!==projectId)||now-this.state.lastNotifiedAt<this.preferences.minimumIntervalSeconds*1000)return;
    const notice=this.state.pending;this.state.pending=undefined;this.state.lastNotifiedAt=now;return notice;
  }
  private notice(event:EvaluationEvent):NotificationNotice|undefined {
    const runLabel=`Training run ${event.operation_id??''}`.trim();
    if(event.kind==='run.succeeded'&&this.preferences.trainingFinished)return {kind:'result',message:`${runLabel} finished successfully. Open Runs for the final results.`,count:1};
    if(event.kind==='run.failed'&&this.preferences.trainingFailed)return {kind:'failure',message:`${runLabel} failed. Open Runs for reconciled status and logs.`,count:1};
    if(event.kind==='run.cancelled'&&this.preferences.trainingCanceled)return {kind:'result',message:`${runLabel} was canceled by the backend.`,count:1};
    const label=`Checkpoint step ${event.step??'?'}${event.checkpoint_id?` (${event.checkpoint_id})`:''}`;
    if(event.kind==='evaluation.failed'&&this.preferences.failures)return {kind:'failure',message:`${label}: evaluation failed. Open Runs for details.`,count:1};
    if(event.kind!=='evaluation.completed')return;
    for(const comparison of evaluationComparisons(event.comparisons).sort((a,b)=>(movement(a)==='regression'?-1:1)-(movement(b)==='regression'?-1:1))){
      if(this.preferences.metric&&comparison.metric!==this.preferences.metric)continue;
      const change=movement(comparison);if(!change||Math.abs(comparison.delta_from_previous!)<this.preferences.minimumDelta)continue;
      if(change==='improvement'&&!this.preferences.improvements||change==='regression'&&!this.preferences.regressions)continue;
      return {kind:change,message:`${label}: ${comparison.metric} ${change==='improvement'?'improved':'regressed'} by ${metricNumber(Math.abs(comparison.delta_from_previous!))} against the previous compatible checkpoint.`,count:1};
    }
    if(this.preferences.results)return {kind:'result',message:`${label}: evaluation results are ready.`,count:1};
  }
}
