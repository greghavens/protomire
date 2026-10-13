import * as vscode from 'vscode';
import { randomUUID } from 'node:crypto';
import { realpath } from 'node:fs/promises';
import { Project, RecordValue, ServiceTransport } from './types';
export type ManagedSourceRegistration=(folderPath:string,projectId:string,trusted:true)=>Promise<{alias:string;repository_id:string;workspace_path:string;project_id:string;operator_trusted:boolean;registered:boolean}>;
/** The browser sends neither filesystem paths nor a trust flag. Only native
 * selection + the explicit native confirmation below can register source trust.
 * Registration never initializes Git, makes commits, pins a recipe or executes.
 */
export async function registerProjectSource(client:ServiceTransport,project:Project,folder:vscode.Uri,register:ManagedSourceRegistration,assertCurrent:()=>void=()=>{}):Promise<RecordValue|undefined>{
  if(folder.scheme!=='file')throw new Error('Choose a local project folder before enabling its source.');
  const canonical=await realpath(folder.fsPath);if(canonical!==folder.fsPath)throw new Error('Choose the real project folder, not a redirected or symbolic-link path.');
  const before=await client.invoke<Project>('project.get',{project_id:project.project_id});assertCurrent();
  const confirmed=await vscode.window.showWarningMessage(`Allow this model project to execute trusted code from this exact local Git checkout?\n\nProject: ${before.model_name}\nFolder: ${canonical}\n\nThis is your code with local filesystem/process access, not a hostile-code sandbox. The folder must already be a committed Git root. ModelBuilder will register its identity only for this project and bind it at project revision ${before.revision}. It does not create Git history, run training, pin files, install dependencies or authorize spending. Each run still requires source/dependency validation and admission.`,{modal:true},'Register trusted project source');assertCurrent();
  if(confirmed!=='Register trusted project source')return;
  const receipt=await register(canonical,before.project_id,true);assertCurrent();
  if(!receipt?.registered||receipt.project_id!==before.project_id||receipt.workspace_path!==canonical||receipt.operator_trusted!==true||!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(receipt.alias)||!/^sha256:[0-9a-f]{64}$/.test(receipt.repository_id))throw new Error('Managed source registration returned a different project or checkout identity. Nothing was bound.');
  const bound=await client.invoke<Project>('project.source.bind',{project_id:before.project_id,expected_revision:before.revision,source_alias:receipt.alias,source_repository_id:receipt.repository_id,trusted:true,idempotency_key:randomUUID()});
  return {project:bound,source:receipt,execution_started:false,recipe_pinned:false};
}
