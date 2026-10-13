import * as vscode from 'vscode';
import { randomBytes, randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { OperationClient, OperationError } from './client';
import { RecipeDocument, editableRecipeSource, currentProjectRecipe } from './document';
import { checkMessage, escapeHtml, serviceUrl, UI_OPERATIONS } from './security';
import { OperationSpec, Project, Recipe, RecordValue } from './types';
import { EvaluationNotifications } from './notifications';
import { EVALUATION_WINDOW } from './checkpointCurves';
import { discoverRegistryRecipe, publishRegistryRecipe } from './registryFiles';
import { chooseLocalSource, acknowledgeSource, sourceBindingPayload, matchLocalSource, reviewAndPinSource } from './sourceWorkspace';
import { saveNativeModel } from './modelExport';
import { importResearchPdf } from './researchDocuments';
import { configureOptionalAssistant } from './assistantSetup';
import { NewProjectView } from './newProject';
import { attachProjectProjection } from './projectProjection';
import { registerProjectSource } from './projectSourceSetup';
import { openHubWorkflow } from './hubWorkflow';
import { HubController } from './hubController';
import { ManagedCore, corePaths } from './managedCore';
import { NativeReviewController } from './nativeReview';
import { configureHub } from './hubSettings';

const TOKEN_KEY = 'modelbuilder.serviceToken';
const SECTIONS = ['Research', 'Code', 'Data', 'Recipe', 'Runs', 'Versions'] as const;
type Navigation = {projectId?:string; section?:string; recipeId?:string; runId?:string};
class ExplorerItem extends vscode.TreeItem {
  constructor(label:string, readonly kind:string, readonly project?:Project, readonly record?:RecordValue) {
    super(label, kind === 'project' || (kind === 'section' && ['Research','Runs','Versions'].includes(label)) ? vscode.TreeItemCollapsibleState.Collapsed : vscode.TreeItemCollapsibleState.None);
    this.contextValue = `modelbuilder.${kind}`;
    if (project) {
      this.command = {command:'modelbuilder.openWorkbench',title:'Open',arguments:[{projectId:project.project_id,section:kind === 'project' ? 'Recipe' : kind === 'section' ? label : kind,recipeId:record?.recipe_id,runId:record?.operation_id}]};
      this.tooltip = record ? JSON.stringify(record,null,2) : `${project.model_name}\nProject ${project.project_id}\nRevision ${project.revision}`;
    }
    this.iconPath = new vscode.ThemeIcon(({project:'project',Research:'book',Code:'code',Data:'database',Recipe:'type-hierarchy',Runs:'play-circle',Versions:'versions'} as Record<string,string>)[kind === 'section' ? label : kind] ?? 'circle-outline');
    if (record) this.description = String(record.status ?? (record.revision ? `r${record.revision}` : record.backend_run_id ?? ''));
  }
}
export class ModelExplorer implements vscode.TreeDataProvider<ExplorerItem> {
  private changed = new vscode.EventEmitter<ExplorerItem | undefined>();
  readonly onDidChangeTreeData = this.changed.event;
  private project?:Project;
  constructor(private client:OperationClient) {}
  setProject(project?:Project){this.project=project;this.refresh();}
  refresh() { this.changed.fire(undefined); }
  getTreeItem(item:ExplorerItem) { return item; }
  async getChildren(item?:ExplorerItem):Promise<ExplorerItem[]> {
    try {
      if (!item) {if(this.project){const root=new ExplorerItem(this.project.model_name,'project',this.project);root.collapsibleState=vscode.TreeItemCollapsibleState.Expanded;return [root];}const landing=new ExplorerItem('Open a model project','landing');landing.command={command:'modelbuilder.openWorkbench',title:'Choose or create a model project'};return [landing];}
      if (item.kind === 'project') return SECTIONS.map(section=>new ExplorerItem(section,'section',item.project));
      if (item.kind !== 'section' || !item.project) return [];
      const project = item.project;
      const payload = {project_id:project.project_id};
      const operations:Record<string,[string,string,string]> = {Research:['hypothesis.list','hypotheses','proposed_change'],Runs:['run.list','runs','operation_id'],Versions:['model.version.list','versions','name']};
      const config = operations[String(item.label)];
      if (!config) return [];
      const result = await this.client.invoke(config[0],payload);
      return (result[config[1]] ?? []).map((record:RecordValue)=>new ExplorerItem(String(record[config[2]] ?? record.model_version_id ?? record.id),String(item.label),project,record));
    } catch (error) { const leaf = new ExplorerItem(error instanceof Error ? error.message : String(error),'error'); leaf.command={command:'modelbuilder.configureConnection',title:'Configure connection'}; return [leaf]; }
  }
}

/** Native Monaco JSON source, saved via the exact same recipe.update operation. */
class RecipeFileSystem implements vscode.FileSystemProvider {
  private changed = new vscode.EventEmitter<vscode.FileChangeEvent[]>();
  readonly onDidChangeFile = this.changed.event;
  private documents = new Map<string,RecipeDocument>();
  constructor(private client:OperationClient, private didSave:()=>void) {}
  watch():vscode.Disposable { return new vscode.Disposable(()=>{}); }
  acceptRemote(recipe:Recipe){const uri=vscode.Uri.from({scheme:'modelrecipe',path:`/${recipe.project_id}/${recipe.recipe_id}.modelrecipe.json`});const doc=this.documents.get(uri.toString());const editor=vscode.workspace.textDocuments.find(item=>item.uri.toString()===uri.toString());if(doc&&!editor?.isDirty&&doc.current?.revision!==recipe.revision){doc.current=recipe;this.changed.fire([{type:vscode.FileChangeType.Changed,uri}]);}}
  async open(projectId:string,recipeId:string):Promise<vscode.Uri> {
    const uri=vscode.Uri.from({scheme:'modelrecipe',path:`/${projectId}/${recipeId}.modelrecipe.json`});
    const existing = vscode.workspace.textDocuments.find(doc=>doc.uri.toString()===uri.toString());
    if (!existing?.isDirty) { const doc=new RecipeDocument(this.client); await doc.load(projectId,recipeId); this.documents.set(uri.toString(),doc); this.changed.fire([{type:vscode.FileChangeType.Changed,uri}]); }
    return uri;
  }
  async stat(uri:vscode.Uri):Promise<vscode.FileStat> { const data=await this.readFile(uri); return {type:vscode.FileType.File,ctime:0,mtime:0,size:data.length}; }
  readDirectory():[string,vscode.FileType][] { return []; }
  createDirectory() { throw vscode.FileSystemError.NoPermissions('Create recipes in ModelBuilder.'); }
  delete() { throw vscode.FileSystemError.NoPermissions('Recipe deletion is not exposed by this editor.'); }
  rename() { throw vscode.FileSystemError.NoPermissions('Change the recipe name, not its identity.'); }
  private async document(uri:vscode.Uri) {
    let doc=this.documents.get(uri.toString());
    if (!doc) {
      const [projectId,filename]=uri.path.slice(1).split('/');
      if (!projectId || !filename?.endsWith('.modelrecipe.json')) throw vscode.FileSystemError.FileNotFound(uri);
      doc=new RecipeDocument(this.client); await doc.load(projectId,filename.replace(/\.modelrecipe\.json$/,'')); this.documents.set(uri.toString(),doc);
    }
    return doc;
  }
  async readFile(uri:vscode.Uri) { return Buffer.from(editableRecipeSource((await this.document(uri)).current!)); }
  async writeFile(uri:vscode.Uri,content:Uint8Array) {
    const doc=await this.document(uri);
    try { await doc.save(JSON.parse(Buffer.from(content).toString('utf8')),randomUUID()); this.changed.fire([{type:vscode.FileChangeType.Changed,uri}]); this.didSave(); }
    catch(error) {
      if (error instanceof OperationError && error.status===409) {
        const latest=await this.client.invoke<Recipe>('recipe.get',{project_id:doc.current!.project_id,recipe_id:doc.current!.recipe_id});
        const server=await vscode.workspace.openTextDocument({content:JSON.stringify(latest,null,2),language:'json'});
        await vscode.commands.executeCommand('vscode.diff',server.uri,uri,`Recipe conflict: server r${latest.revision} ↔ local r${doc.current!.revision}`);
        throw vscode.FileSystemError.Unavailable('409 conflict. Your unsaved source is preserved. Compare the server revision, then reopen the recipe to resolve.');
      }
      throw error;
    }
  }
}

export class Workbench implements vscode.Disposable {
  get currentProject():Project|undefined{return this.project;}
  private panel?:vscode.WebviewPanel;
  private catalog:OperationSpec[]=[];
  private project?:Project;
  private document:RecipeDocument;
  private dirty=false;
  private busy=false;
  private documentImportActive=false;
  private polling=false;
  private runId?:string;
  private runRefreshSequence=0;
  private timer:NodeJS.Timeout;
  private generation=0;
  private disposed=false;
  private localSource?:{projectId:string;alias:string;repositoryId:string;folder:vscode.Uri};
  private readonly notifications:EvaluationNotifications;
  private watchedRuns=new Set<string>();
  private drafts:Record<string,{base:Recipe;state:RecordValue}>={};
  constructor(private context:vscode.ExtensionContext,private client:OperationClient,private explorer:ModelExplorer,private files:RecipeFileSystem,private readonly projectChanged:(projectId?:string)=>void=()=>{}) {
    this.document=new RecipeDocument(client);
    this.notifications=new EvaluationNotifications(context.workspaceState?.get('modelbuilder.evaluationNotificationState'),context.workspaceState?.get('modelbuilder.evaluationNotificationPreferences'));
    this.drafts=context.workspaceState?.get('modelbuilder.unsavedDrafts',{})??{};
    this.timer=setInterval(()=>void this.poll(),4000);
  }
  dispose() { this.projectChanged(undefined);this.disposed=true;this.generation++;clearInterval(this.timer);this.panel?.dispose();this.panel=undefined;this.watchedRuns.clear(); }
  private post(type:string,body:RecordValue={}) { return this.panel?.webview.postMessage({type,...body}); }
  async open(nav:Navigation={}) {
    if (!this.panel) {
      this.panel=vscode.window.createWebviewPanel('modelbuilder.workbench','Model Workbench',vscode.ViewColumn.One,{enableScripts:true,retainContextWhenHidden:true,localResourceRoots:[vscode.Uri.joinPath(this.context.extensionUri,'media')]});
      this.panel.webview.html=this.html(this.panel.webview);
      this.panel.onDidDispose(()=>{this.panel=undefined;this.generation++;});
      this.panel.webview.onDidReceiveMessage(raw=>void this.message(raw));
    }
    this.panel.reveal();
    if (nav.projectId){if(!this.catalog.length)this.catalog=await this.client.catalog();await this.navigate(nav);}
  }
  private async ready() {
    this.catalog=await this.client.catalog();
    const {projects}=await this.client.invoke('project.list',{});
    const datasetSchema=JSON.parse(await readFile(join(this.context.extensionUri.fsPath,'schemas','dataset.v1.schema.json'),'utf8'));
    await this.post('init',{catalog:this.catalog,projects,trusted:vscode.workspace.isTrusted,notificationPreferences:this.notifications.preferences,datasetSchema});
    if (this.project) {await this.navigate({projectId:this.project.project_id});if(this.document.current){await this.post('recipe',{recipe:this.document.current});const backup=this.drafts[this.document.current.recipe_id];if(backup){this.dirty=true;await this.post('restoreDraft',{state:backup.state});}}}
  }
  private async navigate(nav:Navigation) {
    if (!nav.projectId) return;
    if(this.project && nav.projectId!==this.project.project_id){await this.closeProject();return;}
    const generation=++this.generation;
    let snapshot=await this.client.invoke('project.open',{project_id:nav.projectId});
    if (generation!==this.generation) return;
    if(this.project?.project_id!==snapshot.project.project_id&&snapshot.source?.binding&&this.catalog.some(item=>item.name==='project.source.list')){
      const prepared=await this.prepareProjectSource(snapshot,generation);if(!prepared||generation!==this.generation)return;snapshot=prepared;
    }
    const current=currentProjectRecipe(snapshot);
    const changedProject=this.project?.project_id!==snapshot.project.project_id;
    this.project=snapshot.project;this.projectChanged(this.project!.project_id); if(changedProject){this.watchedRuns.clear();if(this.notifications.state.pending?.projectId!==this.project!.project_id)this.notifications.state.pending=undefined;}
    this.runId=nav.runId??(snapshot.runs??[]).find((run:RecordValue)=>['pending','submitted','queued','running','cancel_requested','cancel_pending'].includes(run.status))?.operation_id??(snapshot.runs??[])[0]?.operation_id;this.explorer.setProject?.(this.project);await vscode.commands.executeCommand('setContext','modelbuilder.projectOpen',true);if(this.panel)this.panel.title=this.project!.model_name;
    await this.post('project',{snapshot,section:nav.section === 'Recipes' ? 'Recipe' : nav.section ?? 'Recipe',runId:this.runId,notificationPreferences:this.notifications.preferences});
    if(this.document.current?.recipe_id!==current.recipe_id || !this.dirty)await this.loadRecipe(current.recipe_id);
    await this.refreshRun();
    await this.refreshCapabilities();
    await this.refreshResearch();
    await this.refreshDatasets();
  }
  private async prepareProjectSource(snapshot:RecordValue,generation:number):Promise<RecordValue|undefined> {
    const project=snapshot.project,choice=await chooseLocalSource(this.client,project.project_id);if(!choice||generation!==this.generation)return;
    const binding=snapshot.source?.binding;
    if(binding?.alias!==choice.source.alias||binding?.repository_id!==choice.source.repository_id){
      if(!await acknowledgeSource(choice)||generation!==this.generation)return;
      try{await this.client.invoke('project.source.bind',sourceBindingPayload(choice,project.project_id,project.revision,randomUUID()));}
      catch(error){if(error instanceof Error&&'status' in error&&error.status===409){await this.client.invoke('project.open',{project_id:project.project_id});throw new Error('The project or source identity changed. Open the project again to review its current source; no new identity was accepted.');}throw error;}
      snapshot=await this.client.invoke('project.open',{project_id:project.project_id});
    }
    if(generation!==this.generation)return;
    this.localSource={projectId:project.project_id,alias:choice.source.alias,repositoryId:choice.source.repository_id,folder:choice.folder};
    if(!(vscode.workspace.workspaceFolders??[]).length){await this.context.globalState?.update('modelbuilder.pendingSourceProject',{projectId:project.project_id,folderPath:choice.folder.fsPath});await vscode.commands.executeCommand('vscode.openFolder',choice.folder,{forceNewWindow:false});return;}
    return snapshot;
  }
  private async openProjectCode():Promise<void> {
    this.requireProject();if(!this.catalog.some(item=>item.name==='project.source.status')){await vscode.commands.executeCommand('workbench.view.explorer');return;}
    const projectId=this.project!.project_id,generation=this.generation,status=await this.client.invoke('project.source.status',{project_id:projectId});if(generation!==this.generation)return;
    const binding=status.binding;if(!binding||!status.available||status.repository_id!==binding.repository_id)throw new Error('The model source is unbound or unavailable. Reopen the project after the operator configures its source.');
    const folders=vscode.workspace.workspaceFolders??[];if(folders.length!==1)throw new Error('Reopen the project and choose its approved source folder.');
    const current=await matchLocalSource(folders[0].uri,[{...status,alias:binding.alias,repository_id:binding.repository_id,workspace_path:status.workspace_path,operator_trusted:status.operator_trusted,available:status.available}]);
    if(current.alias!==binding.alias||current.repository_id!==binding.repository_id)throw new Error('The open folder no longer matches this project’s source identity. Reopen the project.');
    if(generation===this.generation)await vscode.commands.executeCommand('workbench.view.explorer');
  }
  private async pinRecipeSource(message:RecordValue):Promise<void> {
    this.requireProject();if(!this.document.current)throw new Error('Open the current model recipe before pinning source.');
    if(!['project.source.status','recipe.source.pin.preview','recipe.source.pin'].every(name=>this.catalog.some(item=>item.name===name)))throw new Error('This service does not support reviewed source pinning.');
    const generation=this.generation,document=this.document,project=structuredClone(this.project!),recipe=structuredClone(document.current!);
    if(message.projectId!==project.project_id||message.recipeId!==recipe.recipe_id||message.expectedRevision!==recipe.revision||message.expectedProjectRevision!==project.revision)throw new Error('The displayed source review is stale. Reload the project before pinning source.');
    const unsaved=()=>this.dirty||vscode.workspace.textDocuments?.some(doc=>doc.isDirty&&doc.uri.scheme==='modelrecipe'&&doc.uri.path===`/${project.project_id}/${recipe.recipe_id}.modelrecipe.json`);
    const assertCurrent=()=>{if(generation!==this.generation||this.document!==document||project.project_id!==this.project?.project_id||project.revision!==this.project?.revision||recipe.recipe_id!==this.document.current?.recipe_id||recipe.revision!==this.document.current?.revision)throw new Error('The project or recipe changed during source review. Start a new review; no revision was rebased.');if(unsaved())throw new Error('Save or explicitly discard the unsaved recipe draft before pinning source. Your draft is preserved.');};
    assertCurrent();
    const result=await reviewAndPinSource(this.client,{project,recipe,sourceRepositoryId:message.sourceRepositoryId,assertCurrent});if(!result)return;
    if(generation!==this.generation||this.document!==document||project.project_id!==this.project?.project_id){this.explorer.refresh();return;}
    if(result.recipe?.project_id!==project.project_id||result.recipe?.recipe_id!==recipe.recipe_id||result.recipe?.revision!==recipe.revision+1||result.execution_allowed!==false)throw new Error('The source pin response did not match this recipe. Reload to inspect the result.');
    if(unsaved()||this.document.current?.revision!==recipe.revision)await this.post('remoteChanged',{revision:result.recipe.revision,digest:result.recipe.digest});
    else{this.document.current=result.recipe;this.files.acceptRemote?.(result.recipe);await this.post('recipe',{recipe:result.recipe});}
    this.explorer.refresh();await this.refreshSnapshot();await this.post('result',{operation:'recipe.source.pin',result});
  }
  async closeProject():Promise<boolean> {
    if(this.project){
      const tabs=vscode.window.tabGroups?.all.flatMap(group=>group.tabs).filter(tab=>{const input=tab.input as {uri?:vscode.Uri;modified?:vscode.Uri};const uri=input.uri??input.modified;return uri?.scheme==='modelrecipe'&&uri.path.startsWith(`/${this.project!.project_id}/`);})??[];
      if(tabs.length&&!await vscode.window.tabGroups.close(tabs,true))return false;
      if(this.dirty&&!await this.confirmDiscard())return false;
    }
    const generation=++this.generation;this.project=undefined;this.projectChanged(undefined);this.localSource=undefined;this.runId=undefined;this.watchedRuns.clear();this.notifications.state.pending=undefined;this.document=new RecipeDocument(this.client);this.dirty=false;this.explorer.setProject?.();if(this.panel)this.panel.title='Model projects';
    await vscode.commands.executeCommand('setContext','modelbuilder.projectOpen',false);await this.post('landing',{projects:[]});
    const {projects}=await this.client.invoke('project.list',{});if(generation===this.generation&&!this.project)await this.post('landing',{projects});return true;
  }
  async registryFile(action:'discover'|'publish'):Promise<void> {
    this.requireProject();if(!this.document.current)throw new Error('Open the model recipe first.');if(this.busy)throw new Error('Wait for the current operation to finish.');
    const generation=this.generation,project=structuredClone(this.project!),recipe=structuredClone(this.document.current);
    const assertCurrent=()=>{if(generation!==this.generation||project.project_id!==this.project?.project_id||recipe.recipe_id!==this.document.current?.recipe_id)throw new Error('The model project changed while the registry dialog was open. No further action was taken.');if(this.dirty||vscode.workspace.textDocuments?.some(doc=>doc.isDirty&&doc.uri.scheme==='modelrecipe'&&doc.uri.path===`/${project.project_id}/${recipe.recipe_id}.modelrecipe.json`))throw new Error('Save or explicitly discard the unsaved recipe draft before this File action. Your draft is preserved.');};assertCurrent();
    this.busy=true;await this.post('busy',{busy:true});
    try{const result=await (action==='discover'?discoverRegistryRecipe:publishRegistryRecipe)(this.client,{project,recipe,assertCurrent});if(!result)return;if(generation!==this.generation||project.project_id!==this.project?.project_id)return;
      if(action==='discover'&&result.recipe){if(result.recipe.recipe_id!==recipe.recipe_id||result.recipe.project_id!==project.project_id)throw new Error('The service returned a different recipe identity. The current editor was preserved.');if(this.dirty||vscode.workspace.textDocuments?.some(doc=>doc.isDirty&&doc.uri.scheme==='modelrecipe'&&doc.uri.path===`/${project.project_id}/${recipe.recipe_id}.modelrecipe.json`))await this.post('remoteChanged',{revision:result.recipe.revision,digest:result.recipe.digest});else{this.document.current=result.recipe;this.files.acceptRemote?.(result.recipe);await this.post('recipe',{recipe:result.recipe});}await this.refreshSnapshot();this.explorer.refresh();void vscode.window.showInformationMessage(`Private recipe imported as revision ${result.recipe.revision} of this model's recipe. No source files were written or code executed.`);}
      if(action==='publish')void vscode.window.showInformationMessage(`Published ${result.package_name}@${result.package_version} to the local private recipe registry.`);
    }catch(error){await this.post('error',{message:error instanceof Error?error.message:String(error)});throw error;}finally{this.busy=false;await this.post('busy',{busy:false});}
  }
  /** Native File commands use editor dialogs and the same service, never webview I/O. */
  async recipeBundleFile(action:'import'|'export'):Promise<void> {
    this.requireProject();if(!this.document.current)throw new Error('Open the model recipe first.');
    if(this.busy)throw new Error('Wait for the current operation to finish.');
    if(this.dirty)throw new Error('Save your current draft or explicitly reload it before importing or exporting a recipe. Your draft is preserved.');
    const generation=this.generation, projectId=this.project!.project_id, recipeId=this.document.current.recipe_id, baseRevision=this.document.current.revision;
    const requireSaved=()=>{if(this.dirty||vscode.workspace.textDocuments?.some(doc=>doc.isDirty&&doc.uri.scheme==='modelrecipe'&&doc.uri.path===`/${projectId}/${recipeId}.modelrecipe.json`))throw new Error('Save or explicitly discard the unsaved recipe source before this File action. All drafts have been preserved.');};
    requireSaved();
    const unchanged=()=>{requireSaved();if(generation!==this.generation||projectId!==this.project?.project_id||recipeId!==this.document.current?.recipe_id)throw new Error('The model project changed while the file dialog was open. No file operation was completed.');};
    this.busy=true;await this.post('busy',{busy:true});
    try {
      if(action==='import'){
        const selected=await vscode.window.showOpenDialog({title:'Import Recipe',openLabel:'Import Recipe',canSelectMany:false,canSelectFiles:true,canSelectFolders:false,filters:{'Recipe JSON bundles':['json']}});
        if(!selected?.[0])return;unchanged();
        const stat=await vscode.workspace.fs.stat(selected[0]);unchanged();if(stat.size>8*1024*1024)throw new Error('Recipe bundle exceeds the 8 MiB limit.');
        const bytes=await vscode.workspace.fs.readFile(selected[0]);if(bytes.byteLength>8*1024*1024)throw new Error('Recipe bundle exceeds the 8 MiB limit.');
        const bundle_json=new TextDecoder('utf-8',{fatal:true}).decode(bytes);unchanged();
        await this.operation('recipe.import',{bundle_json,expected_revision:baseRevision},true);
      }else{
        // Capture exact server bytes before the dialog; later MCP edits cannot alter this file.
        const result=await this.client.invoke('recipe.export',{project_id:projectId,recipe_id:recipeId,format:'json'});unchanged();
        if(typeof result.bundle_json!=='string')throw new Error('Service did not return a lossless recipe bundle.');
        const bytes=new TextEncoder().encode(result.bundle_json);if(bytes.byteLength>8*1024*1024)throw new Error('Recipe bundle exceeds the 8 MiB limit.');
        const capturedRevision=JSON.parse(result.bundle_json).recipe.revision;
        const folder=vscode.workspace.workspaceFolders?.[0]?.uri;
        const destination=await vscode.window.showSaveDialog({title:`Export Recipe (revision ${capturedRevision})`,saveLabel:'Export Recipe',defaultUri:folder?vscode.Uri.joinPath(folder,'model.recipe.json'):undefined,filters:{'Recipe JSON bundles':['json']}});
        if(!destination)return;unchanged();
        await vscode.workspace.fs.writeFile(destination,bytes);
        void vscode.window.showInformationMessage(`Recipe revision ${capturedRevision} exported to ${destination.fsPath}.`);
      }
    }catch(error){await this.post('error',{message:error instanceof Error?error.message:String(error),code:error instanceof OperationError?error.code:undefined});throw error;}
    finally{this.busy=false;await this.post('busy',{busy:false});}
  }
  private async confirmDiscard():Promise<boolean> {const confirmed=await vscode.window.showWarningMessage('Discard unsaved recipe changes?',{modal:true},'Discard')==='Discard';if(confirmed)this.dirty=false;if(confirmed&&this.document.current){delete this.drafts[this.document.current.recipe_id];await this.persistDrafts();}return confirmed;}
  private async persistDrafts(){await this.context.workspaceState?.update('modelbuilder.unsavedDrafts',this.drafts);}
  private async loadRecipe(id:string) {
    if (!this.project) return;
    if (this.dirty && !await this.confirmDiscard()) return;
    const generation=++this.generation;
    const next=new RecipeDocument(this.client); const recipe=await next.load(this.project.project_id,id);
    if (generation!==this.generation) return;
    const backup=this.drafts[id];this.document=next;this.dirty=Boolean(backup);if(backup){next.current=backup.base;await this.post('recipe',{recipe:backup.base});await this.post('restoreDraft',{state:backup.state});if(recipe.revision!==backup.base.revision)await this.post('remoteChanged',{revision:recipe.revision,digest:recipe.digest});}else await this.post('recipe',{recipe});
  }
  /** Historical bodies stay read-only; clone restores into this recipe's next revision. */
  private async recipeHistoryAction(message:RecordValue) {
    this.requireProject();
    const document=this.document, base=document.current, generation=this.generation;
    if(!base || message.projectId!==this.project!.project_id || message.projectId!==base.project_id || message.recipeId!==base.recipe_id || message.expectedRevision!==base.revision){await this.post('recipeHistorySettled',{projectId:message.projectId,recipeId:message.recipeId,requestId:message.requestId});throw new Error('The model recipe changed. Reopen revision history before continuing.');}
    const context={projectId:base.project_id,recipeId:base.recipe_id,requestId:message.requestId};
    const current=()=>!this.disposed&&generation===this.generation&&this.document===document&&this.project?.project_id===base.project_id&&document.current?.recipe_id===base.recipe_id;
    const authorized=(operation:string)=>{if(!this.catalog.some(item=>item.name===operation&&item.authorized!==false))throw new Error(`Recipe history requires access to ${operation}.`);};
    const post=async(action:string,result:RecordValue,extra:RecordValue={})=>{if(current())await this.post('recipeHistoryResult',{...context,expectedRevision:action==='restore'?document.current!.revision:base.revision,action,result,...extra});};
    const historical=(value:RecordValue)=>{if(value.recipe_id!==base.recipe_id||value.project_id!==base.project_id||value.revision!==message.sourceRevision)throw new Error('The historical revision returned by the service does not match this recipe.');};
    const nativeDrafts=()=>vscode.workspace.textDocuments?.filter(item=>item.isDirty&&item.uri.scheme==='modelrecipe'&&item.uri.path===`/${base.project_id}/${base.recipe_id}.modelrecipe.json`)??[];
    const workbenchDraftIdentity=()=>JSON.stringify({dirty:this.dirty,state:this.drafts[base.recipe_id]?.state});
    const draftIdentity=()=>JSON.stringify({workbench:workbenchDraftIdentity(),native:nativeDrafts().map(item=>({version:item.version,text:item.getText?.()}))});
    try {
      if(message.action==='list') {
        authorized('recipe.history');
        const result=await this.client.invoke('recipe.history',{project_id:base.project_id});
        if(result.recipe_id!==base.recipe_id||!Array.isArray(result.revisions)||result.revisions.some((item:Recipe)=>item.recipe_id!==base.recipe_id||item.project_id!==base.project_id))throw new Error('Recipe history does not match the open model recipe.');
        await post('list',result);
      } else if(message.action==='inspect') {
        authorized('recipe.get');
        const result=await this.client.invoke('recipe.get',{project_id:base.project_id,recipe_id:base.recipe_id,revision:message.sourceRevision});
        historical(result);await post('inspect',result);
      } else if(message.action==='diff') {
        authorized('recipe.diff');
        const result=await this.client.invoke('recipe.diff',{project_id:base.project_id,recipe_id:base.recipe_id,from_revision:message.fromRevision,to_revision:message.toRevision});
        await post('diff',result,{fromRevision:message.fromRevision,toRevision:message.toRevision});
      } else if(message.action==='restore') {
        for(const operation of ['recipe.clone','recipe.get','recipe.diff','recipe.history'])authorized(operation);
        const identity=draftIdentity(), workbenchIdentity=workbenchDraftIdentity(), hadDraft=this.dirty, hadNativeDraft=nativeDrafts().length>0;
        const selected=await this.client.invoke('recipe.get',{project_id:base.project_id,recipe_id:base.recipe_id,revision:message.sourceRevision});
        if(!current())return;historical(selected);
        const diff=await this.client.invoke('recipe.diff',{project_id:base.project_id,recipe_id:base.recipe_id,from_revision:base.revision,to_revision:message.sourceRevision});
        if(!current())return;
        await post('inspect',selected);await post('diff',diff,{fromRevision:base.revision,toRevision:message.sourceRevision});
        const hasDraft=hadDraft||hadNativeDraft;
        const keep=hasDraft?'Restore and keep drafts':'Restore revision';
        const discard='Discard workbench draft and restore';
        const warning=`Restore saved revision ${message.sourceRevision} (“${selected.name}”) as revision ${base.revision+1} of this same recipe? Previous revisions and runs remain available. No code is executed.`
          +(hadDraft?' Your unsaved workbench draft can be kept at its original base revision, or explicitly discarded only after restore succeeds.':'')
          +(hadNativeDraft?' Unsaved native JSON will be kept at its original base revision; save or revert it in its editor when ready.':'');
        const choice=await vscode.window.showWarningMessage(warning,{modal:true},keep,...(hadDraft?[discard]:[]));
        if(choice!==keep&&!(hadDraft&&choice===discard))return;
        if(!current())return;
        if(document.current!==base||draftIdentity()!==identity)throw new Error('The recipe or an unsaved draft changed during restore review. Reopen history and review again. All drafts have been preserved.');
        let restored:Recipe;
        try {
          restored=await this.client.invoke<Recipe>('recipe.clone',{project_id:base.project_id,recipe_id:base.recipe_id,source_revision:message.sourceRevision,expected_revision:base.revision,idempotency_key:randomUUID()});
        } catch(error) {
          if(current()&&error instanceof Error&&'status' in error&&error.status===409)throw new Error('Restore conflicted with a newer saved recipe revision. No restore was applied and all drafts are preserved. Refresh history; explicitly reload or resolve your draft before trying again.');
          throw error;
        }
        if(restored.project_id!==base.project_id||restored.recipe_id!==base.recipe_id||restored.revision!==base.revision+1)throw new Error('The service returned an unexpected restore result. Refresh history before continuing; do not repeat the restore without reviewing saved revisions.');
        this.files.acceptRemote?.(restored);this.explorer.refresh();
        if(!current())return;
        // A draft typed while the request was in flight takes precedence over discard.
        const retainedDraft=(hadDraft&&choice===keep)||workbenchDraftIdentity()!==workbenchIdentity;
        if(retainedDraft) {
          this.dirty=true;
          await this.post('remoteChanged',{revision:restored.revision,digest:restored.digest});
        } else {
          document.current=restored;this.dirty=false;delete this.drafts[base.recipe_id];await this.persistDrafts();
          if(!current())return;
          await this.post('recipe',{recipe:restored});
        }
        if(!current())return;
        void vscode.window.showInformationMessage(`Recipe revision ${message.sourceRevision} restored as revision ${restored.revision}. Previous revisions and runs remain available.`);
        // Restoration is already committed. A failed history refresh must never suggest retrying it.
        try {
          const history=await this.client.invoke('recipe.history',{project_id:base.project_id});
          await post('restore',history,{sourceRevision:message.sourceRevision,restoredRevision:restored.revision,retainedDraft,retainedNativeDraft:nativeDrafts().length>0});
          if(current())await this.refreshSnapshot();
        } catch(error) {if(current())await this.post('error',{message:`Restored successfully as revision ${restored.revision}, but the history refresh failed. Refresh history to inspect it. ${error instanceof Error?error.message:String(error)}`});}
      }
    } catch(error) {if(current())throw error;}
    finally {if(current())await this.post('recipeHistorySettled',context);}
  }
  private async message(raw:unknown) {
    let message:RecordValue;
    try { message=checkMessage(raw); } catch(error) {await this.post('error',{message:error instanceof Error?error.message:'Invalid workbench message.'});return;}
    if (message.type==='dirty') { this.dirty=message.dirty===true; return; }
    if(message.type==='draftState'){if(this.document.current&&message.recipeId===this.document.current.recipe_id&&message.state&&typeof message.state.source==='string'){this.drafts[message.recipeId]={base:this.document.current,state:message.state};await this.persistDrafts();}return;}
    if (this.busy) { await this.post('error',{message:'An operation is already in progress. Wait for its result.'}); return; }
    this.busy=true;this.documentImportActive=message.type==='importResearchDocument';await this.post('busy',{busy:true});
    try {
      switch(message.type) {
        case 'ready': await this.ready();break;
        case 'configure': await vscode.commands.executeCommand('modelbuilder.configureConnection');await this.ready();break;
        case 'createProject': if(this.project)throw new Error('Close this model project before creating another.');await vscode.commands.executeCommand('modelbuilder.createProject');await this.ready();break;
        case 'selectProject': if(this.project)throw new Error('Close this model project before opening another.');await this.navigate({projectId:String(message.projectId)});break;
        case 'closeProject': await this.closeProject();break;
        case 'refresh': if(this.project) await this.navigate({projectId:this.project.project_id});else await this.ready();break;
        case 'nativeSource': if(this.document.current) await vscode.window.showTextDocument(await this.files.open(this.document.current.project_id,this.document.current.recipe_id),{viewColumn:vscode.ViewColumn.Beside});break;
        case 'saveRecipe': {
          const draft=JSON.parse(String(message.source));
          const savingDocument=this.document;const generation=this.generation;const submittedDraftState=JSON.stringify(this.drafts[savingDocument.current!.recipe_id]?.state);
          try {const recipe=await savingDocument.save(draft,randomUUID());this.files.acceptRemote(recipe);if(generation===this.generation&&this.document===savingDocument){const pending=this.drafts[recipe.recipe_id];if(pending&&JSON.stringify(pending.state)!==submittedDraftState){pending.base=recipe;this.dirty=true;await this.persistDrafts();await this.post('savedRetained',{recipe});}else{this.dirty=false;delete this.drafts[recipe.recipe_id];await this.persistDrafts();await this.post('recipe',{recipe});}await this.refreshSnapshot();}this.explorer.refresh();}
          catch(error) {if(error instanceof OperationError && error.status===409 && generation===this.generation && this.document===savingDocument) {const diff=await savingDocument.diff(draft);const server=await this.client.invoke('recipe.get',{project_id:this.project!.project_id,recipe_id:this.document.current!.recipe_id});await this.post('conflict',{details:error.details,diff,server,local:draft});} throw error;}
          break;
        }
        case 'validateRecipe': await this.post('result',{operation:'recipe.validate',result:await this.document.validate(JSON.parse(String(message.source)))});break;
        case 'recipeHistory': await this.recipeHistoryAction(message);break;
        case 'diffRecipe': await this.post('result',{operation:'recipe.diff',result:await this.document.diff(JSON.parse(String(message.source)))});break;
        case 'reloadRecipe': if(this.document.current) await this.loadRecipe(this.document.current.recipe_id);break;
        case 'operation': await this.operation(String(message.operation),message.payload ?? {});break;
        case 'selectRun': this.runId=String(message.operationId);await this.refreshRun();break;
        case 'nativeSaveModel': {
          this.requireProject();const generation=this.generation,projectId=this.project!.project_id;
          for(const operation of ['model.export.list','model.export.package','model.export.read'])if(!this.catalog.some(item=>item.name===operation))throw new Error('Model export read access is unavailable.');
          const selection:RecordValue={project_id:projectId};
          if(typeof message.backendRunId==='string')selection.backend_run_id=message.backendRunId;
          if(typeof message.modelVersionId==='string')selection.model_version_id=message.modelVersionId;
          if(!selection.backend_run_id&&!selection.model_version_id)throw new Error('Select a native run or version first.');
          const saved=await saveNativeModel(this.client,selection,()=>{if(generation!==this.generation||projectId!==this.project?.project_id||this.disposed)throw new Error('The model project changed; save was stopped.');});
          if(saved)void vscode.window.showInformationMessage(`Model package saved to ${saved}`);break;
        }
        case 'notificationPreferences':
          this.requireProject();this.notifications.updatePreferences(message.preferences);
          await this.context.workspaceState?.update('modelbuilder.evaluationNotificationPreferences',this.notifications.preferences);
          await this.post('notificationPreferences',{preferences:this.notifications.preferences});break;
        case 'refreshDatasets': await this.refreshDatasets();break;
        case 'refreshCosts': await this.refreshCostDiagnostics();break;
        case 'prepareDatasetVersion': {
          this.requireProject();const generation=this.generation,projectId=this.project!.project_id;
          const dataset=await this.client.invoke('dataset.get',{project_id:projectId,dataset_id:String(message.datasetId),revision:message.revision});
          if(generation===this.generation&&projectId===this.project?.project_id)await this.post('datasetVersion',{projectId,dataset});break;
        }
        case 'refreshResearch': await this.refreshSnapshot();await this.refreshResearch();break;
        case 'importResearchDocument': {
          this.requireProject();const generation=this.generation,projectId=this.project!.project_id,endpoint=this.client.endpoint();
          if(message.projectId!==projectId)throw new Error('The model project changed. Reopen the citation before importing.');
          for(const name of ['research.document.import','research.document.capabilities','evidence.get'])if(!this.catalog.some(item=>item.name===name&&item.authorized!==false))throw new Error('Document import is not authorized on this service.');
          const current=()=>{if(generation!==this.generation||projectId!==this.project?.project_id||this.disposed||endpoint!==this.client.endpoint())throw new Error('The model project or service changed; PDF import was stopped.');};
          const result=await importResearchPdf(this.client,{project_id:projectId,evidence_id:message.evidenceId,expected_evidence_revision:message.expectedEvidenceRevision},endpoint,current);
          if(result&&generation===this.generation&&projectId===this.project?.project_id&&!this.disposed&&endpoint===this.client.endpoint()){await this.post('result',{operation:'research.document.import',result});await this.refreshResearch();this.explorer.refresh();}break;
        }
        case 'setupAssistant': await vscode.commands.executeCommand('modelbuilder.setupAssistant');break;
        case 'openCode': await this.openProjectCode();break;
        case 'pinRecipeSource': await this.pinRecipeSource(message);break;
        default: throw new Error('Unknown workbench action.');
      }
    } catch(error) {await this.post('error',{message:error instanceof Error ? error.message : String(error),code:error instanceof OperationError ? error.code : undefined,details:error instanceof OperationError ? error.details : undefined});}
    finally {this.busy=false;this.documentImportActive=false;await this.post('busy',{busy:false});}
  }
  private requireProject() {if(!this.project) throw new Error('Select a model project first.');}
  private async operation(name:string,raw:unknown,fromFile=false) {
    this.requireProject();
    if((!UI_OPERATIONS.has(name)&&!(fromFile&&name==='recipe.import')) || !raw || typeof raw!=='object' || Array.isArray(raw)) throw new Error('Unsupported workbench operation.');
    const generation=this.generation;
    const spec=this.catalog.find(item=>item.name===name);if(!spec) throw new Error('Operation is not available on this service.');
    const payload:RecordValue={...raw as RecordValue,project_id:this.project!.project_id};
    delete payload.idempotency_key;delete payload.request_id;
    if(['run.submit','run.resume'].includes(name)&&(this.dirty||vscode.workspace.textDocuments?.some(doc=>doc.isDirty&&doc.uri.scheme==='modelrecipe'&&doc.uri.path.startsWith(`/${this.project!.project_id}/`))))throw new Error('Save the recipe draft before starting or resuming a run, so the run uses your visible training and evaluation settings.');
    if(spec.scope==='execute') {
      if(!vscode.workspace.isTrusted) throw new Error('Execution requires a trusted workspace.');
      const tracking=payload.tracking??this.project!.tracking;
      const online=['run.submit','run.resume'].includes(name)&&tracking?.mode==='online';
      delete payload.tracking_online_consent;
      const disclosure=online?` This run also sends allowlisted metrics (${(tracking.metric_names??[]).join(', ')||'none'}), sanitized run identity${tracking.artifact_policy==='metrics_snapshot'?' and a metrics JSON snapshot':''} to Weights & Biases at ${tracking.entity}/${tracking.project}. Source code, datasets and checkpoints are not uploaded.`:'';
      if(await vscode.window.showWarningMessage((name==='run.cancel'?'Request cancellation? The run remains active until the backend confirms termination.':'Run trusted local code? Remote spending is disabled.')+disclosure,{modal:true},'Continue')!=='Continue') return;
      if(online){payload.tracking=structuredClone(tracking);payload.tracking_online_consent=true;}
    }
    if(spec.mutating) payload.idempotency_key=randomUUID();
    for(const key of ['recipe_id','expected_revision','expected_recipe_revision'])if((name.startsWith('recipe.')||['run.plan','run.submit','run.resume','dataset.bind'].includes(name))&&key in spec.input_schema.properties){if(!this.document.current)throw new Error('This project has no current recipe.');payload[key]=key==='recipe_id'?this.document.current.recipe_id:fromFile&&name==='recipe.import'&&key==='expected_revision'?(raw as RecordValue).expected_revision:this.document.current.revision;}
    if(name==='recipe.import'){if(this.dirty)throw new Error('Save your current draft or explicitly reload it before importing a recipe. Your draft is preserved.');if(await vscode.window.showWarningMessage('Open this recipe bundle into the current model project? Its contents become a new revision of this model’s recipe. Previous revisions and runs remain available.',{modal:true},'Open bundle')!=='Open bundle')return;}
    if(['research.proposal.create','research.proposal.apply'].includes(name)){
      if(!this.document.current)throw new Error('Open the current recipe before staging or applying a proposal.');
      payload.expected_recipe_revision=this.document.current.revision;
    }
    if(name==='recipe.smoke.prepare'){
      const draftDirty=()=>this.dirty||vscode.workspace.textDocuments?.some(doc=>doc.isDirty&&doc.uri.scheme==='modelrecipe'&&doc.uri.path.startsWith(`/${this.project!.project_id}/`));
      if(draftDirty())throw new Error('Save or explicitly reload your recipe draft before saving a smoke revision. Your draft is preserved.');
      const revision=this.document.current?.revision;
      if(await vscode.window.showWarningMessage(`Save a non-production smoke revision of this same recipe at revision ${payload.expected_revision}? Only declared component budgets will be capped. Existing revisions stay available. No code is executed and smoke checkpoints cannot be promoted.`,{modal:true},'Save smoke revision')!=='Save smoke revision')return;
      if(generation!==this.generation||draftDirty()||this.document.current?.revision!==revision)throw new Error('The project or recipe changed during smoke review. Your draft is preserved.');
    }
    if(name==='dataset.bind'){
      const draftDirty=()=>this.dirty||vscode.workspace.textDocuments?.some(doc=>doc.isDirty&&doc.uri.scheme==='modelrecipe'&&doc.uri.path.startsWith(`/${this.project!.project_id}/`));
      if(draftDirty())throw new Error('Save or explicitly reload your recipe draft before binding a dataset. Your draft is preserved.');
      if(await vscode.window.showWarningMessage(`Bind dataset ${payload.dataset_id} revision ${payload.dataset_revision}, split ${payload.split_id} (${payload.purpose}) to recipe revision ${payload.expected_revision}? This updates import node ${payload.node_id}${payload.target_node?` and replaces the connection to ${payload.target_node}.${payload.target_port}`:''}. Data remains at its approved source. No code is executed.`,{modal:true},'Bind dataset split')!=='Bind dataset split')return;
      if(generation!==this.generation||draftDirty())throw new Error('The project or recipe draft changed during dataset review. Your draft is preserved.');
    }
    if(name==='research.proposal.apply'){
      if(this.dirty||vscode.workspace.textDocuments?.some(doc=>doc.isDirty&&doc.uri.scheme==='modelrecipe'&&doc.uri.path.startsWith(`/${this.project!.project_id}/`)))throw new Error('Save or explicitly reload your current recipe draft before applying a proposal. Your draft is preserved.');
      delete payload.confirm_apply;
      const proposal=await this.client.invoke('research.proposal.get',{project_id:this.project!.project_id,proposal_id:payload.proposal_id});
      if(generation!==this.generation)throw new Error('The model project changed before proposal review.');
      if(proposal.status!=='approved')throw new Error('Explicitly approve this proposal before applying it.');
      await this.post('result',{operation:'research.proposal.get',result:proposal});
      if(await vscode.window.showWarningMessage(`Apply approved proposal “${proposal.title??payload.proposal_id}” to recipe revision ${payload.expected_recipe_revision}? Review its exact staged diff and citations. This creates the next recipe revision; it does not execute training or verify scientific claims.`,{modal:true},'Apply approved proposal')!=='Apply approved proposal')return;
      payload.confirm_apply=true;
      if(this.dirty||vscode.workspace.textDocuments?.some(doc=>doc.isDirty&&doc.uri.scheme==='modelrecipe'&&doc.uri.path.startsWith(`/${this.project!.project_id}/`)))throw new Error('The recipe draft changed during proposal review. Your draft is preserved.');
    }
    if(name==='run.submit') {
      if(!this.document.current)throw new Error('Open a recipe first.');
      const recipe=this.document.current;
      const targets=await this.client.invoke('execution.targets',{project_id:this.project!.project_id});
      const policy=await this.client.invoke('policy.get',{project_id:this.project!.project_id});
      const available:RecordValue[]=(targets.targets??[]).filter((target:RecordValue)=>target.available===true&&policy.allowed_targets.includes(target.id));
      if(!available.length)throw new Error('No available local execution target is permitted by this project policy.');
      const choice=await vscode.window.showQuickPick(available.map((target:RecordValue)=>({label:target.id,
        description:`${target.device} placement; no resource reservation`,
        detail:target.exact_resume?'Exact continuation is limited to compatible CPU checkpoints.':'Accelerator exact continuation is unverified; warm start only.',target})),
        {title:'Select the recipe execution device',placeHolder:'Must match the saved recipe environment.device and its pinned profile'});
      if(!choice)return;
      const selected=(choice as {target:RecordValue}).target;
      if(selected.device!==(recipe.environment?.device??'cpu'))throw new Error('Save a recipe revision with the selected environment.device and matching pinned environment profile before submitting.');
      if(generation!==this.generation||this.document.current?.revision!==recipe.revision)throw new Error('The recipe changed during device selection. Review the current revision before submitting.');
      payload.recipe_id=recipe.recipe_id;payload.expected_revision=recipe.revision;payload.trusted=true;payload.target=selected.id;
    }
    if(fromFile&&(this.dirty||vscode.workspace.textDocuments?.some(doc=>doc.isDirty&&doc.uri.scheme==='modelrecipe'&&doc.uri.path===`/${this.project!.project_id}/${this.document.current!.recipe_id}.modelrecipe.json`)))throw new Error('Unsaved recipe source appeared while the dialog was open. Your draft is preserved.');
    if(generation!==this.generation)throw new Error('The model project changed before the operation was submitted.');
    let result:RecordValue;try{result=await this.client.invoke(name,payload);}catch(error){if(name==='model.version.promote'&&error instanceof OperationError&&error.status===409&&generation===this.generation){await this.refreshSnapshot();}throw error;}
    if(generation!==this.generation){this.explorer.refresh();return;}
    if(!['recipe.import','recipe.export'].includes(name))await this.post('result',{operation:name,result,request:['dataset.preview','run.compare'].includes(name)?payload:undefined});
    if(result.operation_id && ['run.submit','run.resume'].includes(name)) {
      this.runId=result.operation_id;this.watchedRuns.add(result.operation_id);
      this.notifications.startRun(this.project!.project_id,result.operation_id);await this.persistNotificationState();
    }
    if(name.startsWith('run.') && this.runId) await this.refreshRun();
    if(name==='recipe.import'&&this.document.current){if(this.dirty)await this.post('remoteChanged',{revision:result.revision,digest:result.digest});else await this.loadRecipe(this.document.current.recipe_id);void vscode.window.showInformationMessage(`Recipe imported as revision ${result.revision}. No code was executed.`);}
    if(['research.proposal.apply','dataset.bind','recipe.smoke.prepare'].includes(name)&&result.recipe){if(this.dirty||vscode.workspace.textDocuments?.some(doc=>doc.isDirty&&doc.uri.scheme==='modelrecipe'&&doc.uri.path.startsWith(`/${this.project!.project_id}/`))){await this.post('remoteChanged',{revision:result.recipe.revision,digest:result.recipe.digest});}else{this.document.current=result.recipe;this.files.acceptRemote?.(result.recipe);await this.post('recipe',{recipe:result.recipe});}}
    if(spec.mutating) {this.explorer.refresh();await this.refreshSnapshot();}
    if(name==='model.version.promote'){const versions=await this.client.invoke('model.version.list',{project_id:payload.project_id});if(generation===this.generation)await this.post('result',{operation:'model.version.list',result:versions});}
    if(name.startsWith('research.'))await this.refreshResearch();
    if(['dataset.register','dataset.version'].includes(name))await this.refreshDatasets();
  }
  private async refreshDatasets(){
    if(!this.project||!this.catalog.some(item=>item.name==='dataset.list'))return;
    const generation=this.generation,projectId=this.project.project_id;
    try{const result=await this.client.invoke('dataset.list',{project_id:projectId});if(generation===this.generation&&projectId===this.project?.project_id)await this.post('datasets',{projectId,datasets:result.datasets??[]});}
    catch(error){if(generation===this.generation&&projectId===this.project?.project_id)await this.post('datasets',{projectId,datasets:[],error:error instanceof Error?error.message:String(error)});}
  }
  private async refreshResearch(){
    if(!this.project)return;
    const generation=this.generation,projectId=this.project.project_id;
    const requests=[['watches','research.watch.list'],['findings','research.finding.list'],['proposals','research.proposal.list'],['texts','research.text.list']].filter(([,operation])=>this.catalog.some(item=>item.name===operation));
    if(!requests.length)return;
    const values=await Promise.allSettled(requests.map(([,operation])=>this.client.invoke(operation,{project_id:projectId})));
    if(generation!==this.generation||projectId!==this.project?.project_id)return;
    const research:RecordValue={errors:{}};
    requests.forEach(([key],index)=>{const value=values[index];if(value.status==='fulfilled'){research[key]=value.value[key]??[];if(key==='texts')research.retrievals=value.value.retrievals??[];}else research.errors[key]=value.reason instanceof Error?value.reason.message:String(value.reason);});
    await this.post('research',{projectId,research});
  }
  private async refreshCostDiagnostics(){
    this.requireProject();const generation=this.generation,projectId=this.project!.project_id;
    const requests=[['providers','cost.providers'],['ledger','cost.ledger.get'],['history','cost.history']].filter(([,name])=>this.catalog.some(item=>item.name===name&&!item.mutating));
    const results=await Promise.allSettled(requests.map(([,name])=>this.client.invoke(name,{project_id:projectId})));if(generation!==this.generation||projectId!==this.project?.project_id)return;
    const result:RecordValue={errors:{}};requests.forEach(([key],index)=>{const response=results[index];if(response.status==='fulfilled')result[key]=response.value;else result.errors[key]=response.reason instanceof Error?response.reason.message:String(response.reason);});
    if(!requests.length)result.errors.service='This service does not expose remote cost diagnostics.';
    await this.post('costDiagnostics',{projectId,result});
  }
  private async refreshCapabilities(){
    if(!this.project||!this.catalog.some(item=>item.name==='execution.targets'))return;
    const generation=this.generation,projectId=this.project.project_id;
    try{const result=await this.client.invoke('execution.targets',{project_id:projectId});if(generation===this.generation&&projectId===this.project?.project_id)await this.post('capabilities',{projectId,result});}
    catch(error){if(generation===this.generation)await this.post('connection',{message:error instanceof Error?error.message:String(error)});}
  }
  private async refreshSnapshot() {if(!this.project)return;const generation=this.generation;const projectId=this.project.project_id;const snapshot=await this.client.invoke('project.open',{project_id:projectId});if(generation===this.generation&&projectId===this.project?.project_id){this.project=snapshot.project;await this.post('snapshot',{snapshot});}}
  private async refreshRun() {
    if(!this.project || !this.runId)return;
    const generation=this.generation;const runId=this.runId;const projectId=this.project.project_id;const sequence=++this.runRefreshSequence;
    const payload={project_id:projectId,operation_id:runId};
    const operations=['run.status','run.logs','run.artifacts'];
    if(this.catalog.some(item=>item.name==='run.evaluations.list'))operations.push('run.evaluations.list');
    const values=await Promise.allSettled(operations.map(operation=>this.client.invoke(operation,operation==='run.evaluations.list'&&this.catalog.find(item=>item.name===operation)?.input_schema?.properties?.limit?{...payload,limit:EVALUATION_WINDOW}:payload)));
    if(this.disposed||sequence!==this.runRefreshSequence||generation!==this.generation||runId!==this.runId||projectId!==this.project?.project_id)return;
    const result:RecordValue={operation_id:runId};
    ['status','logs','artifacts','evaluations'].slice(0,values.length).forEach((key,index)=>{const value=values[index];result[key]=value.status==='fulfilled'?value.value:{unavailable:value.reason instanceof Error?value.reason.message:String(value.reason)};});
    if(!result.evaluations)result.evaluations={unavailable:'This service does not expose live checkpoint evaluations.'};
    await this.post('run',{projectId,run:result});
  }
  private async persistNotificationState(){await this.context.workspaceState?.update('modelbuilder.evaluationNotificationState',this.notifications.state);}
  private async refreshNotifications() {
    if(!this.project||this.disposed||!this.catalog.some(item=>item.name==='run.events.list'))return;
    const generation=this.generation,projectId=this.project.project_id;
    const current=()=>!this.disposed&&generation===this.generation&&projectId===this.project?.project_id;
    const response=await this.client.invoke('run.list',{project_id:projectId});if(!current())return;
    const runs:RecordValue[]=response.runs??[];
    const active=(status:string)=>['pending','submitted','queued','running','cancel_requested','cancel_pending'].includes(status);
    for(const run of runs)if(active(run.status))this.watchedRuns.add(run.operation_id);
    if(this.runId)this.watchedRuns.add(this.runId);
    await this.post('runList',{projectId,runs});
    for(const runId of [...this.watchedRuns]){
      const page=await this.client.invoke('run.events.list',{project_id:projectId,operation_id:runId,after:this.notifications.cursor(projectId,runId),limit:200});
      if(!current())return;
      this.notifications.consume(projectId,runId,page.events??[],page.next_cursor,page.has_more===true);
      if(!page.has_more&&this.notifications.hasTerminal(projectId,runId)&&runId!==this.runId&&!active(runs.find(run=>run.operation_id===runId)?.status))this.watchedRuns.delete(runId);
    }
    // Save cursor and cooldown before showing a toast, so reconnect/restart cannot replay it.
    const notice=this.notifications.takeNotice(Date.now(),projectId);await this.persistNotificationState();if(!current()||!notice)return;
    const text=`${this.project!.model_name}: ${notice.message}${notice.count>1?` (${notice.count} new evaluation updates.)`:''}`;
    if(notice.kind==='failure'||notice.kind==='regression')void vscode.window.showWarningMessage(text);
    else void vscode.window.showInformationMessage(text);
  }
  private async poll() {
    if(this.disposed||(this.busy&&!this.documentImportActive)||this.polling||!this.project)return;
    this.polling=true;const generation=this.generation;
    try {
      const polledDocument=this.document;
      const current=polledDocument.current;
      if(this.panel?.visible&&current) {
        const next=await this.client.invoke<Recipe>('recipe.get',{project_id:current.project_id,recipe_id:current.recipe_id});
        if(generation===this.generation && this.document===polledDocument && polledDocument.current===current && next.revision!==current.revision) {
          if(this.dirty) await this.post('remoteChanged',{revision:next.revision,digest:next.digest});
          else {this.document.current=next;this.files.acceptRemote?.(next);await this.post('recipe',{recipe:next});}
        }
      }
      if(generation===this.generation&&this.panel?.visible)await this.refreshRun();
      if(generation===this.generation)await this.refreshNotifications();
    } catch(error) {if(generation===this.generation&&!this.disposed)await this.post('connection',{message:error instanceof Error?error.message:String(error)});}
    finally {this.polling=false;}
  }
  private html(webview:vscode.Webview):string {
    const nonce=randomBytes(24).toString('base64');
    const script=webview.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri,'media','workbench.js'));
    const css=webview.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri,'media','workbench.css'));
    return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${webview.cspSource}; style-src ${webview.cspSource}; script-src 'nonce-${nonce}'; connect-src 'none'; font-src ${webview.cspSource};"><link rel="stylesheet" href="${escapeHtml(css.toString())}"><title>ModelBuilder Workbench</title></head><body><div id="app"></div><script nonce="${nonce}" src="${escapeHtml(script.toString())}"></script></body></html>`;
  }
}

