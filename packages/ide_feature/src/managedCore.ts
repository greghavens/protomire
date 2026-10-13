/** Bundled core lease. No system Python, shell invocation, global profile edits or persisted IDE token. */
import { spawn, ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync, readFileSync, realpathSync } from 'node:fs';
import { isAbsolute, join, relative, sep } from 'node:path';

export interface CoreConnection {url:string; token:string; instance:string}
export interface CorePaths {python:string; entrypoint:string; runtimeRoot:string; stateDir:string}
export interface NativeReview {id:string;requested_by:string;project_id:string;project_name:string;operation:string;payload:Record<string,unknown>;payload_digest:string;review_digest:string;status:string;expires_at:number;action_ttl_seconds:number;required_scopes:string[];disclosure?:unknown;result?:unknown;error?:{message?:string};notice?:string}
export interface SourceRegistration {alias:string;repository_id:string;workspace_path:string;project_id:string;operator_trusted:boolean;registered:boolean}
export interface HubAccountReference {keyring_service:string;username:string;alias:string}
export interface HubSettings {max_bytes:number;project_id:string;revision:number;account:HubAccountReference|null;folder:{path:string;device:number;inode:number}|null;credential_status?:string}
export interface HubSettingsReview {plan:{project_id:string;expected_revision:number;account:HubAccountReference|null;folder:HubSettings['folder'];max_bytes:number;expires_at:number};review_digest:string}
export type CoreSpawner = typeof spawn;
function contained(root:string,name:unknown):string {
  if(typeof name!=='string'||!name||isAbsolute(name))throw new Error('Bundled runtime path is invalid. Reinstall ModelBuilder.');
  const path=join(root,name),rel=relative(realpathSync(root),realpathSync(path));
  if(rel==='..'||rel.startsWith('..'+sep)||isAbsolute(rel))throw new Error('Bundled runtime path escapes the installation.');
  return path;
}
export function corePaths(extensionPath:string,stateDir:string,appRoot?:string):CorePaths|undefined {
  if(process.env.MODELBUILDER_DEVELOPMENT==='1')return undefined;
  const root=join(extensionPath,'resources','core');
  let required=false;
  if(appRoot&&existsSync(join(appRoot,'product.json')))required=JSON.parse(readFileSync(join(appRoot,'product.json'),'utf8')).modelbuilderBundledCore===true;
  if(!existsSync(join(root,'runtime.json'))){if(required)throw new Error('Mandatory ModelBuilder core is missing. Reinstall the application.');return undefined;}
  if(!isAbsolute(stateDir))throw new Error('Private native app storage is unavailable; the bundled core cannot start.');
  const manifest=JSON.parse(readFileSync(join(root,'runtime.json'),'utf8'));
  if(manifest.schema_version!==1)throw new Error('Unsupported bundled core version. Reinstall ModelBuilder.');
  return {python:contained(root,manifest.service_python),entrypoint:contained(root,'managed_core.py'),runtimeRoot:root,stateDir};
}
export function coreEnvironment(source:NodeJS.ProcessEnv):NodeJS.ProcessEnv {
  const keys=['PATH','LANG','LC_ALL','TZ','SYSTEMROOT','WINDIR','TMPDIR','TMP','TEMP','HOME','USERPROFILE','APPDATA','LOCALAPPDATA','DBUS_SESSION_BUS_ADDRESS','XDG_RUNTIME_DIR'];
  return Object.fromEntries(keys.filter(key=>source[key]!==undefined).map(key=>[key,source[key]]));
}
export class ManagedCore {
  private child?:ChildProcessWithoutNullStreams;
  private pending?:Promise<CoreConnection>;
  private current?:CoreConnection;
  private disposed=false;
  private credentialChildren=new Set<ChildProcessWithoutNullStreams>();
  private cancelStartup?:()=>void;
  get url():string|undefined{return this.current?.url;}
  constructor(readonly paths:CorePaths,private readonly launcher:CoreSpawner=spawn,private readonly timeoutMs=45000,private readonly request:typeof fetch=fetch){}
  connection():Promise<CoreConnection>{
    if(this.disposed)return Promise.reject(new Error('The ModelBuilder core lease is closed.'));
    if(this.current)return Promise.resolve(this.current);
    if(this.pending)return this.pending;
    const pending=new Promise<CoreConnection>((resolve,reject)=>{
      const child=this.launcher(this.paths.python,['-I','-B',this.paths.entrypoint,'lease','--runtime-root',this.paths.runtimeRoot,'--state-dir',this.paths.stateDir],{env:coreEnvironment(process.env),windowsHide:true,stdio:'pipe'}) as ChildProcessWithoutNullStreams;
      this.child=child;let output='',settled=false;
      const timer=setTimeout(()=>fail('The bundled core did not become ready. Check the installation and private app storage.'),this.timeoutMs);
      const fail=(message:string)=>{if(!settled){settled=true;clearTimeout(timer);reject(new Error(message));}child.stdin.end();child.kill();if(this.child===child){this.child=undefined;this.current=undefined;this.pending=undefined;}};
      this.cancelStartup=()=>fail('The ModelBuilder core lease is closed.');
      child.stdin.on('error',()=>{});
      // Never forward subprocess output: it may contain the local session credential.
      child.stderr.resume();
      child.on('error',()=>fail('The bundled core could not launch. Reinstall ModelBuilder.'));
      child.on('exit',()=>{if(!settled)fail('The bundled core exited before it was ready.');if(this.child===child){this.child=undefined;this.current=undefined;this.pending=undefined;}});
      child.stdout.on('data',(data:Buffer)=>{
        if(settled)return;
        output+=data.toString('utf8');if(output.length>8192)return fail('The bundled core returned an invalid startup response.');
        if(!output.includes('\n'))return;
        try{
          const value=JSON.parse(output.slice(0,output.indexOf('\n'))),url=new URL(value.url);
          if(url.protocol!=='http:'||url.hostname!=='127.0.0.1'||!url.port||url.pathname!=='/'||url.username||url.password||url.search||url.hash||!/^[a-f0-9]{64}$/.test(value.token)||!/^[a-f0-9]{32}$/.test(value.instance))throw new Error();
          this.current={url:url.origin,token:value.token,instance:value.instance};settled=true;this.cancelStartup=undefined;clearTimeout(timer);resolve(this.current);
        }catch{fail('The bundled core returned an invalid startup response.');}
      });
    });
    this.pending=pending;pending.catch(()=>{if(this.pending===pending)this.pending=undefined;});return pending;
  }
  private async reviewRequest(path:string,payload?:Record<string,unknown>):Promise<any>{
    const connection=await this.connection();
    const response=await this.request(connection.url+path,{method:payload?'POST':'GET',redirect:'error',headers:{'Authorization':'Bearer '+connection.token,'Content-Type':'application/json'},body:payload?JSON.stringify(payload):undefined,signal:AbortSignal.timeout(180000)});
    const result=await response.json();if(!response.ok)throw new Error(result.error?.message??'Native request review failed.');return result;
  }
  /** Called only after explicit native persistent-storage confirmation. Secret travels on stdin, never HTTP/MCP or argv. */
  async storeHubCredential(reference:{keyring_service:string;username:string},secret:string):Promise<{status:'stored'|'not_stored'|'uncertain';code:string}>{
    await this.connection(); // verifies the installed runtime before helper launch
    const manifest=JSON.parse(readFileSync(join(this.paths.runtimeRoot,'runtime.json'),'utf8'));
    const python=contained(this.paths.runtimeRoot,manifest.hub_python),helper=contained(this.paths.runtimeRoot,'hub_credentials.py');
    return new Promise(resolve=>{
      let output='',settled=false;
      const finish=(status:'stored'|'not_stored'|'uncertain',code:string)=>{if(settled)return;settled=true;clearTimeout(timer);resolve({status,code});};
      const child=this.launcher(python,['-I','-B',helper],{env:coreEnvironment(process.env),windowsHide:true,stdio:'pipe'}) as ChildProcessWithoutNullStreams;
      this.credentialChildren.add(child);
      const timer=setTimeout(()=>{child.kill();finish('uncertain','secure_store_timeout');},60000);
      child.stderr.resume();child.stdin.on('error',()=>finish('uncertain','secure_store_interrupted'));
      child.on('error',()=>finish('not_stored','secure_store_unavailable'));
      child.stdout.on('data',(data:Buffer)=>{output+=data.toString('utf8');if(output.length>1024){child.kill();finish('uncertain','invalid_secure_store_receipt');}});
      child.on('close',(exitCode:number|null)=>{
        this.credentialChildren.delete(child);
        if(exitCode!==0)return finish('uncertain','secure_store_interrupted');
        try{const value=JSON.parse(output);if(!['stored','not_stored','uncertain'].includes(value.status)||!/^[-a-z_]{1,80}$/.test(value.code))throw new Error();finish(value.status,value.code);}
        catch{finish('uncertain','invalid_secure_store_receipt');}
      });
      child.stdin.end(JSON.stringify({service:reference.keyring_service,username:reference.username,secret}));
    });
  }
  async hubSettings(projectId:string):Promise<HubSettings>{
    if(!/^[a-zA-Z0-9_-]{1,128}$/.test(projectId))throw new Error('A specific model project is required.');
    return this.reviewRequest('/_desktop/hub/settings/'+projectId);
  }
  async previewHubSettings(projectId:string,revision:number,account:{keyring_service:string;username:string}|null,folder:string|null,folderSelected=false,maxBytes=16*1024**3):Promise<HubSettingsReview>{
    return this.reviewRequest('/_desktop/hub/settings/preview',{project_id:projectId,expected_revision:revision,account,folder_path:folder,folder_selected:folderSelected,max_bytes:maxBytes});
  }
  async applyHubSettings(review:HubSettingsReview):Promise<HubSettings>{return this.reviewRequest('/_desktop/hub/settings/apply',review as unknown as Record<string,unknown>);}
  async pendingReviews():Promise<NativeReview[]>{return (await this.reviewRequest('/_desktop/reviews')).requests;}
  async reviewStatus(requestId:string):Promise<NativeReview>{if(!/^[0-9a-f-]{36}$/.test(requestId))throw new Error('Invalid exact review identity.');return this.reviewRequest('/_desktop/reviews/'+requestId);}
  async requestReview(projectId:string,operation:string,payload:Record<string,unknown>):Promise<NativeReview>{return this.reviewRequest('/_desktop/reviews',{project_id:projectId,operation,payload});}
  async decideReview(request:NativeReview,decision:'approve'|'deny'|'revoke'):Promise<NativeReview>{
    if(!/^[0-9a-f-]{36}$/.test(request.id)||!/^[0-9a-f]{64}$/.test(request.review_digest))throw new Error('Invalid exact review identity.');
    return this.reviewRequest('/_desktop/reviews/'+request.id+'/decision',{review_digest:request.review_digest,decision});
  }
  /** Invoke only after a native folder selection and explicit native trust confirmation. */
  async registerSource(folderPath:string,projectId:string,trustConsent:boolean):Promise<SourceRegistration>{
    if(trustConsent!==true)throw new Error('Explicit native source trust confirmation is required.');
    if(!isAbsolute(folderPath)||folderPath.includes('\0')||!/^[a-zA-Z0-9_-]{1,128}$/.test(projectId))throw new Error('A selected native folder and model project are required.');
    const connection=await this.connection();
    const response=await this.request(connection.url+'/_desktop/source',{method:'POST',redirect:'error',headers:{'Authorization':'Bearer '+connection.token,'Content-Type':'application/json'},body:JSON.stringify({folder_path:folderPath,project_id:projectId,trust_consent:true}),signal:AbortSignal.timeout(30000)});
    const value=await response.json() as SourceRegistration&{error?:{message?:string}};
    if(!response.ok)throw new Error(value.error?.message??'The selected source folder could not be registered.');
    if(value.project_id!==projectId||value.workspace_path!==folderPath||value.registered!==true||value.operator_trusted!==true||!/^workspace-[a-f0-9]{24}$/.test(value.alias)||!/^sha256:[a-f0-9]{64}$/.test(value.repository_id))throw new Error('Source registration returned a mismatched receipt.');
    return {alias:value.alias,repository_id:value.repository_id,workspace_path:value.workspace_path,project_id:value.project_id,operator_trusted:true,registered:true};
  }
  /** External consumers launch their own stdio process and receive only project-scoped read access. */
  mcpCommand(projectId:string):{command:string;args:string[]}{
    if(!/^[a-zA-Z0-9_-]{1,128}$/.test(projectId))throw new Error('A specific model project is required for assistant access.');
    return {command:this.paths.python,args:['-I','-B',this.paths.entrypoint,'mcp','--runtime-root',this.paths.runtimeRoot,'--state-dir',this.paths.stateDir,'--project-id',projectId]};
  }
  dispose(){for(const child of this.credentialChildren)child.kill();this.credentialChildren.clear();this.disposed=true;this.cancelStartup?.();this.cancelStartup=undefined;this.current=undefined;this.child?.stdin.end();this.child=undefined;}
}
