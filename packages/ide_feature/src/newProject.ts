import * as vscode from 'vscode';
import { randomUUID, randomBytes } from 'node:crypto';
import { Project, Recipe, RecordValue, ServiceTransport } from './types';
import { ProjectQuickstartSession, HARNESS_CAPABILITIES, StartingPoint } from './projectQuickstart';
import { acknowledgeSource, chooseLocalSource } from './sourceWorkspace';
import { configureOptionalAssistant, AssistantSetupOptions } from './assistantSetup';
import { escapeHtml } from './security';
import { ProjectDraftRecovery } from './projectDraftRecovery';
import { registerProjectSource, ManagedSourceRegistration } from './projectSourceSetup';
import { checkScaffoldTarget, createProjectScaffold, SCAFFOLD_PATHS } from './projectScaffold';

export interface NewProjectHooks {
  onCreated(project:Project,folder:vscode.Uri|undefined,start:StartingPoint):Promise<void>;
  assistantOptions?(project:Project):AssistantSetupOptions;
  downloadBaseModel?(project:Project,assertCurrent:()=>void):Promise<void>;
  registerSource?:ManagedSourceRegistration;
}
/** Dedicated New Project page. The existing opened-workspace UI is not replaced. */
export class NewProjectView implements vscode.Disposable {
  private panel?:vscode.WebviewPanel;
  private session?:ProjectQuickstartSession;
  private folder?:vscode.Uri;
  private source?:{source_alias:string;source_repository_id:string;trusted:true};
  private nativeBusy=false;
  private opened=false;
  private scaffoldReady=false;
  private opening=false;
  private recovery:ProjectDraftRecovery;
  constructor(private context:vscode.ExtensionContext,private client:ServiceTransport,private hooks:NewProjectHooks){this.recovery=new ProjectDraftRecovery(context);}
  async open(){
    if(this.panel){this.panel.reveal();return;}if(this.opening)return;this.opening=true;
    try{
      const pending=await this.recovery.list();
      const resumeChoice=pending.length?await vscode.window.showQuickPick([...pending.map(item=>({label:item.folder,description:'Resume this approved project creation',pending:item})),{label:'Create a different model project',description:'Keeps pending requests available for later recovery',pending:undefined}],{title:'New Project or resume approved creation'}):undefined;
      if(pending.length&&!resumeChoice)return;const selected=resumeChoice?.pending;
      this.session=selected?ProjectQuickstartSession.restore(selected.request):new ProjectQuickstartSession(randomUUID());this.folder=selected?vscode.Uri.file(selected.folder):undefined;this.source=selected?.request.source;this.opened=false;this.scaffoldReady=false;
      const panel=vscode.window.createWebviewPanel('modelbuilder.newProject','New Model Project',vscode.ViewColumn.One,{enableScripts:true,retainContextWhenHidden:true,localResourceRoots:[vscode.Uri.joinPath(this.context.extensionUri,'media')]});
      this.panel=panel;panel.webview.html=newProjectHtml(panel.webview,this.context.extensionUri);
      panel.onDidDispose(()=>{if(this.panel===panel)this.panel=undefined;});
      panel.webview.onDidReceiveMessage(message=>void this.handle(message,panel));
    }finally{this.opening=false;}
  }
  private async post(error?:string){if(this.panel&&this.session)await this.panel.webview.postMessage({type:'state',...this.session.snapshot(),folder:this.folder?.fsPath,source:this.source,harnesses:HARNESS_CAPABILITIES,nativeBusy:this.nativeBusy,baseModelAvailable:!!this.hooks.downloadBaseModel,sourceRegistrationAvailable:!!this.hooks.registerSource,scaffoldReady:this.scaffoldReady,scaffoldPaths:SCAFFOLD_PATHS,error});}
  private async handle(message:unknown,panel:vscode.WebviewPanel){
    if(this.panel!==panel||!this.session)return;
    const session=this.session;
    try{
      if(!message||typeof message!=='object'||Array.isArray(message)||JSON.stringify(message).length>12000)throw new Error('Invalid New Project request.');
      const m=message as RecordValue;
      if(Object.keys(m).some(key=>!['type','revision','draft'].includes(key))||!['ready','review','back','cancel','create','chooseFolder','bindSource','setupAssistant','open','downloadBaseModel','registerSource'].includes(m.type))throw new Error('Unsupported New Project request.');
      if(m.type==='ready'){await this.post();return;}
      if(!Number.isSafeInteger(m.revision)||m.revision!==session.revision)throw new Error('This New Project page changed. Review its current state.');
      if(this.nativeBusy)throw new Error('Finish or cancel the open native dialog first.');
      const current=()=>{if(this.panel!==panel||this.session!==session||session.revision!==m.revision)throw new Error('The New Project page changed or closed. Review its current state before continuing.');};
      switch(m.type){
        case 'review':if(!this.folder)throw new Error('Choose a local project folder for the versioned project structure.');this.nativeBusy=true;await this.post();await checkScaffoldTarget(this.folder.fsPath);current();session.edit(m.draft,m.revision);session.review(this.source,session.revision);break;
        case 'back':session.back(m.revision);break;
        case 'cancel':session.cancel(m.revision);panel.dispose();return;
        case 'chooseFolder':{
          if(session.stage!=='details')throw new Error('Go back to project details to choose a folder.');
          this.nativeBusy=true;await this.post();const selected=await vscode.window.showOpenDialog({title:'Choose this model project’s local folder',canSelectFiles:false,canSelectFolders:true,canSelectMany:false,openLabel:'Use project folder'});current();
          if(selected?.[0]){if(selected[0].scheme!=='file')throw new Error('Choose a local project folder.');this.folder=selected[0];this.source=undefined;session.revision++;}break;
        }
        case 'bindSource':{
          if(session.stage!=='details')throw new Error('Go back to project details to bind a source.');
          this.nativeBusy=true;await this.post();const choice=await chooseLocalSource(this.client);current();if(!choice)break;
          const approved=await acknowledgeSource(choice);current();if(approved){this.folder=choice.folder;this.source={source_alias:choice.source.alias,source_repository_id:choice.source.repository_id,trusted:true};session.revision++;}break;
        }
        case 'create':{
          const selectedFolder=this.folder;
          if(!selectedFolder)throw new Error('Choose the project folder before creating it.');
          const promise=session.create(this.client,m.revision,request=>this.recovery.save(selectedFolder.fsPath,request));await this.post();const project=await promise;
          if(!selectedFolder)throw new Error('The project was created, but its selected local folder is unavailable.');
          const recipe=await this.client.invoke<Recipe>('recipe.get',{project_id:project.project_id,recipe_id:project.recipe_id});
          const scaffold=await createProjectScaffold(selectedFolder.fsPath,project,recipe);if(scaffold.status==='drift')throw new Error(`Local structure has changed or is missing files: ${scaffold.drift.join(', ')}. Existing edits are preserved.`);if(this.session===session)this.scaffoldReady=true;
          const folders=this.context.globalState.get<Record<string,string>>('modelbuilder.projectFolders',{});await this.context.globalState.update('modelbuilder.projectFolders',{...folders,[project.project_id]:selectedFolder.fsPath});await this.recovery.clear(selectedFolder.fsPath);
          // Closing while HTTP is in flight cannot retract an accepted creation.
          if(this.panel!==panel){void vscode.window.showInformationMessage(`Created ${project.model_name} with one recipe and its local structure. Open it from Model Explorer.`);return;}break;
        }
        case 'registerSource':{
          if(!session.project||!this.folder||!this.hooks.registerSource)throw new Error('Managed source registration is unavailable. Open the project to author code; execution remains blocked until source is explicitly registered.');
          this.nativeBusy=true;await this.post();const result=await registerProjectSource(this.client,session.project,this.folder,this.hooks.registerSource,current);if(result){session.project=result.project;session.revision++;}break;
        }
        case 'setupAssistant':{
          if(!session.project)throw new Error('Create the project before setting up its project-bound assistant.');
          if(session.draft.assistant==='none')throw new Error('No optional assistant was selected.');
          if(!this.folder)throw new Error('No local project folder was selected. Open the model project and choose its folder in assistant setup.');
          this.nativeBusy=true;await this.post();await configureOptionalAssistant(this.context,undefined,{...this.hooks.assistantOptions?.(session.project),client:session.draft.assistant,workspace:this.folder,projectId:session.project.project_id});break;
        }
        case 'open':{
          if(!this.scaffoldReady)throw new Error('Finish creating the local project structure before opening it.');
          if(!session.project)throw new Error('Create the model project first.');if(this.opened)return;this.opened=true;
          try{await this.hooks.onCreated(session.project,this.folder,session.draft.quickstart?session.draft.startingPoint:'blank');panel.dispose();return;}catch(error){this.opened=false;throw error;}
        }
        case 'downloadBaseModel':{
          if(!session.project||!this.hooks.downloadBaseModel)throw new Error('The reviewed base-model download flow is unavailable in this build. The empty recipe remains usable for from-scratch work.');
          this.nativeBusy=true;await this.post();await this.hooks.downloadBaseModel(session.project,current);break;
        }
      }
      if(this.session===session&&this.panel===panel){this.nativeBusy=false;await this.post();}
    }catch(error){if(this.session===session&&this.panel===panel){this.nativeBusy=false;await this.post(error instanceof Error?error.message:'New Project could not complete.');}else if(session.approvedRequest()){void vscode.window.showWarningMessage('Project creation still needs attention. Reopen New Project to resume the approved request without creating a duplicate.');}}
  }
  dispose(){this.panel?.dispose();}
}
export function newProjectHtml(webview:vscode.Webview,extension:vscode.Uri):string{
  const nonce=randomBytes(18).toString('base64');const resource=(name:string)=>escapeHtml(webview.asWebviewUri(vscode.Uri.joinPath(extension,'media',name)).toString());
  return `<!doctype html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';"><link rel="stylesheet" href="${resource('newProject.css')}"></head><body><main><p class="eyebrow">MODELBUILDER · NEW PROJECT</p><h1>What do you want to build?</h1><p class="intro">One model project. One evolving recipe. Choose a starting point, then make it your own.</p><div id="error" role="alert" hidden></div><section id="details"><label>Model name<input id="modelName" maxlength="200" autocomplete="off" placeholder="My model"></label><label>Project slug<input id="slug" maxlength="80" autocomplete="off" placeholder="my-model"></label><label class="toggle"><input id="quickstart" type="checkbox" checked>Use optional quickstart</label><div id="guided"><label>What should your model do?<textarea id="goal" maxlength="8000" rows="3" placeholder="Describe the goal, constraints, and how you’ll know it works. Do not enter credentials."></textarea></label><fieldset><legend>Where would you like to begin?</legend><label class="choice"><input type="radio" name="start" value="research">Research first<span>Gather evidence and plan a reviewed implementation.</span></label><label class="choice"><input type="radio" name="start" value="base_model">Start with a base model<span>Choose an optional download after creation. Nothing downloads now.</span></label><label class="choice"><input type="radio" name="start" value="scratch" checked>Build from scratch<span>Write architecture and training code using the native editor.</span></label></fieldset><label>Optional coding assistant<select id="assistant"><option value="none">No assistant</option><option value="codex">Codex</option><option value="claude">Claude Code</option><option value="triggerfish">Triggerfish · compatibility pending</option></select></label><p id="assistantNote" class="muted"></p></div><label>Project folder<span id="folder" class="muted">Choose a folder for the versioned project structure. Source binding and execution stay separate.</span></label><div class="actions"><button data-action="chooseFolder" class="secondary">Choose local folder</button><button data-action="bindSource" class="secondary">Bind approved source</button></div><p id="source" class="muted"></p></section><section id="review" hidden><h2>Review your project</h2><dl id="summary"></dl><p>The reviewed folder receives a versioned project descriptor, source/tests folders, recipe/revision JSON snapshots, research notes and immutable artifact-manifest schema. The native app also projects later reviewed canonical project/recipe revisions here; receipt hashes stop projection if local files change. Existing files are preserved; .gitignore gains a reviewed block excluding large data, weights, checkpoints, logs and caches. No Git repository or remote is created.</p><p>Creates one model project with one non-executable draft recipe. No model download, training, vendor sign-in or paid call occurs. You can develop the recipe from any starting point.</p></section><section id="created" hidden><h2>Your model project is ready</h2><p id="createdDescription"></p><p>The recipe is a draft. Add and validate real source, datasets and components before execution.</p><div class="actions"><button data-action="registerSource" id="registerSource" class="secondary">Enable trusted local source</button><button data-action="setupAssistant" id="setup" class="secondary">Set up selected assistant</button><button data-action="downloadBaseModel" id="download" class="secondary">Choose optional base model</button></div><p id="capability" class="muted"></p></section><footer id="controls"></footer><p class="footnote">Assistant installation, sign-in and working MCP transport are separate checks. Triggerfish is a capability interface only until compatibility is accepted.</p></main><script nonce="${nonce}" src="${resource('newProject.js')}"></script></body></html>`;
}
