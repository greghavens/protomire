import { createHash } from 'node:crypto';
import { isAbsolute } from 'node:path';
import { ApprovedProjectRequest, ProjectQuickstartSession } from './projectQuickstart';
interface RecoveryContext {globalState:{get<T>(key:string,defaultValue:T):T;update(key:string,value:unknown):PromiseLike<void>};secrets:{get(key:string):PromiseLike<string|undefined>;store(key:string,value:string):PromiseLike<void>;delete(key:string):PromiseLike<void>}}
export interface PendingProject {folder:string;request:ApprovedProjectRequest}
const INDEX='modelbuilder.pendingProjects.v1';
const key=(folder:string)=>'modelbuilder.newProject.v1.'+createHash('sha256').update(folder).digest('hex');
/** Only an explicitly approved create request reaches this encrypted store.
 * The ordinary app index contains folder paths, never goals or request payloads.
 */
export class ProjectDraftRecovery{
  constructor(private context:RecoveryContext){}
  async save(folder:string,request:ApprovedProjectRequest){if(!isAbsolute(folder))throw new Error('Pending project requires its selected native folder.');ProjectQuickstartSession.restore(request);const folders=this.context.globalState.get<string[]>(INDEX,[]);if(!folders.includes(folder))await this.context.globalState.update(INDEX,[...folders,folder]);await this.context.secrets.store(key(folder),JSON.stringify({schema_version:'1',folder,request}));}
  async list():Promise<PendingProject[]>{const result:PendingProject[]=[];for(const folder of this.context.globalState.get<string[]>(INDEX,[])){if(typeof folder!=='string'||!isAbsolute(folder))throw new Error('Pending project index is invalid.');const raw=await this.context.secrets.get(key(folder));if(!raw)continue;let value;try{value=JSON.parse(raw);}catch{throw new Error('A pending project request could not be read safely.');}if(value.schema_version!=='1'||value.folder!==folder)throw new Error('Pending project identity does not match its workspace.');ProjectQuickstartSession.restore(value.request);result.push({folder,request:value.request});}return result;}
  async clear(folder:string){await this.context.secrets.delete(key(folder));await this.context.globalState.update(INDEX,this.context.globalState.get<string[]>(INDEX,[]).filter(path=>path!==folder));}
}
