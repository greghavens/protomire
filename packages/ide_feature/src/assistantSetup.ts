import * as vscode from 'vscode';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { isAbsolute, join } from 'node:path';
import { stat } from 'node:fs/promises';
import { serviceUrl } from './security';
import { RecordValue } from './types';

export type AssistantClient='codex'|'claude'|'triggerfish'|'opencode'|'kilocode';
export interface AssistantSetupOptions {client?:AssistantClient;workspace?:vscode.Uri;projectId?:string;managed?:{python:string;command:{command:string;args:string[]}}}
export interface AssistantSetupPaths {python:string;helper:string;serviceUrl:string;ideCli?:string;extensionsDir?:string;userDataDir?:string;config?:string;workspace?:string;projectId?:string;mcpCommand?:{command:string;args:string[]}}
/** Fixed argument vectors only; the webview cannot provide executable paths or shell text. */
export function assistantSetupArguments(action:string,client:AssistantClient,paths:AssistantSetupPaths,approval?:string):string[]{
  if(!['catalog','doctor','preview-install','install','preview-config','configure','preview-project','apply-project','preview-rollback','rollback-project'].includes(action))throw new Error('Unsupported assistant setup action.');
  if(['opencode','kilocode'].includes(client)&&!['preview-project','apply-project','preview-rollback','rollback-project'].includes(action))throw new Error('This harness currently supports managed project skills and MCP setup; vendor installation and sign-in remain separate.');
  if(client==='triggerfish'&&!['preview-project','apply-project','preview-rollback','rollback-project'].includes(action))throw new Error('Triggerfish supports inactive project preparation only; vendor installation and activation are unavailable.');
  if(!isAbsolute(paths.python)||!isAbsolute(paths.helper))throw new Error('Assistant setup requires absolute interpreter and helper paths.');
  const args=['-B',paths.helper,action];
  if(action!=='catalog')args.push('--client',client);
  if(paths.extensionsDir&&['catalog','doctor','preview-install','install'].includes(action))args.push('--extensions-dir',paths.extensionsDir);
  if(['preview-install','install'].includes(action)){
    if(!paths.ideCli||!isAbsolute(paths.ideCli))throw new Error('Launch ModelBuilder with its distribution launcher or choose its absolute IDE launcher path.');
    args.push('--ide-cli',paths.ideCli);if(paths.userDataDir)args.push('--user-data-dir',paths.userDataDir);
  }
  if(['doctor','preview-config','configure'].includes(action)){
    args.push('--python',paths.python,'--service-url',serviceUrl(paths.serviceUrl));
    if(paths.config&&action!=='doctor')args.push('--config',paths.config);
    if(paths.workspace&&action!=='doctor')args.push('--workspace',paths.workspace);
  }
  if(['preview-project','apply-project','preview-rollback','rollback-project'].includes(action)){
    if(!paths.workspace||!isAbsolute(paths.workspace))throw new Error('Select an explicit local project folder.');
    args.push('--workspace',paths.workspace);
    if(['preview-project','apply-project'].includes(action)){
      if(!paths.projectId)throw new Error('Select or create one model project first.');
      args.push('--project-id',paths.projectId);
      if(paths.mcpCommand)args.push('--mcp-command-json',JSON.stringify(paths.mcpCommand));
      else args.push('--python',paths.python,'--service-url',serviceUrl(paths.serviceUrl));
    }
  }
  if(['install','configure','apply-project','rollback-project'].includes(action)){
    if(!approval||!/^[a-f0-9]{64}$/.test(approval))throw new Error('A verified setup preview approval is required.');
    args.push('--approval',approval);
  }
  return args;
}
export async function runAssistantSetup(action:string,client:AssistantClient,paths:AssistantSetupPaths,approval?:string):Promise<RecordValue>{
  const args=assistantSetupArguments(action,client,paths,approval);
  let stdout:string;
  try{const result=await promisify(execFile)(paths.python,args,{timeout:action==='install'?180000:60000,maxBuffer:2*1024*1024,windowsHide:true,encoding:'utf8'});stdout=result.stdout;}
  catch(error){const output=(error as {stdout?:string}).stdout;try{const parsed=JSON.parse(output??'');if(parsed.error?.message)throw new Error(parsed.error.message);}catch(parsed){if(parsed instanceof Error&&!(parsed instanceof SyntaxError))throw parsed;}throw new Error('Assistant setup could not complete. Check the selected interpreter, installed service and native tool availability.');}
  let result:RecordValue;try{result=JSON.parse(stdout);}catch{throw new Error('Assistant setup returned an invalid response.');}
  if(result.ok===false)throw new Error(result.error?.message??'Assistant setup failed.');
  return result;
}
async function showReport(title:string,result:RecordValue){const document=await vscode.workspace.openTextDocument({language:'json',content:JSON.stringify(result,null,2)});await vscode.window.showTextDocument(document,{preview:true});void vscode.window.showInformationMessage(title);}

