/** Native local rights authoring. The shared service remains the sole rights authority. */
import * as vscode from 'vscode';
import {RecordValue} from './types';
import {sourceIdentityLines} from './sourceStatus';

export type LocalRightsInvoke=(operation:string,payload?:RecordValue,mutating?:boolean)=>Promise<RecordValue>;
type Choice=vscode.QuickPickItem&{value:string};
const uses:Choice[]=[
  {label:'Acquire or copy local files',description:'download permission; needed only to register a verified copy',value:'download'},
  {label:'Train',value:'train'},{label:'Evaluate',value:'evaluate'},{label:'Generate',value:'generate'},
  {label:'Export',value:'export'},{label:'Publish',value:'publish'},{label:'Promote a model version',value:'promote'}
];
async function choose(title:string,items:Choice[],check:()=>void):Promise<string|undefined>{
  const answer=await vscode.window.showQuickPick(items,{title,ignoreFocusOut:true});check();return answer?.value;
}
async function input(title:string,prompt:string,check:()=>void,maxLength:number,value?:string):Promise<string|undefined>{
  const answer=await vscode.window.showInputBox({title,prompt,value,ignoreFocusOut:true,validateInput:text=>text.trim()&&text.trim().length<=maxLength?undefined:`Enter between 1 and ${maxLength} characters.`});check();
  if(answer===undefined)return;
  const trimmed=answer.trim();if(!trimmed||trimmed.length>maxLength)throw new Error(`${title} is required (up to ${maxLength} characters).`);return trimmed;
}
async function view(title:string,value:RecordValue,check:()=>void):Promise<void>{
  const document=await vscode.workspace.openTextDocument({language:'json',content:JSON.stringify({review:title,...value},null,2)});check();
  await vscode.window.showTextDocument(document,{preview:true,preserveFocus:false});check();
}
async function documents(title:string,items:RecordValue[],check:()=>void,required:boolean):Promise<RecordValue[]|undefined>{
  const selected=await vscode.window.showQuickPick<vscode.QuickPickItem&{item:RecordValue}>(items.map(item=>({label:item.path,description:`${item.size_bytes.toLocaleString()} bytes${item.sha256?` · SHA-256 ${item.sha256}`:''}`,item})),{title,canPickMany:true,ignoreFocusOut:true});check();
  if(!selected)return;
  if(required&&!selected.length)throw new Error('Select the actual applicable terms. Missing or unknown terms remain unresolved; no policy was recorded.');
  return selected.map(choice=>choice.item);
}
/** Every permission starts unselected; no license label is interpreted as a grant. */
async function guidedPolicy(invoke:LocalRightsInvoke,evidence:RecordValue,check:()=>void):Promise<RecordValue|undefined>{
  const origin=evidence.evidence.source_origin;
  await view('Captured source bytes and license evidence: read all applicable terms',{
    ...evidence,...(origin?{source_identity:sourceIdentityLines({...origin,declared_source_commit:origin.commit}),
      rights_review_notice:'Source identity, source trust and imported-byte validation grant no license. Only an explicit owner-approved review of the actual terms can record permitted uses.'}:{})
  },check);
  const captured:RecordValue[]=evidence.evidence.documents??[];
  if(!captured.some(item=>typeof item.text==='string'&&item.text.trim())){
    await vscode.window.showWarningMessage('No nonempty applicable terms were captured. Rights remain unresolved. Choose an actual terms document before recording a policy.');check();return;
  }
  const understood=await choose('Are all applicable rights and obligations resolved?',[
    {label:'Keep unresolved',description:'Missing terms, uncertainty, or more review needed; no permission is granted',value:'unresolved'},
    {label:'Continue an authorized review',description:'I have read the actual applicable terms and can make this decision',value:'review'}
  ],check);if(understood!=='review')return;
  const terms=await documents('Choose captured documents containing the actual applicable license terms',captured.filter(item=>item.text?.trim()),check,true);if(!terms)return;
  const permitted=await vscode.window.showQuickPick(uses,{title:'Select only uses permitted by the reviewed terms',canPickMany:true,ignoreFocusOut:true});check();if(!permitted)return;
  if(!permitted.length)throw new Error('No permitted uses were selected. No policy was recorded.');
  const intended=await input('Review basis and scope','Explain the authority and applicable terms for this decision. A local folder does not prove ownership.',check,4000);if(intended===undefined)return;
  const purpose=await input('Intended purpose','Specific purpose covered by this review; used verbatim for future admission',check,2000);if(purpose===undefined)return;
  const commercial=await choose('Will this use be commercial?', [{label:'Noncommercial use',value:'no'},{label:'Commercial use',value:'yes'}],check);if(commercial===undefined)return;
  const deployment=await input('Deployment context','Where and how the assets or generated outputs will be used',check,200);if(deployment===undefined)return;
  const conditions=await vscode.window.showInputBox({title:'Usage conditions',prompt:'One condition per line. Leave empty only when none apply; advanced review supports full JSON.',ignoreFocusOut:true});check();if(conditions===undefined)return;
  const required=await documents('Choose exact license and notice files that must be retained',captured,check,false);if(!required)return;
  const destinations=await vscode.window.showInputBox({title:'Permitted destination licenses',prompt:'Comma-separated identifiers explicitly allowed by the terms. Leave empty to permit no destination license.',ignoreFocusOut:true});check();if(destinations===undefined)return;
  let policy:RecordValue={license_document_paths:terms.map(item=>item.path),permitted_uses:permitted.map(item=>item.value),intended_context:intended,
    reviewed_usage_context:{purpose,commercial:commercial==='yes',deployment,conditions:conditions.split(/\r?\n/).map(item=>item.trim()).filter(Boolean)},
    required_files:required.map(item=>({path:item.path,sha256:item.sha256})),required_attributions:[],allowed_destination_licenses:[...new Set(destinations.split(',').map(item=>item.trim()).filter(Boolean))],gated_terms_acknowledged:false};
  const obligations=await choose('Additional obligations',[
    {label:'No additional attribution or custom fields',description:'The selected notices and entered conditions capture all applicable obligations',value:'complete'},
    {label:'Edit exact policy and attribution JSON',description:'Preserve custom conditions, attribution text and required paths without simplifying them',value:'advanced'},
    {label:'Keep unresolved',description:'Stop without recording a policy',value:'unresolved'}
  ],check);if(!obligations||obligations==='unresolved')return;
  if(obligations==='advanced'){
    const editor=await vscode.workspace.openTextDocument({language:'json',content:JSON.stringify(policy,null,2)});check();await vscode.window.showTextDocument(editor,{preview:false});check();
    const edited=await vscode.window.showInformationMessage('Edit this policy, including every required attribution and condition. Continue only when the complete policy is ready for exact review.','Review edited policy');check();if(edited!=='Review edited policy')return;
    try{policy=JSON.parse(editor.getText());}catch{throw new Error('The advanced policy must be valid JSON. No policy was recorded.');}
    if(!policy||typeof policy!=='object'||Array.isArray(policy))throw new Error('The advanced policy must be a JSON object.');
  }
  const payload={...policy,evidence_id:evidence.evidence_id,evidence_digest:evidence.evidence_digest,confirm_review:true};
  await view('Exact proposed owner-reviewed policy; verify permissions, context and obligations',payload,check);
  const answer=await vscode.window.showWarningMessage('Record this exact rights decision after reviewing the captured evidence and all obligations? The decision is limited to the selected source hashes and usage context. Unknown or unresolved terms must remain unresolved.',{modal:true},'Record reviewed policy','Keep unresolved');check();
  if(answer!=='Record reviewed policy')return;
  return invoke('hub.license.review',payload,true);
}