export function activate(context:vscode.ExtensionContext) {
  void vscode.commands.executeCommand('setContext','modelbuilder.projectOpen',false);
  // Supported workbench commands only: hide stock assistant chrome without a core fork.
  if(vscode.workspace.getConfiguration('chat').get('disableAIFeatures',true))void vscode.commands.executeCommand('workbench.action.closeAuxiliaryBar');
  const bundled=corePaths(context.extensionUri.fsPath,context.globalStorageUri?join(context.globalStorageUri.fsPath,'core'):'',vscode.env?.appRoot);
  const managed=bundled?new ManagedCore(bundled):undefined;
  const nativeReviews=managed?new NativeReviewController(managed):undefined;
  if(managed&&nativeReviews)context.subscriptions.push(managed,nativeReviews);
  const client=new OperationClient(()=>managed?.url??vscode.workspace.getConfiguration('modelbuilder').get('serviceUrl','http://127.0.0.1:8765'),async(base)=>{const stored=await context.secrets.get(TOKEN_KEY);if(!stored)return undefined;try{const credential=JSON.parse(stored);return credential.url===base && typeof credential.token==='string'?credential.token:undefined;}catch{return undefined;}},fetch,managed?()=>managed.connection():undefined);
  const stopProjection=attachProjectProjection(client,id=>context.globalState.get<Record<string,string>>('modelbuilder.projectFolders',{})[id],message=>void vscode.window.showWarningMessage(message));
  context.subscriptions.push({dispose:stopProjection});
  const explorer=new ModelExplorer(client);
  const files=new RecipeFileSystem(client,()=>explorer.refresh());
  const hubReview=managed&&nativeReviews?async(operation:string,payload:RecordValue,beforeDecision:()=>void):Promise<RecordValue|undefined>=>{
    beforeDecision();
    const queued=await managed.requestReview(payload.project_id,operation,payload);
    beforeDecision();
    const decided=await nativeReviews.review(queued,beforeDecision);
    if(!decided||!['succeeded','active'].includes(decided.status))return undefined;
    const receipt=decided.result;
    if(!receipt||typeof receipt!=='object'||Array.isArray(receipt))throw new Error('The reviewed Hub operation returned an invalid receipt. Inspect its request status before retrying.');
    return receipt as RecordValue;
  }:undefined;
  const hub=new HubController(client,()=>client.endpoint(),hubReview);context.subscriptions.push(hub);
  const workbench=new Workbench(context,client,explorer,files,id=>hub.setProject(id));
  context.subscriptions.push(vscode.workspace.onDidChangeConfiguration(event=>{if(event.affectsConfiguration('modelbuilder.serviceUrl'))hub.connectionChanged();}));
  context.subscriptions.push(workbench,vscode.workspace.registerFileSystemProvider('modelrecipe',files,{isCaseSensitive:true}),vscode.window.registerTreeDataProvider('modelbuilder.explorer',explorer));
  const register=(name:string,fn:(...args:any[])=>any)=>context.subscriptions.push(vscode.commands.registerCommand(name,async(...args:any[])=>{try{return await fn(...args);}catch(error){void vscode.window.showErrorMessage(error instanceof Error?error.message:String(error));}}));
  register('modelbuilder.configureHub',async()=>{
    const project=workbench.currentProject;
    if(!project||!managed)throw new Error('Open a model project in the managed app before configuring Hub settings.');
    const endpoint=client.endpoint();
    return configureHub(managed,project,()=>{if(workbench.currentProject?.project_id!==project.project_id||client.endpoint()!==endpoint)throw new Error('The model project or connection changed during Hub settings.');});
  });
  register('modelbuilder.openHub',()=>{hub.setProject(workbench.currentProject?.project_id);return hub.open();});
  register('modelbuilder.hubStage',(path:string,projectId:string,commit:string)=>hub.stage(path,projectId,commit));
  register('modelbuilder.hubUnstage',(path:string,projectId:string,commit:string)=>hub.stage(path,projectId,commit,true));
  register('modelbuilder.hubPublishStaged',()=>hub.publish());
  const assistantOptions=(project:Project)=>({projectId:project.project_id,...(managed?{managed:{python:managed.paths.python,command:managed.mcpCommand(project.project_id)}}:{})});
  register('modelbuilder.setupAssistant',async()=>{
    const project=workbench.currentProject;if(!project)throw new Error('Open a model project before configuring its optional assistant.');
    const folder=context.globalState.get<Record<string,string>>('modelbuilder.projectFolders',{})[project.project_id];
    return configureOptionalAssistant(context,undefined,{...assistantOptions(project),...(folder?{workspace:vscode.Uri.file(folder)}:{})});
  });
  register('modelbuilder.registerProjectSource',async()=>{
    const project=workbench.currentProject;if(!project||!managed)throw new Error('Open a model project in the managed app before registering trusted source.');
    const folder=context.globalState.get<Record<string,string>>('modelbuilder.projectFolders',{})[project.project_id];if(!folder)throw new Error('This project has no selected local layout folder.');
    const current=()=>{if(workbench.currentProject?.project_id!==project.project_id)throw new Error('The model project changed during source registration.');};
    const result=await registerProjectSource(client,project,vscode.Uri.file(folder),(path,id,trusted,kind)=>managed.registerSource(path,id,trusted,kind),current);
    if(result){explorer.refresh();await workbench.open({projectId:project.project_id,section:'Code'});}
  });
  const newProject=new NewProjectView(context,client,{
    assistantOptions,
    registerSource:managed?(path,id,trusted,kind)=>managed.registerSource(path,id,trusted,kind):undefined,
    downloadBaseModel:async(project,assertCurrent)=>{await openHubWorkflow(client,project.project_id,assertCurrent,hubReview);},
    onCreated:async(project,folder,start)=>{
      if(!await workbench.closeProject())throw new Error('The new project is saved. Existing unsaved work was preserved; close it when ready to open the new project.');
      const opened=vscode.workspace.workspaceFolders??[];
      if(folder&&(opened.length!==1||opened[0].uri.scheme!=='file'||opened[0].uri.fsPath!==folder.fsPath)){
        await context.globalState.update('modelbuilder.pendingSourceProject',{projectId:project.project_id,folderPath:folder.fsPath,section:start==='research'?'Research':start==='scratch'?'Code':'Recipe'});
        await vscode.commands.executeCommand('vscode.openFolder',folder,{forceNewWindow:false});return;
      }
      explorer.refresh();await workbench.open({projectId:project.project_id,section:start==='research'?'Research':start==='scratch'?'Code':'Recipe'});
    }
  });context.subscriptions.push(newProject);
  register('modelbuilder.openWorkbench',(nav:Navigation={})=>workbench.open(nav));
  register('modelbuilder.importRecipeBundle',()=>workbench.recipeBundleFile('import'));
  register('modelbuilder.exportRecipeBundle',()=>workbench.recipeBundleFile('export'));
  register('modelbuilder.discoverPrivateRecipe',()=>workbench.registryFile('discover'));
  register('modelbuilder.publishPrivateRecipe',()=>workbench.registryFile('publish'));
  register('modelbuilder.closeProject',()=>workbench.closeProject());
  register('modelbuilder.refresh',()=>explorer.refresh());
  register('modelbuilder.configureConnection',async(options?:{url?:string;token?:string})=>{
    if(managed){await managed.connection();await client.catalog();explorer.refresh();void vscode.window.showInformationMessage('The bundled ModelBuilder core is connected.');return;}
    if(!vscode.workspace.isTrusted) throw new Error('Trust this workspace before configuring a service credential.');
    const url=options?.url ?? await vscode.window.showInputBox({title:'ModelBuilder service URL',value:vscode.workspace.getConfiguration('modelbuilder').get('serviceUrl','http://127.0.0.1:8765'),validateInput:value=>{try{serviceUrl(value);return null;}catch(e){return (e as Error).message;}}});
    if(!url)return; serviceUrl(url);
    const token=options?.token ?? await vscode.window.showInputBox({title:'ModelBuilder scoped service token',password:true,prompt:'Stored in the operating system secret store. Never sent to the webview or written to settings.',ignoreFocusOut:true});
    if(!token)return;
    await context.secrets.store(TOKEN_KEY,JSON.stringify({url:serviceUrl(url),token}));await vscode.workspace.getConfiguration('modelbuilder').update('serviceUrl',url,vscode.ConfigurationTarget.Global);
    await client.catalog();explorer.refresh();void vscode.window.showInformationMessage('Connected to the ModelBuilder operation service.');
  });
  register('modelbuilder.createProject',()=>newProject.open());
  register('modelbuilder.openRecipeSource',async(projectId?:string,recipeId?:string)=>{
    if(!projectId || !recipeId) throw new Error('Open the model project and use Open native JSON in its Recipe editor.');
    return vscode.window.showTextDocument(await files.open(projectId,recipeId));
  });
  const pending=context.globalState?.get<{projectId:string;folderPath:string;section?:string}>('modelbuilder.pendingSourceProject');const openFolders=vscode.workspace.workspaceFolders??[];if(pending&&openFolders.length===1&&openFolders[0].uri.scheme==='file'&&openFolders[0].uri.fsPath===pending.folderPath){void (async()=>{await context.globalState.update('modelbuilder.pendingSourceProject',undefined);await workbench.open({projectId:pending.projectId,section:pending.section});})().catch(error=>vscode.window.showErrorMessage(error instanceof Error?error.message:String(error)));}
  return {client,explorer,files,workbench,managed,hub,clearConnection:()=>context.secrets.delete(TOKEN_KEY)};
}
export function deactivate() {}
