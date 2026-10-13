import { createHash } from 'node:crypto';
import { isAbsolute } from 'node:path';
import { ProjectDraft, validateProjectDraftFields } from './projectQuickstart';
export const WIZARD_DRAFT_KEY='modelbuilder.newProjectDraft.v1';
export interface WizardDraft {schema_version:'1';draft_id?:string;draft:ProjectDraft;step:string;folder?:string;source?:{source_alias:string;source_repository_id:string;trusted:true}}
interface Secrets {get(key:string):PromiseLike<string|undefined>;store(key:string,value:string):PromiseLike<void>;delete(key:string):PromiseLike<void>}
export function validateWizardDraft(value:unknown):WizardDraft{
  if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('Invalid saved project draft.');
  const r=value as WizardDraft;
  if(Object.keys(r).some(key=>!['schema_version','draft_id','draft','step','folder','source'].includes(key))||r.schema_version!=='1'||!['assistant','goal','start','project'].includes(r.step))throw new Error('Invalid saved project draft.');
  if(r.draft_id!==undefined&&(typeof r.draft_id!=='string'||! /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(r.draft_id)))throw new Error('Invalid saved draft identity.');
  const draft=validateProjectDraftFields(r.draft);
  if(r.folder!==undefined&&(typeof r.folder!=='string'||!isAbsolute(r.folder)))throw new Error('Saved project folder must be local and absolute.');
  if(r.source!==undefined&&(!r.folder||!r.source||typeof r.source!=='object'||Object.keys(r.source).some(key=>!['source_alias','source_repository_id','trusted'].includes(key))||typeof r.source.source_alias!=='string'||!r.source.source_alias||r.source.source_alias.length>200||!/^sha256:[a-f0-9]{64}$/.test(r.source.source_repository_id)||r.source.trusted!==true))throw new Error('Invalid saved source choice.');
  return structuredClone({...r,draft});
}
export function wizardDraftFingerprint(record:WizardDraft):string{return createHash('sha256').update(JSON.stringify(validateWizardDraft(record))).digest('hex');}
export class UnreadableWizardDraftError extends Error {
  constructor(readonly storedValue:string){super('The encrypted unfinished project draft could not be read safely.');}
}
interface DraftQueue {tail:Promise<void>}
const queues=new WeakMap<Secrets,DraftQueue>();
/** One queue per SecretStorage context also covers reopened/new controllers. */
export class ProjectWizardDraft {
  private queue:DraftQueue;
  constructor(private secrets:Secrets){const existing=queues.get(secrets);this.queue=existing??{tail:Promise.resolve()};if(!existing)queues.set(secrets,this.queue);}
  private enqueue(action:()=>Promise<void>):Promise<void>{const next=this.queue.tail.catch(()=>{}).then(action);this.queue.tail=next;return next;}
  async whenIdle():Promise<void>{await this.queue.tail.catch(()=>{});}
  async load():Promise<WizardDraft|undefined>{
    await this.whenIdle();const raw=await this.secrets.get(WIZARD_DRAFT_KEY);
    if(raw===undefined||await this.secrets.get(discardKey(fingerprint(raw))))return undefined;
    let value;try{value=JSON.parse(raw);}catch{throw new UnreadableWizardDraftError(raw);}
    if(value?.schema_version==='2'){
      if(Object.keys(value).some(key=>!['schema_version','fingerprint'].includes(key))||!validFingerprint(value.fingerprint))throw new UnreadableWizardDraftError(raw);
      const payload=await this.secrets.get(snapshotKey(value.fingerprint));if(payload===undefined)return undefined;
      try{if(fingerprint(payload)!==value.fingerprint)throw new Error('Draft snapshot identity mismatch.');return validateWizardDraft(JSON.parse(payload));}catch{throw new UnreadableWizardDraftError(raw);}
    }
    let record;try{record=validateWizardDraft(value);}catch{throw new UnreadableWizardDraftError(raw);}
    return await this.secrets.get(discardKey(wizardDraftFingerprint(record)))?undefined:record;
  }
  save(record:WizardDraft):Promise<void>{const value=JSON.stringify(validateWizardDraft(record)),id=fingerprint(value);return this.enqueue(async()=>{await this.secrets.store(snapshotKey(id),value);await this.secrets.store(WIZARD_DRAFT_KEY,JSON.stringify({schema_version:'2',fingerprint:id}));});}
  clear():Promise<void>{return this.enqueue(async()=>{const raw=await this.secrets.get(WIZARD_DRAFT_KEY);if(raw===undefined)return;let value;try{value=JSON.parse(raw);}catch{}if(value?.schema_version==='2'&&validFingerprint(value.fingerprint))await this.secrets.delete(snapshotKey(value.fingerprint));else await this.secrets.store(discardKey(fingerprint(raw)),'1');});}
  approve(persist:(fingerprint?:string)=>Promise<void>,origin?:WizardDraft|string|(()=>WizardDraft|string|undefined)):Promise<void>{return this.enqueue(async()=>{const record=typeof origin==='function'?origin():origin,owned=record===undefined?undefined:typeof record==='string'?record:wizardDraftFingerprint(record);if(owned!==undefined&&!validFingerprint(owned))throw new Error('Invalid approved draft identity.');await persist(owned);if(owned!==undefined){await this.secrets.delete(snapshotKey(owned));await this.secrets.store(discardKey(owned),'1');}});}
  discardUnreadable(error:UnreadableWizardDraftError):Promise<void>{return this.enqueue(async()=>{if(await this.secrets.get(WIZARD_DRAFT_KEY)!==error.storedValue)throw new Error('The saved draft changed. Reopen New Project before deciding what to discard.');await this.secrets.store(discardKey(fingerprint(error.storedValue)),'1');});}
}
function fingerprint(value:string):string{return createHash('sha256').update(value).digest('hex');}
function validFingerprint(value:unknown):value is string{return typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);}
const snapshotKey=(id:string)=>'modelbuilder.wizardSnapshot.v2.'+id;
const discardKey=(id:string)=>'modelbuilder.wizardDiscard.v2.'+id;
