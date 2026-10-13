import * as vscode from 'vscode';
import {RecordValue} from './types';

export class HubRepositoryItem extends vscode.TreeItem {
  constructor(label:string,readonly kind:'repository'|'group'|'file',readonly key:string,readonly data:RecordValue={}){
    super(label,kind==='file'?vscode.TreeItemCollapsibleState.None:vscode.TreeItemCollapsibleState.Expanded);
    this.contextValue=`hub.${kind}`;
  }
}
/** A companion repository view, not a replacement Git implementation.
 * Staging is UI-local; only exact server previews can authorize publication.
 */
export class HubRepositoryTree implements vscode.TreeDataProvider<HubRepositoryItem>,vscode.Disposable {
  private comparison:RecordValue|undefined;
  constructor(protected readonly commandProject?:string){}
  repositoryIdentity():RecordValue|undefined{return this.comparison?{project_id:this.commandProject,commit:this.comparison.commit}:undefined;}
  private readonly staged=new Set<string>();
  private readonly changed=new vscode.EventEmitter<HubRepositoryItem|undefined>();
  readonly onDidChangeTreeData=this.changed.event;
  getTreeItem(item:HubRepositoryItem):vscode.TreeItem{return item;}
  setComparison(value:RecordValue):void{
    if(!value.repo_id||!['model','dataset'].includes(value.repo_type)||typeof value.commit!=='string'||!value.commit||value.commit.length>512||!Array.isArray(value.changes))throw new Error('Repository comparison has no immutable identity.');
    this.comparison=structuredClone(value);this.staged.clear();this.changed.fire(undefined);
  }
  clear():void{this.comparison=undefined;this.staged.clear();this.changed.fire(undefined);}
  stage(path:string):void{
    const row=this.comparison?.changes.find((item:RecordValue)=>item.path===path);
    if(!row?.local||row.state==='remote_only')throw new Error('Only exact local files can be staged for upload. Remote deletion is unsupported.');
    this.staged.add(path);this.changed.fire(undefined);
  }
  unstage(path:string):void{this.staged.delete(path);this.changed.fire(undefined);}
  stagedSelection():RecordValue{
    if(!this.comparison||!this.staged.size)throw new Error('Stage at least one local file before previewing publication.');
    return {provider:this.comparison.provider,repo_id:this.comparison.repo_id,repo_type:this.comparison.repo_type,source_alias:this.comparison.source_alias,inspected_commit:this.comparison.commit,files:[...this.staged].sort()};
  }
  getChildren(item?:HubRepositoryItem):HubRepositoryItem[]{
    const value=this.comparison;if(!value)return [];
    if(!item){const repo=new HubRepositoryItem(value.repo_id,'repository','repository');repo.description=`${value.repo_type} · ${value.commit.slice(0,12)}`;repo.tooltip=`Immutable revision ${value.commit}. Files are separate from source Git. Nothing uploads automatically.`;return [repo];}
    if(item.kind==='repository')return [new HubRepositoryItem(`Staged uploads (${this.staged.size})`,'group','staged'),new HubRepositoryItem('Local changes','group','changes'),new HubRepositoryItem('Remote files','group','remote')];
    if(item.kind!=='group')return [];
    const rows:RecordValue[]=value.changes.filter((row:RecordValue)=>item.key==='staged'?this.staged.has(row.path):item.key==='changes'?row.local&&!this.staged.has(row.path):row.remote);
    return rows.map(row=>{const file=new HubRepositoryItem(row.path,'file',row.path,row);file.description=row.state;file.tooltip=`${row.path}\nLocal SHA256: ${row.local?.sha256??'none'}\nRemote SHA256/Git blob: ${row.remote?.sha256??row.remote?.git_blob_sha1??'none'}\nRemote commit: ${value.commit}`;file.contextValue=this.staged.has(row.path)?'hub.staged':row.local?'hub.local':'hub.remote';if(row.local)file.command={command:this.staged.has(row.path)?'modelbuilder.hubUnstage':'modelbuilder.hubStage',title:this.staged.has(row.path)?'Unstage upload':'Stage upload',arguments:[row.path,this.commandProject,value.commit]};return file;});
  }
  dispose():void{this.changed.dispose();}
}

/** Native VS Code SCM companion for large model/dataset files. No Git hooks,
 * save listeners, background publication or guessed local filesystem mapping.
 */
export class HubSourceControl extends HubRepositoryTree {
  readonly sourceControl:vscode.SourceControl;
  private readonly working:vscode.SourceControlResourceGroup;
  private readonly stagedGroup:vscode.SourceControlResourceGroup;
  private current:RecordValue|undefined;
  private readonly paths=new Set<string>();
  constructor(private readonly projectId:string){
    super(projectId);
    this.sourceControl=vscode.scm.createSourceControl('modelbuilder.hub','Hugging Face artifacts',vscode.Uri.from({scheme:'modelbuilder-hub',authority:projectId,path:'/'}));
    this.sourceControl.inputBox.visible=false;
    this.sourceControl.acceptInputCommand={command:'modelbuilder.hubPublishStaged',title:'Preview and publish staged Hub files',arguments:[]};
    this.working=this.sourceControl.createResourceGroup('hubChanges','Hub changes');
    this.stagedGroup=this.sourceControl.createResourceGroup('hubStaged','Staged Hub uploads');
    this.working.hideWhenEmpty=true;this.stagedGroup.hideWhenEmpty=true;
  }
  override setComparison(value:RecordValue):void{super.setComparison(value);this.current=structuredClone(value);this.paths.clear();this.render();}
  override stage(path:string):void{super.stage(path);this.paths.add(path);this.render();}
  override unstage(path:string):void{super.unstage(path);this.paths.delete(path);this.render();}
  override clear():void{super.clear();this.current=undefined;this.paths.clear();this.render();}
  private render():void{
    const rows:RecordValue[]=this.current?.changes??[];
    const resource=(row:RecordValue,staged:boolean):vscode.SourceControlResourceState=>({resourceUri:vscode.Uri.from({scheme:'modelbuilder-hub',authority:this.projectId,path:`/${row.path}`}),contextValue:staged?'hubStaged':'hubChange',decorations:{tooltip:`${row.state} · ${this.current?.repo_id} @ ${this.current?.commit}\nLocal SHA256 ${row.local.sha256}\nStaging does not upload. Publication requires exact approval.`},command:{command:staged?'modelbuilder.hubUnstage':'modelbuilder.hubStage',title:staged?'Unstage Hub file':'Stage Hub file',arguments:[row.path,this.projectId,this.current?.commit]}});
    this.working.resourceStates=rows.filter(row=>row.local&&!this.paths.has(row.path)&&['added','modified'].includes(row.state)).map(row=>resource(row,false));
    this.stagedGroup.resourceStates=rows.filter(row=>this.paths.has(row.path)).map(row=>resource(row,true));
    this.sourceControl.count=this.working.resourceStates.length+this.stagedGroup.resourceStates.length;
    this.sourceControl.statusBarCommands=this.current?[{command:'modelbuilder.openHub',title:`$(cloud) ${this.current.repo_id} @ ${this.current.commit.slice(0,8)}`,tooltip:'Inspect Hub repository and immutable revision'},{command:'modelbuilder.hubPublishStaged',title:`$(cloud-upload) Preview ${this.paths.size} staged`,tooltip:'Review and approve exact files before publishing'}]:[];
  }
  override dispose():void{this.working.dispose();this.stagedGroup.dispose();this.sourceControl.dispose();super.dispose();}
}