export async function reviewLocalAssetRights(invoke:LocalRightsInvoke,capabilities:RecordValue,check:()=>void):Promise<RecordValue|undefined>{
  const aliases:string[]=capabilities.publication_sources??[];
  if(!aliases.length){await vscode.window.showInformationMessage('No local folders are registered for this project. Use Protomire: Configure Hub to register a folder, then start this review again. Folder registration grants no asset rights.');check();return;}
  const repoType=await choose('Local artifact type',[{label:'Source origin for scratch outputs',description:'One pinned source review for future lineage; no pretrained model required',value:'source'},{label:'Model files',value:'model'},{label:'Dataset files',value:'dataset'}],check);if(!repoType)return;
  const source=await choose('Approved local source',aliases.map(alias=>({label:alias,value:alias})),check);if(!source)return;
  let sourceSnapshot:RecordValue|undefined;
  if(repoType==='source'){
    const project=await invoke('project.get');
    const recipe=await invoke('recipe.get',{recipe_id:project.recipe_id});
    const status=await invoke('project.source.status');
    if(!status.available||!status.binding||!recipe.source?.files?.length)throw new Error('Bind the project source and validate its pinned source inventory before reviewing source-origin rights. Imported snapshots use the extracted recipe inventory. No model weights are needed.');
    if(status.source_kind!==undefined&&!['git','imported_snapshot'].includes(status.source_kind))throw new Error('Unsupported bound source kind. No source rights evidence was captured.');
    if(status.source_kind==='imported_snapshot'&&(status.repository_id!==status.binding.repository_id||status.pinned_source_matches!==true||status.declared_source_commit!==recipe.source.commit||!/^(?:sha256:)?[0-9a-f]{64}$/.test(status.snapshot_digest??'')||!/^(?:sha256:)?[0-9a-f]{64}$/.test(status.transport_recipe_digest??'')))throw new Error('The imported snapshot does not match the current bound source and recipe inventory. Reload and validate the snapshot before reviewing rights.');
    sourceSnapshot={project,recipe,status,repository_id:status.binding.repository_id};
  }
  const listing=await invoke('hub.local.files',{source_alias:source});
  if(!listing.files?.length){await vscode.window.showInformationMessage('The selected folder has no eligible regular files. No evidence or policy was created.');check();return;}
  let selected:RecordValue[]|undefined;
  if(sourceSnapshot){
    selected=sourceSnapshot.recipe.source.files.map((item:RecordValue)=>{
      const file=listing.files.find((candidate:RecordValue)=>candidate.path===item.path&&candidate.sha256===item.sha256);
      if(!file)throw new Error(sourceSnapshot!.status.source_kind==='imported_snapshot'?'The selected folder does not contain the exact imported source inventory. Choose the bound snapshot folder; changed imported bytes require a new snapshot and review.':'The selected folder does not contain the exact pinned source closure. Choose the bound source folder and save, commit and pin changes before review.');
      return file;
    });
  }else selected=await documents('Choose exact local model or dataset files',listing.files,check,true);
  if(!selected)return;
  const evidenceDocs=await documents('Choose actual license, terms and notice documents',listing.files,check,true);if(!evidenceDocs)return;
  const files=[...new Map([...selected,...evidenceDocs].map(item=>[item.path,item])).values()];
  const label=await input('Declared local license label','Uninterpreted identifier. Use unknown when no label is established; actual terms and usage need review.',check,200,'unknown');if(label===undefined)return;
  const evidence=await invoke('hub.license.local.inspect',{source_alias:source,repo_type:repoType,files:files.map(item=>item.path),evidence_document_paths:evidenceDocs.map(item=>item.path),max_bytes:files.reduce((sum,item)=>sum+item.size_bytes,0),declared_license:label,...(sourceSnapshot?{source_repository_id:sourceSnapshot.repository_id,source_commit:sourceSnapshot.recipe.source.commit,recipe_id:sourceSnapshot.recipe.recipe_id,expected_revision:sourceSnapshot.recipe.revision,expected_recipe_digest:sourceSnapshot.recipe.digest,expected_project_revision:sourceSnapshot.project.revision}:{})},true);
  if(sourceSnapshot?.status.source_kind==='imported_snapshot'){
    const origin=evidence.evidence?.source_origin,target=origin?.review_target;
    if(origin?.source_kind!=='imported_snapshot'||origin.git_provenance_verified!==false||origin.repository_id!==sourceSnapshot.repository_id||origin.commit!==sourceSnapshot.recipe.source.commit||origin.snapshot_digest!==sourceSnapshot.status.snapshot_digest||origin.transport_recipe_digest!==sourceSnapshot.status.transport_recipe_digest||target?.project_id!==sourceSnapshot.project.project_id||target.project_revision!==sourceSnapshot.project.revision||target.recipe_id!==sourceSnapshot.recipe.recipe_id||target.recipe_revision!==sourceSnapshot.recipe.revision||target.recipe_digest!==sourceSnapshot.recipe.digest)throw new Error('Captured imported snapshot evidence does not match the exact source, project and recipe reviewed. No rights decision was recorded.');
  }
  const policy=await guidedPolicy(invoke,evidence,check);if(!policy)return;
  await view('Recorded source-bound policy',policy,check);
  if(sourceSnapshot){
    const priorOrigins:RecordValue[]=sourceSnapshot.recipe.extensions?.artifact_rights?.sources??[];
    const sourceIds=[...new Set<string>(priorOrigins.filter(item=>item.repo_type==='source').map(item=>item.license_policy_id))];
    const action=await choose('Use reviewed source origin',[
      {label:'Keep reviewed policy only',value:'keep'},
      {label:'Bind origin to future scratch outputs',description:'Preserve dataset and checkpoint parents; existing outputs are unchanged',value:'bind'},
      ...(sourceIds.length?[{label:'Replace selected direct source origins',description:'Explicit old/new review for changed code; preserve input parents, unresolved rights and history',value:'replace'}]:[])
    ],check);if(!action||action==='keep')return policy;
    const actionUses=uses.filter(item=>['train','evaluate','generate','export'].includes(item.value)&&policy.permitted_uses?.includes(item.value));
    if(!actionUses.length)throw new Error('This source policy permits no supported execution use. The review is saved but was not bound.');
    const intendedUse=await choose('Intended source use',actionUses,check);if(!intendedUse)return policy;
    let replacement:RecordValue={};
    if(action==='replace'){
      const policies:RecordValue[]=[];
      for(const policyId of sourceIds)policies.push(await invoke('hub.license.policy.get',{policy_id:policyId}));
      const selected=await vscode.window.showQuickPick<vscode.QuickPickItem&{policy:RecordValue}>(policies.map(item=>({label:item.policy_id,description:`Source commit ${item.source_origin?.commit??'unavailable'} · policy revision ${item.revision}`,policy:item})),{title:'Select only direct source policies to replace',canPickMany:true,ignoreFocusOut:true});check();if(!selected?.length)return policy;
      replacement={replace_source_policy_ids:selected.map(item=>item.policy.policy_id),expected_recipe_digest:sourceSnapshot.recipe.digest,confirm_replace:true};
      await view('Exact old/new source-origin replacement',{
        recipe_id:sourceSnapshot.recipe.recipe_id,recipe_revision:sourceSnapshot.recipe.revision,recipe_digest:sourceSnapshot.recipe.digest,
        old_direct_policies:selected.map(item=>item.policy),new_source_policy:policy,new_source_evidence:evidence,
        preserved_unresolved:sourceSnapshot.recipe.extensions?.artifact_rights?.unresolved??[],
        preserved_input_resources:[...(sourceSnapshot.recipe.resources??[]),...(sourceSnapshot.recipe.external_dependencies??[])],
        consequence:'Replace only the selected direct source policy entries for future outputs. Immutable recipe history and every actual input parent are retained.'
      },check);
    }
    const label=action==='replace'?'Replace reviewed direct origins':'Bind reviewed source origin';
    const answer=await vscode.window.showWarningMessage(action==='replace'?'Replace only the selected old direct source policies with this exact new source review? Check old/new policy IDs, source closures and the recipe revision/digest. Existing input parents, unresolved obligations and immutable history are preserved.':'Bind this exact reviewed source origin to future outputs of the pinned recipe? Existing parent rights and unresolved inputs are preserved. Changed source bytes or usage context require another review.',{modal:true},label);check();if(answer!==label)return policy;
    const result=await invoke(action==='replace'?'hub.source.policy.replace':'hub.source.policy.bind',{recipe_id:sourceSnapshot.recipe.recipe_id,expected_revision:sourceSnapshot.recipe.revision,expected_project_revision:sourceSnapshot.project.revision,license_policy_id:policy.policy_id,intended_use:intendedUse,usage_context:policy.reviewed_usage_context,...replacement},true);
    await view('Reviewed source origin bound to future output lineage',result,check);return result;
  }
  if(!policy.permitted_uses?.includes('download')){
    await vscode.window.showInformationMessage('The reviewed policy was recorded without permission to copy files. For a registered dataset, use Link reviewed policies to a registered dataset. No asset copy was created.');check();return policy;
  }
  const action=await choose('Use the reviewed local policy',[
    {label:'Keep reviewed policy only',description:'A registered dataset can link this policy to its existing source bytes',value:'keep'},
    {label:'Register a verified local copy',description:'Recheck exact hashes and copy selected files into the asset store',value:'register'}
  ],check);if(action!=='register')return policy;
  const asset=await invoke('hub.local.register',{evidence_id:evidence.evidence_id,license_policy_id:policy.policy_id},true);
  await view('Verified local asset and reviewed policy reference',asset,check);
  await vscode.window.showInformationMessage('Verified local files are available under Use verified local assets. Binding those files never selects a pretrained initializer.');check();return asset;
}
