import * as vscode from 'vscode';
import { lstat, realpath, stat } from 'node:fs/promises';
import { isAbsolute, join, normalize, relative, resolve, sep } from 'node:path';
import { randomUUID } from 'node:crypto';
import { Project, Recipe, RecordValue, ServiceTransport } from './types';
export type SourceChoice={alias:string;repository_id:string;workspace_path:string;label?:string;operator_trusted:boolean;available:boolean};
/** Compare only a human-chosen or already-open local folder; never open a service path blindly. */
export async function matchLocalSource(folder:vscode.Uri,sources:SourceChoice[]):Promise<SourceChoice> {
  if(folder.scheme!=='file')throw new Error('Choose a local source folder. Remote workspace locations cannot be assumed to match the service checkout.');
  const canonical=await realpath(folder.fsPath);if(!(await stat(canonical)).isDirectory())throw new Error('Choose a source folder.');
  const matches=sources.filter(source=>source.available===true&&typeof source.workspace_path==='string'&&isAbsolute(source.workspace_path)&&normalize(source.workspace_path)===normalize(canonical));
  if(matches.length!==1)throw new Error(matches.length?'This folder has multiple approved aliases. Ask the operator to remove the ambiguity.':'This local folder does not match an operator-approved source checkout. Ask the service operator to configure its alias. No client path was sent.');
  const source=matches[0];if(!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(source.alias)||!/^sha256:[0-9a-f]{64}$/.test(source.repository_id))throw new Error('The service returned an invalid source identity. Reload before choosing a source.');
  return source;
}
export async function chooseLocalSource(client:ServiceTransport,projectId?:string):Promise<{source:SourceChoice;folder:vscode.Uri}|undefined> {
  const {sources}=await client.invoke('project.source.list',projectId?{project_id:projectId}:{});
  const opened=vscode.workspace.workspaceFolders??[];
  if(opened.length>1)throw new Error('Open one source folder for this model project before binding it.');
  const folder=opened[0]?.uri??(await vscode.window.showOpenDialog({title:'Open model source folder',openLabel:'Use source folder',canSelectMany:false,canSelectFiles:false,canSelectFolders:true}))?.[0];
  if(!folder)return;
  return {source:await matchLocalSource(folder,sources??[]),folder};
}
export async function acknowledgeSource(choice:{source:SourceChoice;folder:vscode.Uri}):Promise<boolean> {
  const {source,folder}=choice;
  if(!vscode.workspace.isTrusted)throw new Error('Review and trust this native workspace before binding executable source. ModelBuilder never grants workspace trust automatically.');
  return await vscode.window.showWarningMessage(`Bind this model project to ${source.label??source.alias} (${source.alias})?\nLocal folder: ${folder.fsPath}\nRepository identity: ${source.repository_id}\nThis is trusted local code with your filesystem and process access, not a hostile-code sandbox. ${source.operator_trusted?'The operator has approved execution.':'The operator has not approved execution; runs remain blocked.'} Binding does not run code. Each run still requires a separate decision.`,{modal:true},'Bind trusted source')==='Bind trusted source';
}
export function sourceBindingPayload(choice:{source:SourceChoice},projectId:string,revision:number,key:string):RecordValue {
  return {project_id:projectId,expected_revision:revision,source_alias:choice.source.alias,source_repository_id:choice.source.repository_id,trusted:true,idempotency_key:key};
}

