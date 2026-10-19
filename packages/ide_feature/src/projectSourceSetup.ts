import * as vscode from 'vscode';
import { randomUUID } from 'node:crypto';
import { realpath } from 'node:fs/promises';
import { Project, RecordValue, ServiceTransport } from './types';
import type {SourceKind} from './sourceStatus';
import type {SourceRegistration} from './managedCore';
export type ManagedSourceRegistration=(folderPath:string,projectId:string,trusted:true,sourceKind:SourceKind)=>Promise<SourceRegistration>;
/** The browser sends neither filesystem paths nor a trust flag. Only native
 * selection + the explicit native confirmation below can register source trust.
 * Registration never initializes Git, makes commits, pins a recipe or executes.
 */
export async function registerProjectSource(client:ServiceTransport,project:Project,folder:vscode.Uri,register:ManagedSourceRegistration,assertCurrent:()=>void=()=>{}):Promise<RecordValue|undefined>{
  if(folder.scheme!=='file')throw new Error('Choose a local project folder before enabling its source.');
  const canonical=await realpath(folder.fsPath);if(canonical!==folder.fsPath)throw new Error('Choose the real project folder, not a redirected or symbolic-link path.');
  const before=await client.invoke<Project>('project.get',{project_id:project.project_id});assertCurrent();
  const selected=await vscode.window.showQuickPick<vscode.QuickPickItem&{value:SourceKind}>([
    {label:'Git checkout',description:'An existing committed Git root; the service verifies its pinned source',value:'git'},
    {label:'Imported source snapshot',description:'Extracted recipe.json and inventoried bytes; its declared commit is not authenticated Git',value:'imported_snapshot'}
  ],{title:'Choose source kind for this exact folder',placeHolder:'Select the source kind explicitly; no folder type is detected automatically',ignoreFocusOut:true});assertCurrent();
  if(!selected)return;
  if(!['git','imported_snapshot'].includes(selected.value))throw new Error('Choose Git checkout or imported snapshot explicitly. No source was registered.');
  const sourceKind=selected.value,imported=sourceKind==='imported_snapshot';
  const explanation=imported?'The folder must contain the extracted recipe.json and exact inventoried bytes. Snapshot validation checks local imported bytes; its declared source commit is not authenticated Git and does not establish the original repository or ownership.':'The folder must already be a committed Git root.';
  const confirmed=await vscode.window.showWarningMessage(`Allow this model project to execute trusted code from this exact ${imported?'imported source snapshot':'local Git checkout'}?\n\nProject: ${before.model_name}\nFolder: ${canonical}\nSource kind: ${sourceKind}\n\nThis is local code with filesystem/process access, not a hostile-code sandbox. ${explanation} Protomire will register its identity only for this project and bind it at project revision ${before.revision}. Source trust is separate from license authority: registration grants no asset rights or copyright license. Review the actual applicable terms separately before recording owner-approved permitted uses. It does not create Git history, run training, pin files, install dependencies or authorize spending. Each run still requires source/dependency validation and admission.`,{modal:true},'Register trusted project source');assertCurrent();
  if(confirmed!=='Register trusted project source')return;
  const receipt=await register(canonical,before.project_id,true,sourceKind);assertCurrent();
  if(!receipt?.registered||receipt.project_id!==before.project_id||receipt.workspace_path!==canonical||receipt.source_kind!==sourceKind||receipt.operator_trusted!==true||!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(receipt.alias)||!/^sha256:[0-9a-f]{64}$/.test(receipt.repository_id))throw new Error('Managed source registration returned a different project, source kind or source identity. Nothing was bound.');
  const bound=await client.invoke<Project>('project.source.bind',{project_id:before.project_id,expected_revision:before.revision,source_alias:receipt.alias,source_repository_id:receipt.repository_id,trusted:true,idempotency_key:randomUUID()});
  return {project:bound,source:receipt,execution_started:false,recipe_pinned:false};
}
