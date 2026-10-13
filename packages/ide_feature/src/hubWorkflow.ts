import * as vscode from 'vscode';
import {randomUUID} from 'node:crypto';
import {RecordValue, ServiceTransport} from './types';
import {reviewLocalAssetRights} from './localAssetReview';

export type HubReview=(operation:string,payload:RecordValue,assertCurrent:()=>void)=>Promise<RecordValue|undefined>;

const terminal=new Set(['succeeded','failed','cancelled','interrupted','needs_reconciliation']);
const bytes=(value:number)=>`${value.toLocaleString()} bytes`;
const projectLocks=new Set<string>();
type Choice=vscode.QuickPickItem&{value:string};
async function pick(title:string,items:Choice[],check:()=>void):Promise<string|undefined>{const item=await vscode.window.showQuickPick(items,{title,ignoreFocusOut:true});check();return item?.value;}
async function inspectDocument(title:string,value:RecordValue,check:()=>void):Promise<void>{
  const document=await vscode.workspace.openTextDocument({language:'json',content:JSON.stringify({review:title,...value},null,2)});check();
  await vscode.window.showTextDocument(document,{preview:true,preserveFocus:false});check();
}
/** Native dialogs only: provider credentials never enter a webview or a request. */
export async function openHubWorkflow(client:ServiceTransport,projectId:string,assertCurrent:()=>void,review?:HubReview):Promise<RecordValue|undefined>{
  if(projectLocks.has(projectId))throw new Error('A Hub workflow is already open for this project. Finish or cancel it first.');
  projectLocks.add(projectId);
  const invoke=operationInvoker(client,projectId,assertCurrent,review);
  try{
    assertCurrent();
    const action=await pick('Model and dataset Hub',[{label:'Review local source, model or dataset rights',value:'localrights'},{label:'Link reviewed policies to a registered dataset',value:'datasetrights'},{label:'Inspect repository and local changes',description:'Immutable Hub files alongside source Git',value:'repository'},{label:'Download a base model',description:'Optional: scratch models do not need a base',value:'model'},{label:'Download a dataset',value:'dataset'},{label:'Publish model or dataset files',description:'Exact preview and a separate publisher grant required',value:'publish'},{label:'Manage transfers',value:'transfers'},{label:'Use verified local assets',value:'assets'}],assertCurrent);
    if(!action)return;
    if(action==='transfers')return manageTransfers(invoke,assertCurrent);
    if(action==='assets')return useAssets(invoke,assertCurrent);
    const capabilities=await invoke('hub.providers');
    if(action==='localrights')return reviewLocalAssetRights(invoke,capabilities,assertCurrent);
    if(action==='datasetrights')return linkDatasetRights(invoke,assertCurrent);
    const providers:RecordValue[]=capabilities.providers??[];
    if(!providers.length)throw new Error('No reviewed Hub runtime is configured. Configure an isolated Hub SDK interpreter in the service operator settings.');
    const provider=await pick('Hub provider',providers.filter(item=>action==='publish'?item.publish:item.download).map(item=>({label:item.id,description:item.sdk??'',value:item.id})),assertCurrent);
    if(!provider)return;
    const accounts:Choice[]=(capabilities.accounts??[]).map((name:string)=>({label:name,description:'Existing secure account alias',value:name}));
    if(action!=='publish')accounts.unshift({label:'Public access',description:'No account credential',value:''});
    if(!accounts.length)throw new Error('Publishing requires an existing OS-keyring account alias configured by the operator. This dialog does not accept credentials.');
    const account=await pick('Hub account',accounts,assertCurrent);if(account===undefined)return;
    const identity:RecordValue={provider,...(account?{account_alias:account}:{})};
    if(action==='publish')return publishFiles(invoke,identity,capabilities,assertCurrent,review);
    if(action==='repository')return browseRepository(invoke,identity,capabilities,assertCurrent);
    identity.repo_type=action;
    const query=await vscode.window.showInputBox({title:`Search ${action==='model'?'base models':'datasets'}`,prompt:'Repository name or search terms',ignoreFocusOut:true});assertCurrent();if(!query?.trim())return;
    const found=await invoke('hub.search',{...identity,query:query.trim(),limit:30});
    const repository=await pick('Choose repository',(found.items??[]).map((item:RecordValue)=>({label:item.repo_id,description:[item.private?'Private':'Public',item.gated?'Gated':''].filter(Boolean).join(' · '),value:item.repo_id})),assertCurrent);if(!repository)return;
    identity.repo_id=repository;
    const revision=await vscode.window.showInputBox({title:'Repository revision',value:'main',prompt:'The service resolves this to an immutable commit before downloading',ignoreFocusOut:true});assertCurrent();if(!revision?.trim())return;
    const inspected=await invoke('hub.inspect',{...identity,remote_revision:revision.trim()});
    if(!['available','gated_authorized'].includes(inspected.access_state))throw new Error(`Repository access is ${inspected.access_state}. Resolve private or gated access on the provider website, then inspect again.`);
    const selected=await vscode.window.showQuickPick<vscode.QuickPickItem&{item:RecordValue}>((inspected.files??[]).map((item:RecordValue)=>({label:item.path,description:bytes(item.size_bytes),item})),{title:`Select exact files at ${inspected.commit}`,canPickMany:true,ignoreFocusOut:true});assertCurrent();if(!selected?.length)return;
    const evidence=await invoke('hub.license.inspect',{...identity,remote_revision:inspected.commit},true);
    const policy=await reviewLicense(invoke,evidence,['download'],assertCurrent);if(!policy)return;
    const names=new Set([...selected.map(item=>item.item.path),...evidence.evidence.documents.map((item:RecordValue)=>item.path)]);
    const total=inspected.files.filter((item:RecordValue)=>names.has(item.path)).reduce((sum:number,item:RecordValue)=>sum+item.size_bytes,0);
    const planned=await invoke('hub.download.plan',{...identity,remote_revision:inspected.commit,files:selected.map(item=>item.item.path),max_bytes:total,license_policy_id:policy.policy_id},true);
    await inspectDocument('Exact download plan',planned,assertCurrent);
    const confirmed=await vscode.window.showWarningMessage(`Download the exact plan (${bytes(total)}) from ${repository}, including captured license and notice files? The reviewed usage context and every source hash are bound to this plan.`,{modal:true},'Download reviewed files');assertCurrent();if(confirmed!=='Download reviewed files')return;
    const transfer=await invoke('hub.download.start',{plan_id:planned.plan_id,plan_digest:planned.plan_digest,accept_license:true},true);
    return monitorTransfer(invoke,transfer,assertCurrent);
  }finally{projectLocks.delete(projectId);}
}