export type SourcePinContext={project:Project;recipe:Recipe;sourceRepositoryId:string;assertCurrent:()=>void};
const fullCommit=/^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$/;
function safeRelativePath(path:string):boolean{return Boolean(path)&&path.length<=512&&!isAbsolute(path)&&!/[\\\x00-\x1f\x7f]/.test(path)&&!path.split('/').some(part=>!part||part==='.'||part==='..');}
export function requiredSourcePinPaths(recipe:Recipe):string[]{
  const paths=[...recipe.nodes.map(item=>item.implementation_ref?.path),recipe.environment?.lock_file?.path].filter((path):path is string=>typeof path==='string');
  if(paths.some(path=>!safeRelativePath(path)))throw new Error('The recipe contains an unsafe required source path. Correct it before pinning source.');
  return [...new Set(paths)].sort();
}
/** A picker can navigate outside its default folder. Check lexical and canonical containment before sending relative paths. */
export async function localSourcePinPaths(folder:vscode.Uri,selected:readonly vscode.Uri[],required:string[]=[]):Promise<string[]>{
  if(folder.scheme!=='file')throw new Error('Source pinning requires a local workspace.');
  const root=await realpath(folder.fsPath),paths=[...required];
  for(const file of selected){if(file.scheme!=='file')throw new Error('Choose local files inside the bound source folder.');const path=relative(resolve(folder.fsPath),resolve(file.fsPath)).split(sep).join('/');if(!safeRelativePath(path))throw new Error('Every selected file must be inside the bound source folder. No client paths were sent.');paths.push(path);}
  const unique=[...new Set(paths)].sort();if(!unique.length||unique.length>1024)throw new Error('Choose between 1 and 1024 source files.');
  for(const path of unique){if(!safeRelativePath(path))throw new Error('Unsafe relative source path.');let current=root;for(const part of path.split('/')){current=join(current,part);if((await lstat(current)).isSymbolicLink())throw new Error(`Source symlinks are not supported: ${path}`);}if(!(await stat(current)).isFile()||await realpath(current)!==current)throw new Error(`Choose a regular source file: ${path}`);}
  return unique;
}
function sourcePinReviewText(review:RecordValue,commit:string,required:string[]):string{
  return ['Review source pin',`Project: ${review.project_id}`,`Recipe: ${review.recipe_id}`,`Recipe revision: ${review.current_revision} → ${review.next_revision}`,`Project revision: ${review.project_revision}`,`Source alias: ${review.source_binding.alias}`,`Repository identity: ${review.source_binding.repository_id}`,`Full Git commit: ${commit}`,'','This creates one recipe revision. It does not modify Git, install dependencies, or execute code.','',`Files (${review.file_inventory.length}) from the verified commit:`,...review.file_inventory.map((file:RecordValue)=>`${file.path}${required.includes(file.path)?' [required by recipe]':''}\n  SHA-256: ${file.sha256}`),'',`Proposed recipe digest: ${review.proposed_recipe_digest}`,'','Previous source:',JSON.stringify(review.before_source,null,2),'','Reviewed source:',JSON.stringify(review.after_source,null,2)].join('\n');
}
/** Native selection and review only. The webview cannot supply a file path or bypass confirmation. */
export async function reviewAndPinSource(client:ServiceTransport,context:SourcePinContext):Promise<RecordValue|undefined>{
  context.assertCurrent();
  const folders=vscode.workspace.workspaceFolders??[];if(folders.length!==1)throw new Error('Open the one bound source folder before pinning recipe source.');const folder=folders[0].uri;
  const guard=()=>{context.assertCurrent();const current=vscode.workspace.workspaceFolders??[];if(current.length!==1||current[0].uri.scheme!==folder.scheme||current[0].uri.fsPath!==folder.fsPath)throw new Error('The native source workspace changed. Start a new source review.');if(vscode.workspace.textDocuments?.some(doc=>doc.isDirty&&doc.uri.scheme==='file'&&safeRelativePath(relative(resolve(folder.fsPath),resolve(doc.uri.fsPath)).split(sep).join('/'))))throw new Error('Save or explicitly discard native source edits before pinning. Your unsaved files are preserved.');};
  guard();
  const status=await client.invoke('project.source.status',{project_id:context.project.project_id});guard();const binding=status.binding;
  if(!binding||!status.available||status.repository_id!==binding.repository_id||binding.repository_id!==context.sourceRepositoryId)throw new Error('The bound repository identity changed or is unavailable. Reload the project and review its current source.');
  await matchLocalSource(folder,[{...status,alias:binding.alias,repository_id:binding.repository_id,workspace_path:status.workspace_path,operator_trusted:status.operator_trusted,available:status.available}]);guard();
  const inputCommit=await vscode.window.showInputBox({title:'Choose committed recipe source',value:status.head_commit??'',prompt:`Repository ${binding.alias} · ${binding.repository_id}. The displayed default is the checkout HEAD. Enter an explicit full Git commit; uncommitted edits cannot be pinned.`,validateInput:value=>fullCommit.test(value)?undefined:'Enter the complete 40- or 64-character hexadecimal Git commit.'});guard();if(inputCommit===undefined)return;if(!fullCommit.test(inputCommit))throw new Error('Enter an explicit full Git commit.');const commit=inputCommit.toLowerCase();
  const required=requiredSourcePinPaths(context.recipe);
  const selected=await vscode.window.showOpenDialog({title:'Choose committed source files (recipe implementation and lock files are also included for review)',openLabel:'Review source files',defaultUri:folder,canSelectFiles:true,canSelectFolders:false,canSelectMany:true});guard();if(!selected?.length)return;
  const paths=await localSourcePinPaths(folder,selected,required);guard();
  const payload={project_id:context.project.project_id,recipe_id:context.recipe.recipe_id,expected_revision:context.recipe.revision,expected_project_revision:context.project.revision,source_repository_id:binding.repository_id,commit,paths};
  const preview=await client.invoke('recipe.source.pin.preview',payload);guard();const review=preview.review;
  if(!review||review.project_id!==payload.project_id||review.recipe_id!==payload.recipe_id||review.current_revision!==payload.expected_revision||review.next_revision!==payload.expected_revision+1||review.project_revision!==payload.expected_project_revision||review.source_binding?.repository_id!==payload.source_repository_id||review.source_binding?.alias!==binding.alias||review.after_source?.commit?.toLowerCase()!==commit.toLowerCase()||review.execution_allowed!==false||review.git_modified!==false||!Array.isArray(review.file_inventory)||!/^[0-9a-f]{64}$/.test(preview.review_digest??''))throw new Error('The source review did not match the selected recipe, revision, commit or repository. Nothing was pinned.');
  const inventory=review.file_inventory.map((file:RecordValue)=>file.path).sort();if(JSON.stringify(inventory)!==JSON.stringify(paths)||review.file_inventory.some((file:RecordValue)=>!/^[0-9a-f]{64}$/.test(file.sha256??'')))throw new Error('The source review returned a different file inventory. Nothing was pinned.');
  const document=await vscode.workspace.openTextDocument({content:sourcePinReviewText(review,commit,required),language:'plaintext'});guard();await vscode.window.showTextDocument(document,{viewColumn:vscode.ViewColumn.Beside,preview:true});guard();
  const inspected=await vscode.window.showInformationMessage('Review the full source inventory in the native document. When you have checked the repository, commit, paths, hashes and recipe revision, continue to the final pin confirmation.','Continue to pin','Cancel');guard();if(inspected!=='Continue to pin')return;
  const answer=await vscode.window.showWarningMessage(`Pin these ${paths.length} reviewed files at commit ${commit}?\nSource: ${binding.alias}\nRepository identity: ${binding.repository_id}\nRecipe ${context.recipe.recipe_id}: revision ${review.current_revision} → ${review.next_revision}.\nReview every relative path and SHA-256 in the native review document, including required implementation and lock files. This does not modify Git or run code.`,{modal:true},'Pin reviewed source');guard();if(answer!=='Pin reviewed source')return;
  // Recheck native selection and source identity after review. Service revisions and digest are never rebased or retried.
  await localSourcePinPaths(folder,selected,required);guard();
  const current=await client.invoke('project.source.status',{project_id:context.project.project_id});guard();if(current.binding?.alias!==binding.alias||current.binding?.repository_id!==binding.repository_id||current.repository_id!==binding.repository_id||!current.available)throw new Error('The bound source changed during review. Nothing was pinned. Start a new source review.');await matchLocalSource(folder,[{...current,alias:binding.alias,repository_id:binding.repository_id,workspace_path:current.workspace_path,operator_trusted:current.operator_trusted,available:current.available}]);guard();
  return client.invoke('recipe.source.pin',{...payload,review_digest:preview.review_digest,confirm_pin:true,idempotency_key:randomUUID()});
}
