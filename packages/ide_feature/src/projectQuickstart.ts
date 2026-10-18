import { Project, RecordValue, ServiceTransport } from './types';
export type Harness='none'|'codex'|'claude'|'triggerfish'|'opencode'|'kilocode';
export type StartingPoint='blank'|'research'|'base_model'|'scratch';
export interface ProjectDraft {modelName:string;slug:string;goal:string;startingPoint:StartingPoint;assistant:Harness;quickstart:boolean}
export interface ProjectSource {source_alias:string;source_repository_id:string;trusted:true}
export interface ApprovedProjectRequest {schema_version:'1';idempotencyKey:string;draft:ProjectDraft;source?:ProjectSource;payload:RecordValue}
export type QuickstartStage='details'|'review'|'creating'|'created'|'cancelled';
export const emptyProjectDraft=():ProjectDraft=>({modelName:'',slug:'',goal:'',startingPoint:'scratch',assistant:'none',quickstart:true});
const allowedDraftKeys=new Set(['modelName','slug','goal','startingPoint','assistant','quickstart']);
export function validateProjectDraftFields(value:unknown):ProjectDraft{
  if(!value||typeof value!=='object'||Array.isArray(value)||Object.keys(value).some(key=>!allowedDraftKeys.has(key)))throw new Error('Invalid project details.');
  const d=value as ProjectDraft;
  if(typeof d.modelName!=='string'||d.modelName.length>200)throw new Error('Enter a model name of 1–200 characters.');
  if(typeof d.slug!=='string'||d.slug.length>80)throw new Error('Use a lowercase project slug with letters, numbers and hyphens (up to 80 characters).');
  if(typeof d.goal!=='string'||d.goal.length>8000)throw new Error('The project goal must be at most 8,000 characters.');
  if(!['blank','research','base_model','scratch'].includes(d.startingPoint)||!['none','codex','claude','triggerfish','opencode','kilocode'].includes(d.assistant)||typeof d.quickstart!=='boolean')throw new Error('Choose a supported starting point and optional assistant.');
  return {...d};
}
export function validateProjectDraft(value:unknown):ProjectDraft{
  const d=validateProjectDraftFields(value);
  if(!d.modelName.trim())throw new Error('Enter a model name of 1–200 characters.');
  if(!/^[a-z0-9][a-z0-9-]{0,79}$/.test(d.slug))throw new Error('Use a lowercase project slug with letters, numbers and hyphens (up to 80 characters).');
  return {...d,modelName:d.modelName.trim()};
}
/** A single native page owns one immutable create payload and idempotency key.
 * UI revisions reject old messages; no project is created until explicit review.
 * An uncertain network response retains the payload/key for a safe retry.
 */
