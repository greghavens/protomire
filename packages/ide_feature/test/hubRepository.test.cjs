const {test}=require('node:test'),assert=require('node:assert/strict'),Module=require('node:module');
const mock={scm:{},Uri:{},TreeItem:class{constructor(label,collapsibleState){this.label=label;this.collapsibleState=collapsibleState;}},TreeItemCollapsibleState:{None:0,Expanded:2},EventEmitter:class{constructor(){this.event=()=>{};}fire(){}dispose(){}}};
const load=Module._load;Module._load=function(name){if(name==='vscode')return mock;return load.apply(this,arguments);};const {HubRepositoryTree,HubSourceControl}=require('../out/hubRepository');Module._load=load;
const comparison={provider:'huggingface',repo_id:'owner/dataset',repo_type:'dataset',commit:'a'.repeat(40),source_alias:'data',changes:[{path:'README.md',state:'unchanged',local:{sha256:'a'},remote:{git_blob_sha1:'b'}},{path:'train.parquet',state:'modified',local:{sha256:'c'},remote:{sha256:'d'}},{path:'old.parquet',state:'remote_only',local:null,remote:{sha256:'e'}}]};
test('repository tree lists immutable revision and stages local files without uploads',()=>{const tree=new HubRepositoryTree();tree.setComparison(comparison);const root=tree.getChildren()[0];assert.match(root.description,/aaaaaaaaaaaa/);assert.equal(tree.getChildren(root).length,3);tree.stage('train.parquet');tree.stage('README.md');assert.deepEqual(tree.stagedSelection().files,['README.md','train.parquet']);const staged=tree.getChildren(tree.getChildren(root)[0]);assert.equal(staged.length,2);assert.equal(staged[0].command.command,'modelbuilder.hubUnstage');tree.unstage('README.md');assert.deepEqual(tree.stagedSelection().files,['train.parquet']);});
test('remote-only files cannot be staged as implicit deletion',()=>{const tree=new HubRepositoryTree();tree.setComparison(comparison);assert.throws(()=>tree.stage('old.parquet'),/deletion is unsupported/);});
test('switching repository comparison clears staged identities',()=>{const tree=new HubRepositoryTree();tree.setComparison(comparison);tree.stage('train.parquet');tree.setComparison({...comparison,commit:'b'.repeat(40)});assert.throws(()=>tree.stagedSelection(),/Stage at least/);tree.clear();assert.deepEqual(tree.getChildren(),[]);});
test('unresolved or malformed repository references are not accepted',()=>{const tree=new HubRepositoryTree();assert.throws(()=>tree.setComparison({...comparison,commit:''}),/immutable identity/);});

test('native SCM groups stage and unstage without an upload or source Git mutation',()=>{
 const groups=[];let created;
 mock.Uri={from:value=>value};mock.scm={createSourceControl:(id,label,rootUri)=>created={id,label,rootUri,inputBox:{},createResourceGroup:(id,label)=>{const group={id,label,resourceStates:[],dispose(){}};groups.push(group);return group;},dispose(){}}};
 const scm=new HubSourceControl('project');scm.setComparison(comparison);
 assert.equal(created.id,'modelbuilder.hub');assert.equal(created.rootUri.scheme,'modelbuilder-hub');assert.equal(created.inputBox.visible,false);
 assert.equal(groups[0].resourceStates.length,1);assert.equal(groups[0].resourceStates[0].resourceUri.path,'/train.parquet');
 scm.stage('train.parquet');assert.equal(groups[0].resourceStates.length,0);assert.equal(groups[1].resourceStates.length,1);assert.equal(created.acceptInputCommand.command,'modelbuilder.hubPublishStaged');
 scm.unstage('train.parquet');assert.equal(groups[1].resourceStates.length,0);scm.clear();assert.equal(created.count,0);scm.dispose();
});
