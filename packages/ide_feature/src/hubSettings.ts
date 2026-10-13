/** Native owner settings: existing OS-keyring references, never token text. */
import * as vscode from 'vscode';
import { createHash, randomBytes } from 'node:crypto';
import type { ManagedCore, HubSettingsReview } from './managedCore';

const BUDGET_UNITS=['B','KiB','MiB','GiB','TiB','PiB'] as const;
export function parseHubBudget(text:string):number{
  const matched=/^([0-9]+)\s*(B|KiB|MiB|GiB|TiB|PiB)?$/i.exec(text.trim());
  if(!matched||matched[1].length>16)throw new Error('Use a whole number of bytes or binary units, such as 64 GiB or 2 TiB.');
  const unit=BUDGET_UNITS.findIndex(item=>item.toLowerCase()===(matched[2]??'B').toLowerCase());
  const bytes=BigInt(matched[1])*(1024n**BigInt(unit));
  if(bytes<1n||bytes>2n**50n)throw new Error('Transfer limit must be between 1 byte and 1 PiB.');
  return Number(bytes);
}
export function formatHubBudget(bytes:number):string{
  for(let unit=BUDGET_UNITS.length-1;unit>=0;unit--)if(bytes%(1024**unit)===0)return `${bytes/(1024**unit)} ${BUDGET_UNITS[unit]}`;
  return `${bytes} B`;
}

export async function configureHub(core:ManagedCore,project:{project_id:string;model_name:string},assertCurrent:()=>void):Promise<void>{
  assertCurrent();
  const settings=await core.hubSettings(project.project_id);
  assertCurrent();
  const choice=await vscode.window.showQuickPick([
    {label:'Set this project’s transfer size limit',action:'budget'},
    {label:'Connect using a Hugging Face token',action:'connect'},
    {label:'Link an existing OS-keyring account',action:'account'},
    {label:'Select a publication folder',action:'folder'},
    {label:'Disconnect this project’s account',action:'disconnect'},
    {label:'Remove this project’s publication folder',action:'remove'}
  ],{title:`Hub settings: ${project.model_name}`,placeHolder:'Public Hub access needs no account. Credentials are saved only in a supported OS-backed keyring.'});
  if(!choice)return;
  let account=settings.account?{keyring_service:settings.account.keyring_service,username:settings.account.username}:null;
  let folder=settings.folder?.path??null;
  let budget=settings.max_bytes??16*1024**3;
  let secret:string|undefined;
  if(choice.action==='budget'){
    const input=await vscode.window.showInputBox({title:'Project transfer size limit',value:formatHubBudget(budget),prompt:'Whole-number bytes or binary units, e.g. 64 GiB or 2 TiB. Maximum 1 PiB. This caps each transfer; actual disk reservation is still checked at start and retry.',ignoreFocusOut:true});
    if(input===undefined)return;
    budget=parseHubBudget(input);
  }else if(choice.action==='connect'){
    secret=await vscode.window.showInputBox({title:'Hugging Face access token',prompt:'Enter your token yourself. After confirmation, it will be saved persistently in your OS-backed keyring for this project’s Hub access. Use a fine-grained token with only the repositories and rights you intend.',password:true,ignoreFocusOut:true});
    if(secret===undefined)return;
    if(secret.length<8||secret.length>512||/\s/.test(secret))throw new Error('Enter a valid access token without whitespace.');
    account={keyring_service:'ModelBuilder.HuggingFace',username:'modelbuilder-'+createHash('sha256').update(project.project_id).digest('hex').slice(0,16)+'-'+randomBytes(16).toString('hex')};
  }else if(choice.action==='account'){
    const service=await vscode.window.showInputBox({title:'Existing OS-keyring service name',prompt:'Lookup name only. Do not enter a token or password. No account authentication is performed here.',value:account?.keyring_service,ignoreFocusOut:true});
    if(service===undefined)return;
    const username=await vscode.window.showInputBox({title:'Existing OS-keyring username',prompt:'The lookup username paired with that service. Never enter an access token.',value:account?.username,ignoreFocusOut:true});
    if(username===undefined)return;
    account={keyring_service:service,username};
  }else if(choice.action==='folder'){
    const selected=await vscode.window.showOpenDialog({title:'Select this project’s publication folder',canSelectFiles:false,canSelectFolders:true,canSelectMany:false,openLabel:'Review this folder'});
    if(!selected?.length)return;
    if(selected[0].scheme!=='file')throw new Error('Select a local native directory for publication.');
    folder=selected[0].fsPath;
  }else if(choice.action==='disconnect')account=null;
  else folder=null;
  assertCurrent();
  const review:HubSettingsReview=await core.previewHubSettings(project.project_id,settings.revision,account,folder,choice.action==='folder',budget);
  assertCurrent();
  const linked=review.plan.account;
  const detail=[`Project: ${project.model_name} (${project.project_id})`,
    linked?`OS-keyring lookup: ${linked.keyring_service} / ${linked.username}`:'No linked account: public unauthenticated access only.',
    `Per-transfer project limit: ${formatHubBudget(review.plan.max_bytes)} (${review.plan.max_bytes} bytes). Every plan still binds its exact files, byte ceiling and disk reservation; this limit is not a promise of free disk space.`,
    `Publication folder: ${review.plan.folder?.path??'none'}`,
    ...(secret!==undefined?['You are authorizing persistent storage of the token you entered in your OS-backed keyring. It is used only by the bundled Hugging Face worker for this project’s requested Hub operations. No plaintext fallback is allowed. This step does not contact Hugging Face or validate the token. Disconnecting the project removes its app reference; the OS-keyring item remains until you remove it in your OS credential manager.']:[]),
    'The app stores lookup names and directory identity in private local app state. It does not store a token here. The bundled worker may read this existing OS-backed credential only for this project’s requested Hub operations. Credential validity is not checked now.',
    'Folder selection allows local inspection and reviewed snapshots. Publication still requires a separate exact-files confirmation. Existing project previews are invalidated and active transfers receive cancellation requests. Already transmitted files cannot be undone.',
    'These settings constrain the app bridge, not other programs running as your OS user.'
  ].join('\n\n');
  const confirm=secret!==undefined?'Save token and apply settings':'Apply settings';
  const decision=await vscode.window.showWarningMessage('Apply these project Hub settings?',{modal:true,detail},confirm);
  if(decision!==confirm){secret=undefined;return;}
  assertCurrent();
  let stored=false;
  if(secret!==undefined&&account){
    const receipt=await core.storeHubCredential(account,secret);secret=undefined;
    if(receipt.status!=='stored')throw new Error(receipt.status==='uncertain'?`Credential storage is uncertain. Settings were not linked. Check your OS credential manager for ${account.keyring_service} / ${account.username} before retrying.`:'The credential was not stored. A supported unlocked OS-backed keyring is required.');
    stored=true;
  }
  let result;
  try{assertCurrent();result=await core.applyHubSettings(review);}
  catch{if(stored&&account)throw new Error(`The credential was saved, but project settings were not confirmed. Check current Hub settings and OS-keyring item ${account.keyring_service} / ${account.username} before retrying.`);throw new Error('Hub settings were not confirmed. Inspect current settings before retrying.');}
  assertCurrent();
  if(result.project_id!==project.project_id||result.revision!==settings.revision+1)throw new Error('Hub settings returned a mismatched receipt. Inspect current settings before retrying.');
  void vscode.window.showInformationMessage('Hub settings saved. Credentials have not been validated with Hugging Face. Reopen Hub to refresh sources and accounts.');
}