export class ProjectQuickstartSession{
  revision=1;stage:QuickstartStage='details';draft=emptyProjectDraft();project?:Project;
  private source?:ProjectSource;private frozenPayload?:RecordValue;private inFlight?:Promise<Project>;
  constructor(readonly idempotencyKey:string){if(!idempotencyKey)throw new Error('A session key is required.');}
  private expect(revision:number){if(revision!==this.revision)throw new Error('The quickstart changed. Review the current page before continuing.');}
  edit(value:unknown,revision:number){this.expect(revision);if(this.stage!=='details'||this.frozenPayload)throw new Error('Project details cannot change after a create attempt. Retry the reviewed request or open the created project.');this.draft=validateProjectDraft(value);this.revision++;}
  review(source:ProjectSource|undefined,revision:number){this.expect(revision);if(this.stage!=='details')throw new Error('Finish the current step first.');validateProjectDraft(this.draft);if(source&&(!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(source.source_alias)||!/^sha256:[0-9a-f]{64}$/.test(source.source_repository_id)||source.trusted!==true))throw new Error('Invalid approved source identity.');this.source=source;this.stage='review';this.revision++;}
  back(revision:number){this.expect(revision);if(this.stage!=='review'||this.frozenPayload)throw new Error('A submitted creation cannot be changed or undone by Back.');this.stage='details';this.revision++;}
  cancel(revision:number){this.expect(revision);this.stage='cancelled';this.revision++;}
  approvedRequest():ApprovedProjectRequest|undefined{return this.frozenPayload?{schema_version:'1',idempotencyKey:this.idempotencyKey,draft:{...this.draft},source:this.source?{...this.source}:undefined,payload:structuredClone(this.frozenPayload)}:undefined;}
  static restore(record:ApprovedProjectRequest):ProjectQuickstartSession{if(record?.schema_version!=='1'||typeof record.idempotencyKey!=='string'||!record.idempotencyKey||record.idempotencyKey.length>200)throw new Error('Invalid pending project request.');const s=new ProjectQuickstartSession(record.idempotencyKey);s.edit(record.draft,s.revision);s.review(record.source,s.revision);const d=s.draft;const expected={model_name:d.modelName,slug:d.slug,...s.source,intent:{schema_version:'1',goal:d.quickstart?d.goal:'',starting_point:d.quickstart?d.startingPoint:'blank',assistant:d.quickstart?d.assistant:'none'},idempotency_key:s.idempotencyKey};if(JSON.stringify(expected)!==JSON.stringify(record.payload))throw new Error('Pending project payload changed; it was not submitted.');s.frozenPayload=structuredClone(expected);return s;}
  create(client:ServiceTransport,revision:number,beforeSubmit?:(request:ApprovedProjectRequest)=>Promise<void>):Promise<Project>{
    // A repeated click from the exact submitted review joins its existing request.
    if(this.inFlight&&revision===this.revision-1)return this.inFlight;
    this.expect(revision);if(this.stage==='created'&&this.project)return Promise.resolve(this.project);
    if(this.stage!=='review')throw new Error('Review the project before creating it.');
    this.frozenPayload??={model_name:this.draft.modelName,slug:this.draft.slug,...this.source,
      intent:{schema_version:'1',goal:this.draft.quickstart?this.draft.goal:'',starting_point:this.draft.quickstart?this.draft.startingPoint:'blank',assistant:this.draft.quickstart?this.draft.assistant:'none'},idempotency_key:this.idempotencyKey};
    this.stage='creating';this.revision++;
    this.inFlight=(async()=>{if(beforeSubmit)await beforeSubmit(this.approvedRequest()!);return client.invoke<Project>('project.create',this.frozenPayload!);})().then(project=>{if(!project?.project_id||!project.recipe_id||project.recipe_id!==project.active_recipe_id)throw new Error('The service did not return one canonical model recipe. Retry this same request.');this.project=project;this.stage='created';this.revision++;return project;}).catch(error=>{this.stage='review';this.revision++;throw error;}).finally(()=>{this.inFlight=undefined;});
    return this.inFlight;
  }
  snapshot(){return {revision:this.revision,stage:this.stage,draft:{...this.draft},source:this.source?{...this.source}:undefined,project:this.project,submitted:!!this.frozenPayload};}
}
export const HARNESS_CAPABILITIES=[
  {id:'none',name:'No assistant',install:'not_required',authentication:'not_required',mcp:'not_required'},
  {id:'codex',name:'Codex',install:'optional_vendor_extension',authentication:'separate_unverified',mcp:'project_config_supported'},
  {id:'claude',name:'Claude Code',install:'optional_vendor_extension',authentication:'separate_unverified',mcp:'project_config_supported'},
  {id:'opencode',name:'OpenCode',install:'vendor_installation_unverified',authentication:'separate_unverified',mcp:'project_config_supported'},
  {id:'kilocode',name:'Kilo Code',install:'vendor_installation_unverified',authentication:'separate_unverified',mcp:'project_config_supported'},
  {id:'triggerfish',name:'Triggerfish',install:'unavailable_pending_compatibility',authentication:'unverified',mcp:'interface_only'},
] as const;