let setupActive=false;
/** Optional native tools remain separate clients. Setup never invokes paid inference. */
export async function configureOptionalAssistant(context:vscode.ExtensionContext,runner:typeof runAssistantSetup=runAssistantSetup,options:AssistantSetupOptions={}):Promise<void>{
  if(setupActive)throw new Error('Finish or cancel the open optional-tool setup before starting another.');
  setupActive=true;
  try{
  if(!vscode.workspace.isTrusted)throw new Error('Trust this workspace before configuring an optional coding tool.');
  const clientChoice=options.client?{label:options.client==='codex'?'Codex':options.client==='claude'?'Claude Code':options.client==='opencode'?'OpenCode':options.client==='kilocode'?'Kilo Code':'Triggerfish',client:options.client}:await vscode.window.showQuickPick([{label:'Codex',description:'Optional OpenAI coding client',client:'codex' as const},{label:'Claude Code',description:'Optional Anthropic coding client',client:'claude' as const},{label:'OpenCode',description:'Project skills and MCP; vendor installation separate',client:'opencode' as const},{label:'Kilo Code',description:'Project skills and MCP; vendor installation separate',client:'kilocode' as const},{label:'Triggerfish',description:'Inactive compatibility bundle; vendor activation blocked',client:'triggerfish' as const}],{title:'Optional coding tool',placeHolder:'Choose an independent client. No coding assistant is required to use ModelBuilder.'});if(!clientChoice)return;
  const triggerfish=clientChoice.client==='triggerfish';
  const projectOnly=['opencode','kilocode'].includes(clientChoice.client);
  if(projectOnly&&(!options.managed||!options.projectId))throw new Error('Open a native managed model project before preparing this harness.');
  if(triggerfish&&(!options.managed||!options.projectId)){await showReport('Triggerfish needs a native managed project to prepare its inactive compatibility bundle.',{ok:true,client:'triggerfish',mcp:'modelbuilder_flat_adapter_available',installation:'UNVERIFIED',authentication:'UNVERIFIED',vendor_acceptance:'UNVERIFIED',activation:'blocked',blocker:'Upstream uses fixed gateway/CLI port 18789. Automatic project-isolated launch is unavailable; select a native managed project to prepare local inactive files.'});return;}
  const action=await vscode.window.showQuickPick(triggerfish?[{label:'Prepare inactive project skills and MCP fragment',action:'preview-project'}]:projectOnly?[{label:'Configure project skills and MCP',action:'preview-project'},{label:'Disconnect managed project integration',action:'preview-rollback'}]:[{label:options.managed?'Check installed clients (sign-in and transport unverified)':'Check MCP setup',action:options.managed?'catalog':'doctor'},{label:'Configure project skills and MCP',action:options.projectId?'preview-project':'preview-config'},{label:'Install optional extension',action:'preview-install'},{label:'View supported versions',action:'catalog'},{label:'Disconnect managed project integration',action:'preview-rollback'}],{title:`${clientChoice.label}: setup`,placeHolder:'Installation and configuration changes always have a review step.'});if(!action)return;
  const python=options.managed?.python??await vscode.window.showInputBox({title:'ModelBuilder service Python interpreter',prompt:'Absolute path to the installed service Python. It runs only the bundled setup helper, never paid model inference.',value:context.workspaceState.get<string>('modelbuilder.assistantSetupPython')??process.env.MODELBUILDER_PYTHON??'',ignoreFocusOut:true,validateInput:value=>isAbsolute(value)?null:'Choose an absolute Python interpreter path.'});if(!python)return;
  if(!(await stat(python)).isFile())throw new Error('The selected Python interpreter is not a file.');
  const paths:AssistantSetupPaths={python,helper:join(context.extensionUri.fsPath,'out','assistant_setup.py'),serviceUrl:vscode.workspace.getConfiguration('modelbuilder').get('serviceUrl','http://127.0.0.1:8765'),ideCli:process.env.MODELBUILDER_IDE_LAUNCHER??process.env.MODELBUILDER_IDE_CLI,extensionsDir:process.env.MODELBUILDER_EXTENSIONS_DIR,userDataDir:process.env.MODELBUILDER_USER_DATA_DIR};
  if(action.action==='preview-install'&&!paths.ideCli){const chosen=await vscode.window.showOpenDialog({title:'Select this ModelBuilder installation’s IDE launcher',canSelectFiles:true,canSelectFolders:false,canSelectMany:false,openLabel:'Use IDE launcher'});if(!chosen?.[0])return;paths.ideCli=chosen[0].fsPath;}
  if(['preview-config','preview-project','preview-rollback'].includes(action.action)){
    const folders=(vscode.workspace.workspaceFolders??[]).filter(folder=>folder.uri.scheme==='file');
    const selected=options.workspace??(folders.length===1?folders[0].uri:folders.length>1?(await vscode.window.showQuickPick(folders.map(folder=>({label:folder.name,description:folder.uri.fsPath,uri:folder.uri})),{title:'Choose the model project folder',placeHolder:'Only this folder receives project-local assistant files.'}))?.uri:(await vscode.window.showOpenDialog({title:'Choose the model project folder',canSelectFiles:false,canSelectFolders:true,canSelectMany:false}))?.[0]);
    if(!selected)return;if(selected.scheme!=='file')throw new Error('Select a local model project folder.');
    paths.workspace=selected.fsPath;paths.projectId=options.projectId;paths.mcpCommand=options.managed?.command;
    if(action.action==='preview-config')paths.config=join(selected.fsPath,clientChoice.client==='codex'?'.codex/config.toml':'.mcp.json');
  }
  await context.workspaceState.update('modelbuilder.assistantSetupPython',python);
  const result=await runner(action.action,clientChoice.client,paths);
  if(action.action==='doctor'||action.action==='catalog'){await showReport(`${clientChoice.label}: ${action.action==='doctor'?'setup check completed':'supported versions'}. See the report for exact readiness.`,result);return;}
  await showReport('Review the exact setup plan before continuing.',result);
  const modifying=action.action==='preview-install'?'install':action.action==='preview-project'?'apply-project':action.action==='preview-rollback'?'rollback-project':'configure';
  const label=modifying==='rollback-project'?'Disconnect integration':triggerfish?'Prepare inactive bundle':modifying==='install'?'Accept terms and install':modifying==='apply-project'?'Apply project integration':'Apply MCP configuration';
  const warning=modifying==='rollback-project'?`Remove only the reviewed managed MCP entry, skills and instruction block from ${paths.workspace}? Unrelated settings and user edits are preserved. Restart the assistant to close its existing connection. The project and model data are not removed.`:triggerfish?`Prepare the inactive Triggerfish MCP fragment and skills in ${paths.workspace}? This does not install, launch, sign in or activate Triggerfish. Upstream's fixed port 18789 prevents automatic per-project isolation. No global configuration, ports, credentials or classifications are changed. Review the plan's interpreter allowlist and activation gates.`:modifying==='install'?`Install ${result.plan?.extension_id??clientChoice.label} version ${result.plan?.version??''} for ${result.plan?.target??'this platform'}, accepting these vendor terms: ${(result.plan?.terms??[]).join(', ')}? The helper verifies the pinned hash and installed version. This does not sign in or run inference.`:modifying==='apply-project'?`Add the reviewed project-local MCP config, skills and managed instruction block in ${paths.workspace}? Existing unrelated instructions and settings are preserved. This does not sign in, install a model, grant execution access or send model prompts. Close the assistant while applying; review any override warnings in the plan.`:`Write the ModelBuilder MCP server entry to ${result.plan?.config_path??'the configuration file shown in the plan'}? Existing unrelated servers are preserved and the target file is backed up. Close the external client before applying this change and restart it afterward. The client needs MODELBUILDER_TOKEN in its own environment; the IDE credential is never copied.`;
  if(await vscode.window.showWarningMessage(warning,{modal:true},label)!==label)return;
  const completed=await runner(modifying,clientChoice.client,paths,result.approval);
  await showReport(`${clientChoice.label}: ${triggerfish?'inactive bundle prepared; vendor activation remains blocked':modifying==='install'?`extension installation verified. Accepted vendor terms: ${(result.plan?.terms??[]).join(', ')}`:'MCP configuration saved'}.`,completed);
  }finally{setupActive=false;}
}
