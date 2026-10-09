import * as vscode from 'vscode';
import { randomBytes, randomUUID } from 'node:crypto';
import { OperationClient, OperationError } from './client';
import { RecipeDocument } from './document';
import { checkMessage, escapeHtml, serviceUrl, UI_OPERATIONS } from './security';
import { OperationSpec, Project, Recipe, RecordValue } from './types';

const TOKEN_KEY = 'modelbuilder.serviceToken';
const SECTIONS = ['Research', 'Code', 'Data', 'Recipes', 'Runs', 'Versions'] as const;
type Navigation = {projectId?:string; section?:string; recipeId?:string; runId?:string};
class ExplorerItem extends vscode.TreeItem {
  constructor(label:string, readonly kind:string, readonly project?:Project, readonly record?:RecordValue) {
    super(label, kind === 'project' || kind === 'section' ? vscode.TreeItemCollapsibleState.Collapsed : vscode.TreeItemCollapsibleState.None);
    this.contextValue = `modelbuilder.${kind}`;
    if (project) {
      this.command = {command:'modelbuilder.openWorkbench',title:'Open',arguments:[{projectId:project.project_id,section:kind === 'section' ? label : kind,recipeId:record?.recipe_id,runId:record?.operation_id}]};
      this.tooltip = record ? JSON.stringify(record,null,2) : `${project.model_name}\nProject ${project.project_id}\nRevision ${project.revision}`;
    }
    this.iconPath = new vscode.ThemeIcon(({project:'project',Research:'book',Code:'code',Data:'database',Recipes:'type-hierarchy',Runs:'play-circle',Versions:'versions'} as Record<string,string>)[kind === 'section' ? label : kind] ?? 'circle-outline');
    if (record) this.description = String(record.status ?? (record.revision ? `r${record.revision}` : record.backend_run_id ?? ''));
  }
}
class ModelExplorer implements vscode.TreeDataProvider<ExplorerItem> {
  private changed = new vscode.EventEmitter<ExplorerItem | undefined>();
  readonly onDidChangeTreeData = this.changed.event;
  constructor(private client:OperationClient) {}
  refresh() { this.changed.fire(undefined); }
  getTreeItem(item:ExplorerItem) { return item; }
  async getChildren(item?:ExplorerItem):Promise<ExplorerItem[]> {
    try {
      if (!item) return (await this.client.invoke<{projects:Project[]}>('project.list',{})).projects.map(project => new ExplorerItem(project.model_name,'project',project));
      if (item.kind === 'project') return SECTIONS.map(section=>new ExplorerItem(section,'section',item.project));
      if (item.kind !== 'section' || !item.project) return [];
      const project = item.project;
      const payload = {project_id:project.project_id};
      const operations:Record<string,[string,string,string]> = {Research:['hypothesis.list','hypotheses','proposed_change'],Recipes:['recipe.list','recipes','name'],Runs:['run.list','runs','operation_id'],Versions:['model.version.list','versions','name']};
      const config = operations[String(item.label)];
      if (!config) return [new ExplorerItem(item.label === 'Code' ? 'Open model code and Git references' : 'Inspect recipe data references',String(item.label),project)];
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
  async readFile(uri:vscode.Uri) { return Buffer.from(JSON.stringify((await this.document(uri)).current,null,2)+'\n'); }
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
  private panel?:vscode.WebviewPanel;
  private catalog:OperationSpec[]=[];
  private project?:Project;
  private document:RecipeDocument;
  private dirty=false;
  private busy=false;
  private polling=false;
  private runId?:string;
  private timer:NodeJS.Timeout;
  private generation=0;
  private drafts:Record<string,{base:Recipe;state:RecordValue}>={};
  constructor(private context:vscode.ExtensionContext,private client:OperationClient,private explorer:ModelExplorer,private files:RecipeFileSystem) {
    this.document=new RecipeDocument(client);
    this.drafts=context.workspaceState?.get('modelbuilder.unsavedDrafts',{})??{};
    this.timer=setInterval(()=>void this.poll(),4000);
  }
  dispose() { clearInterval(this.timer); this.panel?.dispose(); }
  private post(type:string,body:RecordValue={}) { return this.panel?.webview.postMessage({type,...body}); }
  async open(nav:Navigation={}) {
    if (!this.panel) {
      this.panel=vscode.window.createWebviewPanel('modelbuilder.workbench','Model Workbench',vscode.ViewColumn.One,{enableScripts:true,retainContextWhenHidden:true,localResourceRoots:[vscode.Uri.joinPath(this.context.extensionUri,'media')]});
      this.panel.webview.html=this.html(this.panel.webview);
      this.panel.onDidDispose(()=>{this.panel=undefined;this.generation++;});
      this.panel.webview.onDidReceiveMessage(raw=>void this.message(raw));
    }
    this.panel.reveal();
    if (nav.projectId) await this.navigate(nav);
  }
  private async ready() {
    this.catalog=await this.client.catalog();
    const {projects}=await this.client.invoke('project.list',{});
    await this.post('init',{catalog:this.catalog,projects,trusted:vscode.workspace.isTrusted});
    if (this.project) {await this.navigate({projectId:this.project.project_id});if(this.document.current){await this.post('recipe',{recipe:this.document.current});const backup=this.drafts[this.document.current.recipe_id];if(backup){this.dirty=true;await this.post('restoreDraft',{state:backup.state});}}}
  }
  private async navigate(nav:Navigation) {
    if (!nav.projectId) return;
    if (this.dirty && nav.projectId !== this.project?.project_id && !await this.confirmDiscard()) return;
    const generation=++this.generation;
    const snapshot=await this.client.invoke('project.open',{project_id:nav.projectId});
    if (generation!==this.generation) return;
    this.project=snapshot.project; this.runId=nav.runId;
    await this.post('project',{snapshot,section:nav.section ?? 'Recipes',runId:nav.runId});
    if (nav.recipeId || (!this.document.current && snapshot.recipes?.length)) await this.loadRecipe(nav.recipeId ?? snapshot.recipes[0].recipe_id);
    else if (this.document.current?.project_id!==nav.projectId) { this.document=new RecipeDocument(this.client); this.dirty=false; await this.post('recipe',{recipe:null}); }
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
  private async message(raw:unknown) {
    let message:RecordValue;
    try { message=checkMessage(raw); } catch(error) {await this.post('error',{message:error instanceof Error?error.message:'Invalid workbench message.'});return;}
    if (message.type==='dirty') { this.dirty=message.dirty===true; return; }
    if(message.type==='draftState'){if(this.document.current&&message.recipeId===this.document.current.recipe_id&&message.state&&typeof message.state.source==='string'){this.drafts[message.recipeId]={base:this.document.current,state:message.state};await this.persistDrafts();}return;}
    if (this.busy) { await this.post('error',{message:'An operation is already in progress. Wait for its result.'}); return; }
    this.busy=true;await this.post('busy',{busy:true});
    try {
      switch(message.type) {
        case 'ready': await this.ready();break;
        case 'configure': await vscode.commands.executeCommand('modelbuilder.configureConnection');await this.ready();break;
        case 'createProject': await vscode.commands.executeCommand('modelbuilder.createProject');await this.ready();break;
        case 'selectProject': await this.navigate({projectId:String(message.projectId)});break;
        case 'refresh': if(this.project) await this.navigate({projectId:this.project.project_id});else await this.ready();break;
        case 'openRecipe': await this.loadRecipe(String(message.recipeId));break;
        case 'newRecipe': {
          this.requireProject();
          const name=await vscode.window.showInputBox({title:'New recipe',prompt:'Recipe name',value:'Model recipe'});
          if(name) {const recipe=await this.client.invoke<Recipe>('recipe.create',{project_id:this.project!.project_id,name,idempotency_key:randomUUID()});await this.loadRecipe(recipe.recipe_id);await this.refreshSnapshot();this.explorer.refresh();}
          break;
        }
        case 'nativeSource': if(this.document.current) await vscode.window.showTextDocument(await this.files.open(this.document.current.project_id,this.document.current.recipe_id),{viewColumn:vscode.ViewColumn.Beside});break;
        case 'saveRecipe': {
          const draft=JSON.parse(String(message.source));
          const savingDocument=this.document;const generation=this.generation;const submittedDraftState=JSON.stringify(this.drafts[savingDocument.current!.recipe_id]?.state);
          try {const recipe=await savingDocument.save(draft,randomUUID());this.files.acceptRemote(recipe);if(generation===this.generation&&this.document===savingDocument){const pending=this.drafts[recipe.recipe_id];if(pending&&JSON.stringify(pending.state)!==submittedDraftState){pending.base=recipe;this.dirty=true;await this.persistDrafts();await this.post('savedRetained',{recipe});}else{this.dirty=false;delete this.drafts[recipe.recipe_id];await this.persistDrafts();await this.post('recipe',{recipe});}await this.refreshSnapshot();}this.explorer.refresh();}
          catch(error) {if(error instanceof OperationError && error.status===409 && generation===this.generation && this.document===savingDocument) {const diff=await savingDocument.diff(draft);const server=await this.client.invoke('recipe.get',{project_id:this.project!.project_id,recipe_id:this.document.current!.recipe_id});await this.post('conflict',{details:error.details,diff,server,local:draft});} throw error;}
          break;
        }
        case 'validateRecipe': await this.post('result',{operation:'recipe.validate',result:await this.document.validate(JSON.parse(String(message.source)))});break;
        case 'diffRecipe': await this.post('result',{operation:'recipe.diff',result:await this.document.diff(JSON.parse(String(message.source)))});break;
        case 'reloadRecipe': if(this.document.current) await this.loadRecipe(this.document.current.recipe_id);break;
        case 'operation': await this.operation(String(message.operation),message.payload ?? {});break;
        case 'selectRun': this.runId=String(message.operationId);await this.refreshRun();break;
        case 'openCode': await vscode.commands.executeCommand('workbench.view.explorer');break;
        default: throw new Error('Unknown workbench action.');
      }
    } catch(error) {await this.post('error',{message:error instanceof Error ? error.message : String(error),code:error instanceof OperationError ? error.code : undefined,details:error instanceof OperationError ? error.details : undefined});}
    finally {this.busy=false;await this.post('busy',{busy:false});}
  }
  private requireProject() {if(!this.project) throw new Error('Select a model project first.');}
  private async operation(name:string,raw:unknown) {
    this.requireProject();
    if(!UI_OPERATIONS.has(name) || !raw || typeof raw!=='object' || Array.isArray(raw)) throw new Error('Unsupported workbench operation.');
    const generation=this.generation;
    const spec=this.catalog.find(item=>item.name===name);if(!spec) throw new Error('Operation is not available on this service.');
    const payload:RecordValue={...raw as RecordValue,project_id:this.project!.project_id};
    delete payload.idempotency_key;delete payload.request_id;
    if(spec.scope==='execute') {
      if(!vscode.workspace.isTrusted) throw new Error('Execution requires a trusted workspace.');
      if(await vscode.window.showWarningMessage(name==='run.cancel'?'Request cancellation? The run remains active until the backend confirms termination.':'Run trusted local code? Only repository-owned fixture execution is supported; remote spending is disabled.',{modal:true},'Continue')!=='Continue') return;
    }
    if(spec.mutating) payload.idempotency_key=randomUUID();
    if(name==='run.submit') {if(!this.document.current)throw new Error('Open a recipe first.');payload.recipe_id=this.document.current.recipe_id;payload.expected_revision=this.document.current.revision;payload.trusted=true;payload.target='local-cpu';}
    const result=await this.client.invoke(name,payload);
    if(generation!==this.generation){this.explorer.refresh();return;}
    await this.post('result',{operation:name,result});
    if(result.operation_id && ['run.submit','run.resume'].includes(name)) this.runId=result.operation_id;
    if(name.startsWith('run.') && this.runId) await this.refreshRun();
    if(spec.mutating) {this.explorer.refresh();await this.refreshSnapshot();}
  }
  private async refreshSnapshot() {if(!this.project)return;const generation=this.generation;const projectId=this.project.project_id;const snapshot=await this.client.invoke('project.open',{project_id:projectId});if(generation===this.generation&&projectId===this.project?.project_id)await this.post('snapshot',{snapshot});}
  private async refreshRun() {
    if(!this.project || !this.runId)return;
    const generation=this.generation;const runId=this.runId;const projectId=this.project.project_id;
    const payload={project_id:projectId,operation_id:runId};
    const values=await Promise.allSettled(['run.status','run.logs','run.artifacts'].map(operation=>this.client.invoke(operation,payload)));
    if(generation!==this.generation||runId!==this.runId||projectId!==this.project?.project_id)return;
    const result:RecordValue={operation_id:runId};
    ['status','logs','artifacts'].forEach((key,index)=>{const value=values[index];result[key]=value.status==='fulfilled'?value.value:{unavailable:value.reason instanceof Error?value.reason.message:String(value.reason)};});
    await this.post('run',{run:result});
  }
  private async poll() {
    if(!this.panel?.visible || this.busy || this.polling || !this.project)return;
    this.polling=true;
    try {
      const generation=this.generation;
      const polledDocument=this.document;
      const current=polledDocument.current;
      if(current) {
        const next=await this.client.invoke<Recipe>('recipe.get',{project_id:current.project_id,recipe_id:current.recipe_id});
        if(generation===this.generation && this.document===polledDocument && polledDocument.current===current && next.revision!==current.revision) {
          if(this.dirty) await this.post('remoteChanged',{revision:next.revision,digest:next.digest});
          else {this.document.current=next;this.files.acceptRemote?.(next);await this.post('recipe',{recipe:next});}
        }
      }
      if(generation===this.generation) await this.refreshRun();
    } catch(error) {await this.post('connection',{message:error instanceof Error?error.message:String(error)});}
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
  // Supported workbench commands only: hide stock assistant chrome without a core fork.
  if(vscode.workspace.getConfiguration('chat').get('disableAIFeatures',true))void vscode.commands.executeCommand('workbench.action.closeAuxiliaryBar');
  const client=new OperationClient(()=>vscode.workspace.getConfiguration('modelbuilder').get('serviceUrl','http://127.0.0.1:8765'),async(base)=>{const stored=await context.secrets.get(TOKEN_KEY);if(!stored)return undefined;try{const credential=JSON.parse(stored);return credential.url===base && typeof credential.token==='string'?credential.token:undefined;}catch{return undefined;}});
  const explorer=new ModelExplorer(client);
  const files=new RecipeFileSystem(client,()=>explorer.refresh());
  const workbench=new Workbench(context,client,explorer,files);
  context.subscriptions.push(workbench,vscode.workspace.registerFileSystemProvider('modelrecipe',files,{isCaseSensitive:true}),vscode.window.registerTreeDataProvider('modelbuilder.explorer',explorer));
  const register=(name:string,fn:(...args:any[])=>any)=>context.subscriptions.push(vscode.commands.registerCommand(name,async(...args:any[])=>{try{return await fn(...args);}catch(error){void vscode.window.showErrorMessage(error instanceof Error?error.message:String(error));}}));
  register('modelbuilder.openWorkbench',(nav:Navigation={})=>workbench.open(nav));
  register('modelbuilder.refresh',()=>explorer.refresh());
  register('modelbuilder.configureConnection',async(options?:{url?:string;token?:string})=>{
    if(!vscode.workspace.isTrusted) throw new Error('Trust this workspace before configuring a service credential.');
    const url=options?.url ?? await vscode.window.showInputBox({title:'ModelBuilder service URL',value:vscode.workspace.getConfiguration('modelbuilder').get('serviceUrl','http://127.0.0.1:8765'),validateInput:value=>{try{serviceUrl(value);return null;}catch(e){return (e as Error).message;}}});
    if(!url)return; serviceUrl(url);
    const token=options?.token ?? await vscode.window.showInputBox({title:'ModelBuilder scoped service token',password:true,prompt:'Stored in the operating system secret store. Never sent to the webview or written to settings.',ignoreFocusOut:true});
    if(!token)return;
    await context.secrets.store(TOKEN_KEY,JSON.stringify({url:serviceUrl(url),token}));await vscode.workspace.getConfiguration('modelbuilder').update('serviceUrl',url,vscode.ConfigurationTarget.Global);
    await client.catalog();explorer.refresh();void vscode.window.showInformationMessage('Connected to the ModelBuilder operation service.');
  });
  register('modelbuilder.createProject',async()=>{
    const model_name=await vscode.window.showInputBox({title:'Create model project',prompt:'Name of the one model this project develops'});if(!model_name)return;
    const slug=await vscode.window.showInputBox({title:'Project slug',value:model_name.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'')});if(!slug)return;
    const project=await client.invoke<Project>('project.create',{model_name,slug,idempotency_key:randomUUID()});explorer.refresh();await workbench.open({projectId:project.project_id});
  });
  register('modelbuilder.openRecipeSource',async(projectId?:string,recipeId?:string)=>{
    if(!projectId || !recipeId) throw new Error('Select a recipe and use Open native JSON in the workbench.');
    return vscode.window.showTextDocument(await files.open(projectId,recipeId));
  });
  return {client,explorer,files,workbench,clearConnection:()=>context.secrets.delete(TOKEN_KEY)};
}
export function deactivate() {}
