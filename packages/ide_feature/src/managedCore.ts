/** Bundled core lease. No system Python, shell invocation, global profile edits or persisted IDE token. */
import { spawn, ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync, readFileSync, realpathSync } from 'node:fs';
import { isAbsolute, join, relative, sep } from 'node:path';

export interface CoreConnection {url:string; token:string; instance:string}
export interface CorePaths {python:string; entrypoint:string; runtimeRoot:string; stateDir:string}
export interface SourceRegistration {alias:string;repository_id:string;workspace_path:string;project_id:string;operator_trusted:boolean;registered:boolean}
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
  const keys=['PATH','LANG','LC_ALL','TZ','SYSTEMROOT','WINDIR','TMPDIR','TMP','TEMP'];
  return Object.fromEntries(keys.filter(key=>source[key]!==undefined).map(key=>[key,source[key]]));
}
export class ManagedCore {
  private child?:ChildProcessWithoutNullStreams;
  private pending?:Promise<CoreConnection>;
  private current?:CoreConnection;
  private disposed=false;
  private cancelStartup?:()=>void;
  get url():string|undefined{return this.current?.url;}
  constructor(readonly paths:CorePaths,private readonly launcher:CoreSpawner=spawn,private readonly timeoutMs=45000,private readonly request:typeof fetch=fetch){}
  connection():Promise<CoreConnection>{
    if(this.disposed)return Promise.reject(new Error('The ModelBuilder core lease is closed.'));
    if(this.current)return Promise.resolve(this.current);
    if(this.pending)return this.pending;
    const pending=new Promise<CoreConnection>((resolve,reject)=>{
      const child=this.launcher(this.paths.python,['-I',this.paths.entrypoint,'lease','--runtime-root',this.paths.runtimeRoot,'--state-dir',this.paths.stateDir],{env:coreEnvironment(process.env),windowsHide:true,stdio:'pipe'}) as ChildProcessWithoutNullStreams;
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
    return {command:this.paths.python,args:['-I',this.paths.entrypoint,'mcp','--runtime-root',this.paths.runtimeRoot,'--state-dir',this.paths.stateDir,'--project-id',projectId]};
  }
  dispose(){this.disposed=true;this.cancelStartup?.();this.cancelStartup=undefined;this.current=undefined;this.child?.stdin.end();this.child=undefined;}
}
