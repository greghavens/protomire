/* Native dialog-contract tests over the shared operation surface, not GUI acceptance. */
const {test}=require('node:test'),assert=require('node:assert/strict'),Module=require('node:module');
const vscode={window:{},workspace:{}};
const original=Module._load;Module._load=function(name){if(name==='vscode')return vscode;return original.apply(this,arguments);};
const {openHubWorkflow}=require('../out/hubWorkflow');Module._load=original;
const hash='a'.repeat(64),termsHash='b'.repeat(64),commit='c'.repeat(40);
const files=[{path:'model.py',sha256:hash,size_bytes:12},{path:'odd-terms.txt',sha256:termsHash,size_bytes:80}];
const usage={purpose:'Synthetic fixture exercise',commercial:false,deployment:'Isolated test',conditions:['Keep notices']};
function setup(options={}){
 const calls=[],native=[],views=[],messages=[],picks=[],events=[];let current=true;
 const check=()=>{if(!current)throw new Error('Project changed during local review');};
 const inputs={'Declared local license label':'unknown','Review basis and scope':'Synthetic owner-authored fixture terms only','Intended purpose':usage.purpose,'Deployment context':usage.deployment,'Usage conditions':usage.conditions.join('\n'),'Permitted destination licenses':''};
 const choices={'Model and dataset Hub':'localrights','Local artifact type':options.type??'model','Approved local source':'fixture','Are all applicable rights and obligations resolved?':'review','Will this use be commercial?':'no','Additional obligations':options.advanced?'advanced':'complete','Use the reviewed local policy':'register','Use reviewed source origin':'bind','Intended source use':'train',...options.choices};
 vscode.window.showQuickPick=async(items,settings)=>{
  events.push({pick:settings.title});
  picks.push({items,settings});
  if(options.changeAt===settings.title)current=false;
  if(options.cancelAt===settings.title)return undefined;
  if(options.emptyAt===settings.title)return [];
  if(settings.canPickMany){
   if(settings.title==='Select only uses permitted by the reviewed terms')return items.filter(item=>(options.uses??['download','train']).includes(item.value));
   if(settings.title==='Choose exact local model or dataset files')return items.filter(item=>item.item.path==='model.py');
   if(settings.title==='Select only direct source policies to replace')return items;
   return items.filter(item=>item.item.path==='odd-terms.txt');
  }
  return items.find(item=>item.value===choices[settings.title])??items[0];
 };
 vscode.window.showInputBox=async settings=>{if(options.changeAt===settings.title)current=false;if(options.cancelAt===settings.title)return undefined;return inputs[settings.title];};
 vscode.window.showWarningMessage=async(text,settings,...actions)=>{messages.push(text);if(options.changeAt==='final')current=false;return options.rejectFinal?undefined:actions[0];};
 vscode.window.showInformationMessage=async(text,...actions)=>{messages.push(text);return actions[0];};
 vscode.workspace.openTextDocument=async value=>{const data=JSON.parse(value.content);views.push(data);events.push({view:data.review});return {getText:()=>options.invalidAdvanced?'invalid JSON':JSON.stringify({...data,required_attributions:[{path:'NOTICE.txt',text:'Exact synthetic attribution'}]})};};
 vscode.window.showTextDocument=async()=>{};
 const recipe={recipe_id:'recipe',revision:2,digest:hash,source:{commit,files:[{path:'model.py',sha256:hash}]},resources:[],external_dependencies:[]};
 if(options.priorOrigin)recipe.extensions={artifact_rights:{sources:[{repo_type:'source',license_policy_id:'old-policy'}],unresolved:[{reason:'unresolved-input'}]}};
 const project={project_id:'project',recipe_id:'recipe',revision:3};
 const status={available:!options.noSource,binding:{alias:'fixture',repository_id:'sha256:'+hash},...(options.snapshot?{source_kind:'imported_snapshot',repository_id:'sha256:'+hash,declared_source_commit:commit,snapshot_digest:'d'.repeat(64),transport_recipe_digest:'e'.repeat(64),pinned_source_matches:true}:{}),...options.status};
 const origin={repository_id:'sha256:'+hash,commit,files:recipe.source.files,source_kind:'imported_snapshot',snapshot_digest:'d'.repeat(64),transport_recipe_digest:'e'.repeat(64),git_provenance_verified:false,review_target:{project_id:'project',project_revision:3,recipe_id:'recipe',recipe_revision:2,recipe_digest:hash,scope:{project_id:'project',recipe_id:'recipe',content_digest:'f'.repeat(64)}},...options.origin};
 let policy;
 const client={async invoke(op,p){calls.push({op,p});
  if(op==='hub.providers')return {providers:[],accounts:[],publication_sources:options.noAliases?[]:['fixture']};
  if(op==='hub.local.files')return {files:options.noFiles?[]:options.changedSourceBytes?files.map(file=>({...file,sha256:termsHash})):files};
  if(op==='project.get')return project;
  if(op==='recipe.get')return recipe;
  if(op==='project.source.status')return status;
  if(op==='hub.license.local.inspect')return {evidence_id:'evidence',evidence_digest:hash,evidence:{repo_type:p.repo_type,documents:options.missingTerms?[]:[{...files[1],text:'Synthetic owner-authored terms only.'}],source_file_manifest:files,permissions_inferred_from_label:false,...(options.snapshot?{source_origin:origin}:{})}};
  if(op==='hub.license.policy.get')return {policy_id:p.policy_id,revision:1,source_origin:{commit:'d'.repeat(40),files:[{path:'model.py',sha256:'e'.repeat(64)}]}};
  if(op==='hub.license.review'){policy={...p,policy_id:'policy',status:'reviewed'};return policy;}
  if(op==='hub.local.register')return {asset_id:'asset',usage_context:usage};
  if(op==='hub.source.policy.bind')return {future_outputs_only:true,assets_registered:false,recipe};
  throw new Error('Unexpected operation '+op);
 }};
 const review=async(op,p,guard)=>{native.push({op,p});if(options.nativeProjectSwitch)current=false;guard();if(options.denyNative)return undefined;if(op==='hub.source.policy.replace')return {future_outputs_only:true,assets_registered:false,replaced_direct_source_policy_ids:p.replace_source_policy_ids};return {...p,policy_id:'policy',status:'reviewed'};};
 return {calls,native,views,messages,picks,events,client,check,review};
}
test('guided local review captures separately selected actual terms and exact byte budget without a Hub provider',async()=>{
 const h=setup();const result=await openHubWorkflow(h.client,'project',h.check,h.review);
 assert.equal(result.asset_id,'asset');
 const capture=h.calls.find(call=>call.op==='hub.license.local.inspect').p;
 assert.deepEqual(capture.files,['model.py','odd-terms.txt']);assert.deepEqual(capture.evidence_document_paths,['odd-terms.txt']);assert.equal(capture.max_bytes,92);
 const policy=h.native[0].p;assert.deepEqual(policy.permitted_uses,['download','train']);assert.deepEqual(policy.reviewed_usage_context,usage);assert.deepEqual(policy.required_files,[{path:'odd-terms.txt',sha256:termsHash}]);assert.deepEqual(policy.allowed_destination_licenses,[]);
 assert.equal(h.picks.find(item=>item.settings.title==='Select only uses permitted by the reviewed terms').items.some(item=>item.picked),false);
 assert(!h.calls.some(call=>['hub.license.review','hub.search','hub.download.start','hub.publish.start'].includes(call.op)));
});
test('one source-origin review binds the exact pinned revision without copying a fake model asset',async()=>{
 const h=setup({type:'source',uses:['train','export']});const result=await openHubWorkflow(h.client,'project',h.check,h.review);assert(result.future_outputs_only);
 const capture=h.calls.find(call=>call.op==='hub.license.local.inspect').p;assert.equal(capture.repo_type,'source');assert.equal(capture.source_commit,commit);assert.equal(capture.source_repository_id,'sha256:'+hash);
 assert.equal(capture.recipe_id,'recipe');assert.equal(capture.expected_revision,2);assert.equal(capture.expected_recipe_digest,hash);assert.equal(capture.expected_project_revision,3);
 const binding=h.calls.find(call=>call.op==='hub.source.policy.bind').p;assert.equal(binding.expected_revision,2);assert.equal(binding.expected_project_revision,3);assert.equal(binding.intended_use,'train');assert.deepEqual(binding.usage_context,usage);
 assert(!h.calls.some(call=>call.op==='hub.local.register'||call.op==='hub.asset.bind'));assert(!h.picks.some(item=>item.settings.title==='Choose exact local model or dataset files'));
});
test('imported snapshot follows normal guided rights review with exact capture guards and visible unauthenticated byte evidence',async()=>{
 const h=setup({type:'source',snapshot:true,uses:['train','export']});const result=await openHubWorkflow(h.client,'project',h.check,h.review);assert(result.future_outputs_only);
 const capture=h.calls.find(call=>call.op==='hub.license.local.inspect').p;
 assert.deepEqual({...capture,idempotency_key:undefined},{project_id:'project',source_alias:'fixture',repo_type:'source',files:['model.py','odd-terms.txt'],evidence_document_paths:['odd-terms.txt'],max_bytes:92,declared_license:'unknown',source_repository_id:'sha256:'+hash,source_commit:commit,recipe_id:'recipe',expected_revision:2,expected_recipe_digest:hash,expected_project_revision:3,idempotency_key:undefined});
 const captured=h.views.find(item=>item.evidence_id==='evidence'),visible=captured.source_identity.join('\n');
 assert.match(visible,/Source kind: imported_snapshot/);assert.match(visible,/not authenticated Git/);assert.match(visible,/Imported-byte evidence/);assert.match(visible,/extracted recipe.json/);assert(visible.includes('d'.repeat(64)));assert(visible.includes('e'.repeat(64)));assert.match(captured.rights_review_notice,/explicit owner-approved/);assert.equal(captured.evidence.source_origin.git_provenance_verified,false);assert.deepEqual(captured.evidence.source_file_manifest,files);
 assert(h.events.findIndex(item=>item.view==='Captured source bytes and license evidence: read all applicable terms')<h.events.findIndex(item=>item.pick==='Are all applicable rights and obligations resolved?'));
 assert.deepEqual(h.native.map(item=>item.op),['hub.license.review']);assert.equal(h.calls.find(item=>item.op==='hub.source.policy.bind').p.expected_revision,2);
 assert(!h.calls.some(item=>['project.source.bind','recipe.source.pin','hub.local.register','hub.download.start','hub.publish.start','hub.search'].includes(item.op)));assert(!h.picks.some(item=>['Hub provider','Choose committed recipe source'].includes(item.settings.title)));
});
for(const status of [{pinned_source_matches:false},{declared_source_commit:'f'.repeat(40)},{snapshot_digest:undefined},{transport_recipe_digest:undefined},{repository_id:'sha256:'+'f'.repeat(64)},{source_kind:'arbitrary_folder'}])test('unvalidated or changed snapshot status blocks capture '+JSON.stringify(status),async()=>{
 const h=setup({type:'source',snapshot:true,status});await assert.rejects(openHubWorkflow(h.client,'project',h.check,h.review),/snapshot|Unsupported/);assert.equal(h.native.length,0);assert(!h.calls.some(item=>item.op==='hub.license.local.inspect'));
});
for(const origin of [{source_kind:'git'},{git_provenance_verified:true},{snapshot_digest:'f'.repeat(64)},{transport_recipe_digest:'f'.repeat(64)},{commit:'f'.repeat(40)},{repository_id:'sha256:'+'f'.repeat(64)},{review_target:{project_id:'different'}}])test('mismatched captured snapshot evidence cannot open rights approval '+JSON.stringify(origin),async()=>{
 const h=setup({type:'source',snapshot:true,origin});await assert.rejects(openHubWorkflow(h.client,'project',h.check,h.review),/Captured imported snapshot evidence/);assert.equal(h.native.length,0);assert(!h.calls.some(item=>item.op==='hub.source.policy.bind'));
});
test('changed imported source bytes direct the owner to a new snapshot without Git instructions',async()=>{
 const h=setup({type:'source',snapshot:true,changedSourceBytes:true});await assert.rejects(openHubWorkflow(h.client,'project',h.check,h.review),error=>/changed imported bytes require a new snapshot/.test(error.message)&&!/commit and pin/.test(error.message));assert.equal(h.native.length,0);assert(!h.calls.some(item=>item.op==='hub.license.local.inspect'));
});
for(const options of [{missingTerms:true},{choices:{'Are all applicable rights and obligations resolved?':'unresolved'}},{rejectFinal:true},{cancelAt:'Choose actual license, terms and notice documents'},{denyNative:true}])test('snapshot review cancellation or unresolved terms cannot bind rights '+JSON.stringify(options),async()=>{
 const h=setup({type:'source',snapshot:true,...options});if(options.denyNative)await assert.rejects(openHubWorkflow(h.client,'project',h.check,h.review),/not approved/);else assert.equal(await openHubWorkflow(h.client,'project',h.check,h.review),undefined);assert(!h.calls.some(item=>['hub.source.policy.bind','hub.local.register'].includes(item.op)));
});
test('an explicitly selected training-only policy can be recorded for existing dataset bytes without copy permission',async()=>{
 const h=setup({type:'dataset',uses:['train']});const result=await openHubWorkflow(h.client,'project',h.check,h.review);assert.equal(result.policy_id,'policy');assert(!h.calls.some(call=>call.op==='hub.local.register'));assert(h.messages.some(text=>text.includes('Link reviewed policies')));
});
test('advanced exact attribution and custom fields survive guided review without interpretation',async()=>{
 const h=setup({advanced:true});await openHubWorkflow(h.client,'project',h.check,h.review);assert.deepEqual(h.native[0].p.required_attributions,[{path:'NOTICE.txt',text:'Exact synthetic attribution'}]);
});
for(const options of [{noAliases:true},{noFiles:true},{missingTerms:true},{choices:{'Are all applicable rights and obligations resolved?':'unresolved'}},{choices:{'Additional obligations':'unresolved'}},{rejectFinal:true},{cancelAt:'Choose actual license, terms and notice documents'},{cancelAt:'Intended purpose'}])test('cancelled, missing or unresolved local review grants no permission '+JSON.stringify(options),async()=>{
 const h=setup(options);assert.equal(await openHubWorkflow(h.client,'project',h.check,h.review),undefined);assert.equal(h.native.length,0);assert(!h.calls.some(call=>['hub.local.register','hub.source.policy.bind','hub.license.review'].includes(call.op)));
});
for(const stage of ['Approved local source','Choose actual license, terms and notice documents','Intended purpose','final'])test('project navigation at '+stage+' cannot dispatch a rights decision',async()=>{
 const h=setup({changeAt:stage});await assert.rejects(openHubWorkflow(h.client,'project',h.check,h.review),/Project changed/);assert.equal(h.native.length,0);assert(!h.calls.some(call=>call.op==='hub.local.register'));
});
test('native rights cancellation stops asset registration and releases the workflow fence',async()=>{
 const h=setup({denyNative:true});await assert.rejects(openHubWorkflow(h.client,'project',h.check,h.review),/not approved/);assert(!h.calls.some(call=>call.op==='hub.local.register'));const again=setup({noAliases:true});await openHubWorkflow(again.client,'project',again.check,again.review);
});
test('live navigation guard is passed to native approval immediately before dispatch',async()=>{
 const h=setup({nativeProjectSwitch:true});await assert.rejects(openHubWorkflow(h.client,'project',h.check,h.review),/Project changed/);assert(!h.calls.some(call=>call.op==='hub.local.register'));
});
test('empty permissions and invalid advanced JSON cannot silently mark a policy reviewed',async()=>{
 for(const options of [{uses:[]},{advanced:true,invalidAdvanced:true},{emptyAt:'Choose captured documents containing the actual applicable license terms'}]){const h=setup(options);await assert.rejects(openHubWorkflow(h.client,'project',h.check,h.review));assert.equal(h.native.length,0);}
});

test('native source replacement previews exact old/new identity and uses guarded publisher review',async()=>{
 const h=setup({type:'source',uses:['train'],priorOrigin:true,choices:{'Use reviewed source origin':'replace'}});
 const result=await openHubWorkflow(h.client,'project',h.check,h.review);assert.deepEqual(result.replaced_direct_source_policy_ids,['old-policy']);
 assert.deepEqual(h.native.map(item=>item.op),['hub.license.review','hub.source.policy.replace']);const p=h.native[1].p;assert.equal(p.expected_recipe_digest,hash);assert.equal(p.expected_revision,2);assert.deepEqual(p.replace_source_policy_ids,['old-policy']);assert.equal(p.license_policy_id,'policy');assert.equal(p.confirm_replace,true);
 const preview=h.views.find(item=>item.old_direct_policies);assert.equal(preview.old_direct_policies[0].policy_id,'old-policy');assert.equal(preview.new_source_policy.policy_id,'policy');assert.deepEqual(preview.preserved_unresolved,[{reason:'unresolved-input'}]);assert(!h.calls.some(item=>item.op==='hub.source.policy.replace'));
});