type Invoke=(op:string,p?:RecordValue,mutating?:boolean)=>Promise<RecordValue>;
function operationInvoker(client:ServiceTransport,projectId:string,check:()=>void,review?:HubReview):Invoke{
  return async(op,p={},mutating=false)=>{check();const payload={project_id:projectId,...p,...(mutating?{idempotency_key:randomUUID()}:{})};
    const result=review&&['hub.license.review','hub.license.revoke','hub.source.policy.replace'].includes(op)?await review(op,payload,check):await client.invoke(op,payload);check();
    if(!result)throw new Error('The native rights review was not approved. No further Hub action was taken.');return result;
  };
}
async function approvedPublication(invoke:Invoke,preview:RecordValue,check:()=>void,review?:HubReview):Promise<RecordValue|undefined>{
  if(review){
    const transfer=await review('desktop.publish.reviewed',{project_id:preview.project_id,preview_id:preview.preview_id,payload_digest:preview.payload_digest,idempotency_key:randomUUID()},check);check();
    if(!transfer){await invoke('hub.publish.discard',{preview_id:preview.preview_id},true);return;}
    return monitorTransfer(invoke,transfer,check);
  }
  const confirmed=await vscode.window.showWarningMessage(`Publish exactly ${preview.payload.files.length} files (${bytes(preview.payload.total_bytes)}) to ${preview.payload.repo_id}, ${preview.payload.visibility}, license ${preview.payload.license}? Review the source rights, intended context, notices, destination and changes in the opened exact preview.`,{modal:true},'Approve exact publication');check();
  if(confirmed!=='Approve exact publication'){await invoke('hub.publish.discard',{preview_id:preview.preview_id},true);return;}
  const approval=await invoke('hub.publish.approve',{preview_id:preview.preview_id,payload_digest:preview.payload_digest,confirm_publication:true},true);
  return monitorTransfer(invoke,await invoke('hub.publish.start',{preview_id:preview.preview_id,payload_digest:preview.payload_digest,approval_id:approval.approval_id},true),check);
}
/** Explicit review of actual evidence. Empty context/permissions never grant rights. */
async function reviewLicense(invoke:Invoke,evidence:RecordValue,uses:string[],check:()=>void):Promise<RecordValue|undefined>{
  await inspectDocument('Immutable license evidence: read every applicable term and notice',evidence,check);
  const template={license_document_paths:[],permitted_uses:uses,intended_context:'',
    reviewed_usage_context:{purpose:'',commercial:null,deployment:'',conditions:[]},
    required_files:evidence.evidence.documents.filter((item:RecordValue)=>!/^readme(?:\.|$)/i.test(item.path)).map((item:RecordValue)=>({path:item.path,sha256:item.sha256})),
    required_attributions:[],allowed_destination_licenses:[],gated_terms_acknowledged:false};
  const document=await vscode.workspace.openTextDocument({language:'json',content:JSON.stringify(template,null,2)});check();
  await vscode.window.showTextDocument(document,{preview:false});check();
  const choice=await vscode.window.showInformationMessage('Edit this review JSON after reading the captured terms. Identify the actual license document paths, exact intended use context, notices, attribution and permitted destination licenses. This records your authorized decision; labels and account access grant no rights. Click only when the review is complete.','Record reviewed policy');check();
  if(choice!=='Record reviewed policy')return;
  let policy:RecordValue;try{policy=JSON.parse(document.getText());}catch{throw new Error('The license policy review must be valid JSON.');}
  return invoke('hub.license.review',{...policy,evidence_id:evidence.evidence_id,evidence_digest:evidence.evidence_digest,confirm_review:true},true);
}
async function linkDatasetRights(invoke:Invoke,check:()=>void):Promise<RecordValue|undefined>{
  const datasets=(await invoke('dataset.list')).datasets??[];
  const id=await pick('Registered dataset revision',datasets.map((item:RecordValue)=>({label:item.name??item.dataset_id,description:`Revision ${item.revision}`,value:item.dataset_id})),check);if(!id)return;
  const dataset=datasets.find((item:RecordValue)=>item.dataset_id===id);
  const policies=(await invoke('hub.license.policy.list')).policies.filter((item:RecordValue)=>item.status==='reviewed');
  const policyId=await pick('Reviewed source policy',policies.map((item:RecordValue)=>({label:item.policy_id,description:`${item.source.provider}/${item.source.repo_id} revision ${item.revision}`,value:item.policy_id})),check);if(!policyId)return;
  const policy=policies.find((item:RecordValue)=>item.policy_id===policyId);
  await inspectDocument('Exact registered dataset and available reviewed source policies',{dataset,policies},check);
  const template={source_policies:Object.fromEntries(dataset.manifest.sources.map((item:RecordValue)=>[item.id,policyId])),intended_use:'',usage_context:policy.reviewed_usage_context};
  const document=await vscode.workspace.openTextDocument({language:'json',content:JSON.stringify(template,null,2)});check();await vscode.window.showTextDocument(document,{preview:false});check();
  const answer=await vscode.window.showInformationMessage('Set the explicit intended_use and a reviewed policy ID for every source. The service verifies each exact source hash and usage context. This immutable link does not grant new permissions or move the dataset.','Link exact reviewed policies');check();if(answer!=='Link exact reviewed policies')return;
  let body:RecordValue;try{body=JSON.parse(document.getText());}catch{throw new Error('Dataset policy links must be valid JSON.');}
  return invoke('hub.dataset.policy.bind',{...body,dataset_id:id,dataset_revision:dataset.revision},true);
}
async function publicationRights(invoke:Invoke,selection:RecordValue,repoType:string,files:string[],budget:number,license:string,check:()=>void):Promise<RecordValue|undefined>{
  if(selection.source_alias){
    const evidence=await invoke('hub.license.local.inspect',{source_alias:selection.source_alias,repo_type:repoType,files,max_bytes:budget,declared_license:license},true);
    const policy=await reviewLicense(invoke,evidence,['download','publish'],check);if(!policy)return;
    const asset=await invoke('hub.local.register',{evidence_id:evidence.evidence_id,license_policy_id:policy.policy_id},true);
    return {source_asset_ids:[asset.asset_id],usage_context:policy.reviewed_usage_context};
  }
  const raw=await vscode.window.showInputBox({title:'Reviewed publication usage context',prompt:'Exact JSON matching the upstream policy: purpose, commercial, deployment, conditions. No purpose is inferred from the license label.',ignoreFocusOut:true});check();if(!raw)return;
  try{return {usage_context:JSON.parse(raw)};}catch{throw new Error('Usage context must be valid JSON.');}
}
async function publishFiles(invoke:Invoke,identity:RecordValue,capabilities:RecordValue,check:()=>void,review?:HubReview):Promise<RecordValue|undefined>{
  const type=await pick('Publication type',[{label:'Model',value:'model'},{label:'Dataset',value:'dataset'}],check);if(!type)return;
  identity={...identity,repo_type:type};
  const assets=(await invoke('hub.asset.list')).assets??[];
  const sources:Choice[]=[...(capabilities.publication_sources??[]).map((alias:string)=>({label:alias,description:'Operator-approved local files',value:`local:${alias}`})),...assets.filter((asset:RecordValue)=>asset.repo_type===type).map((asset:RecordValue)=>({label:`${asset.repo_id} @ ${asset.commit.slice(0,12)}`,description:'Verified downloaded asset',value:`asset:${asset.asset_id}`}))];
  const source=await pick('Publication source',sources,check);if(!source)return;
  let files:RecordValue[],selection:RecordValue;
  if(source.startsWith('local:')){selection={source_alias:source.slice(6)};files=(await invoke('hub.local.files',selection)).files;}else{selection={asset_id:source.slice(6)};files=assets.find((asset:RecordValue)=>asset.asset_id===selection.asset_id).files;}
  const selected=await vscode.window.showQuickPick<vscode.QuickPickItem&{item:RecordValue}>(files.map(item=>({label:item.path,description:bytes(item.size_bytes),item})),{title:'Select exact publication files; new repositories require a licensed README.md',canPickMany:true,ignoreFocusOut:true});check();if(!selected?.length)return;
  const destination=await vscode.window.showInputBox({title:'Destination repository',prompt:'Exact owner/name. An existing repo will only receive these selected files; no files are deleted.',ignoreFocusOut:true,validateInput:value=>/^[A-Za-z0-9][A-Za-z0-9_.-]*\/[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(value)?undefined:'Use owner/name'});check();if(!destination)return;
  const creation=await pick('Destination repository',[{label:'Update an existing repository',value:'existing'},{label:'Create a new repository',description:'Creation is included in the exact publication approval',value:'new'}],check);if(!creation)return;
  const visibility=await pick('Destination visibility',[{label:'Private',value:'private'},{label:'Public',description:'Selected bytes become public',value:'public'}],check);if(!visibility)return;
  const license=await vscode.window.showInputBox({title:'License identifier',prompt:'Must match README.md metadata exactly, for example mit or apache-2.0',ignoreFocusOut:true});check();if(!license?.trim())return;
  const rights=await publicationRights(invoke,selection,type,selected.map(item=>item.item.path),selected.reduce((sum,item)=>sum+item.item.size_bytes,0),license.trim(),check);if(!rights)return;
  const preview=await invoke('hub.publish.preview',{...identity,...selection,...rights,repo_id:destination,visibility,license:license.trim(),create_repo:creation==='new',files:selected.map(item=>item.item.path),max_bytes:selected.reduce((sum,item)=>sum+item.item.size_bytes,0)},true);
  await inspectDocument('Exact publication payload and change effects',preview,check);
  return approvedPublication(invoke,preview,check,review);
}

async function monitorTransfer(invoke:Invoke,initial:RecordValue,check:()=>void):Promise<RecordValue>{
  return vscode.window.withProgress({location:vscode.ProgressLocation.Notification,title:initial.kind==='publish'?'Publishing exact approved files':'Downloading verified Hub files',cancellable:true},async(progress,cancellation)=>{
    let current=initial,cancelSent=false;
    while(!terminal.has(current.state)){
      check();
      if(cancellation.isCancellationRequested&&!cancelSent){cancelSent=true;await invoke('hub.transfer.cancel',{transfer_id:current.transfer_id},true);}
      progress.report({message:`${current.phase} · ${bytes(current.verified_bytes??0)} of ${bytes(current.total_bytes)}`});
      await new Promise(resolve=>setTimeout(resolve,500));check();
      current=await invoke('hub.transfer.get',{transfer_id:current.transfer_id});
    }
    if(current.state==='succeeded')await vscode.window.showInformationMessage(current.kind==='publish'?`Publication verified at immutable commit ${current.result.commit}.`:'All selected files are hash-verified and available under Use verified local assets.');
    else await vscode.window.showWarningMessage(`Hub transfer ${current.state}. ${current.state==='needs_reconciliation'?'The provider may have accepted the publication. Use Manage transfers to reconcile; do not resubmit.':'Use Manage transfers to inspect or explicitly retry.'}`);
    return current;
  });
}

async function manageTransfers(invoke:Invoke,check:()=>void):Promise<RecordValue|undefined>{
  const transfers=(await invoke('hub.transfer.list')).transfers??[];
  const id=await pick('Hub transfers',transfers.map((row:RecordValue)=>({label:`${row.kind}: ${row.state}`,description:`${row.transfer_id} · ${bytes(row.verified_bytes??0)}/${bytes(row.total_bytes)}`,value:row.transfer_id})),check);if(!id)return;
  const row=await invoke('hub.transfer.get',{transfer_id:id});
  await inspectDocument('Durable transfer status',row,check);
  if(!terminal.has(row.state))return monitorTransfer(invoke,row,check);
  if(row.state==='needs_reconciliation'){
    const answer=await vscode.window.showInformationMessage('Check whether the provider accepted this exact publication? This sends no files.',{modal:true},'Reconcile publication');check();
    if(answer==='Reconcile publication'){const result=await invoke('hub.publish.reconcile',{transfer_id:id},true);await inspectDocument('Publication reconciliation',result,check);return result;}
  }else if(row.kind==='download'&&['failed','cancelled','interrupted'].includes(row.state)){
    const answer=await vscode.window.showInformationMessage('Resume the original immutable download plan and byte budget?',{modal:true},'Retry download');check();
    if(answer==='Retry download')return monitorTransfer(invoke,await invoke('hub.transfer.retry',{transfer_id:id},true),check);
  }
  return row;
}

async function useAssets(invoke:Invoke,check:()=>void):Promise<RecordValue|undefined>{
  const assets=(await invoke('hub.asset.list')).assets??[];
  const id=await pick('Verified Hub assets',assets.map((asset:RecordValue)=>({label:asset.repo_id,description:`${asset.repo_type} · ${asset.commit} · ${bytes(asset.total_bytes)}`,value:asset.asset_id})),check);if(!id)return;
  const asset=await invoke('hub.asset.get',{asset_id:id});
  await inspectDocument('Verified asset and portable resource descriptors',asset,check);
  const answer=await vscode.window.showInformationMessage(asset.repo_type==='dataset'?`Dataset files are available to the dataset manifest editor under root alias ${asset.dataset_root_alias}. Add their pinned resource descriptors to the recipe?`:'Add these pinned model-file resources to the current recipe? Choose a compatible model initializer separately. Scratch initialization remains available.',{modal:true},'Add resources and reviewed usage context');check();
  if(answer!=='Add resources and reviewed usage context')return asset;
  const project=await invoke('project.get');
  const recipe=await invoke('recipe.get',{recipe_id:project.recipe_id});
  return invoke('hub.asset.bind',{asset_id:id,expected_revision:recipe.revision,usage_context:asset.usage_context},true);
}

async function browseRepository(invoke:Invoke,identity:RecordValue,capabilities:RecordValue,check:()=>void):Promise<RecordValue|undefined>{
  const type=await pick('Repository type',[{label:'Model',value:'model'},{label:'Dataset',value:'dataset'}],check);if(!type)return;
  const repository=await vscode.window.showInputBox({title:'Hub repository',prompt:'Exact owner/name',ignoreFocusOut:true});check();if(!repository?.trim())return;
  const revision=await vscode.window.showInputBox({title:'Repository revision',value:'main',prompt:'Resolved to an immutable commit',ignoreFocusOut:true});check();if(!revision?.trim())return;
  const inspected=await invoke('hub.inspect',{...identity,repo_type:type,repo_id:repository.trim(),remote_revision:revision.trim()});
  const source=await pick('Compare local source',[{label:'Inspect remote files only',value:''},...(capabilities.publication_sources??[]).map((alias:string)=>({label:alias,description:'Operator-approved model or dataset files',value:alias}))],check);if(source===undefined)return;
  const result=source?await invoke('hub.repository.compare',{...identity,repo_type:type,repo_id:repository.trim(),remote_revision:inspected.commit,source_alias:source,max_bytes:capabilities.max_bytes}):{provider:identity.provider,repo_id:repository.trim(),repo_type:type,commit:inspected.commit,changes:inspected.files.map((file:RecordValue)=>({path:file.path,state:'remote_only',local:null,remote:file}))};
  await inspectDocument('Hub repository and local change comparison',result,check);
  return result;
}

/** Invoked by the native repository view's staged-upload action. */
export async function publishStagedHubFiles(client:ServiceTransport,projectId:string,selection:RecordValue,check:()=>void,review?:HubReview):Promise<RecordValue|undefined>{
  if(projectLocks.has(projectId))throw new Error('A Hub workflow is already open for this project. Finish or cancel it first.');
  projectLocks.add(projectId);
  try{return await publishStaged(client,projectId,structuredClone(selection),check,review);}finally{projectLocks.delete(projectId);}
}
async function publishStaged(client:ServiceTransport,projectId:string,selection:RecordValue,check:()=>void,review?:HubReview):Promise<RecordValue|undefined>{
  if(!selection.files?.length||!selection.source_alias)throw new Error('Stage local files from an approved source before publishing.');
  const invoke=operationInvoker(client,projectId,check,review);
  const capabilities=await invoke('hub.providers');
  const account=await pick('Publisher account',(capabilities.accounts??[]).map((alias:string)=>({label:alias,value:alias})),check);if(!account)return;
  const visibility=await pick('Destination visibility',[{label:'Private',value:'private'},{label:'Public',value:'public'}],check);if(!visibility)return;
  const license=await vscode.window.showInputBox({title:'License identifier',prompt:'Must match the staged or existing remote README.md metadata',ignoreFocusOut:true});check();if(!license?.trim())return;
  const {provider,repo_id,repo_type,source_alias,files}=selection;
  const rights=await publicationRights(invoke,{source_alias},repo_type,files,capabilities.max_bytes,license.trim(),check);if(!rights)return;
  const preview=await invoke('hub.publish.preview',{...rights,provider,repo_id,repo_type,source_alias,files,account_alias:account,visibility,license:license.trim(),max_bytes:capabilities.max_bytes,create_repo:false,expected_remote_commit:selection.inspected_commit},true);
  await inspectDocument('Exact staged publication and remote changes',preview,check);
  return approvedPublication(invoke,preview,check,review);
}
