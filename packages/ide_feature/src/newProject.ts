import * as vscode from 'vscode';
import { randomUUID, randomBytes } from 'node:crypto';
import { Project, Recipe, RecordValue, ServiceTransport } from './types';
import { ProjectQuickstartSession, HARNESS_CAPABILITIES, StartingPoint, validateProjectDraftFields } from './projectQuickstart';
import { acknowledgeSource, chooseLocalSource } from './sourceWorkspace';
import { configureOptionalAssistant, AssistantSetupOptions } from './assistantSetup';
import { escapeHtml } from './security';
import { ProjectDraftRecovery } from './projectDraftRecovery';
import { ProjectWizardDraft, UnreadableWizardDraftError, WizardDraft } from './projectWizardDraft';
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
  private drafts:ProjectWizardDraft;
  private wizardStep='assistant';
  private ownedDrafts=new WeakMap<ProjectQuickstartSession,WizardDraft|string>();
  private draftIds=new WeakMap<ProjectQuickstartSession,string>();
  constructor(private context:vscode.ExtensionContext,private client:ServiceTransport,private hooks:NewProjectHooks){this.recovery=new ProjectDraftRecovery(context);this.drafts=new ProjectWizardDraft(context.secrets);}
  async open(current:()=>boolean=()=>true):Promise<boolean>{
    if(this.panel){this.panel.reveal();return true;}if(this.opening||!current())return false;this.opening=true;
    try{
      await this.drafts.whenIdle();if(!current())return false;
      const pending=await this.recovery.list();if(!current())return false;
      const resumeChoice=pending.length?await vscode.window.showQuickPick([...pending.map(item=>({label:item.folder,description:'Resume this approved project creation',pending:item})),{label:'Create a different model project',description:'Keeps pending requests available for later recovery',pending:undefined}],{title:'New Project or resume approved creation'}):undefined;
      if(!current()||(pending.length&&!resumeChoice))return false;const selected=resumeChoice?.pending;
      const saved=selected?undefined:await this.recoverDraft(current);if(saved===false||!current())return false;
      this.session=selected?ProjectQuickstartSession.restore(selected.request):new ProjectQuickstartSession(randomUUID());this.folder=selected?vscode.Uri.file(selected.folder):undefined;this.source=selected?.request.source;this.opened=false;this.scaffoldReady=false;this.nativeBusy=false;
      this.draftIds.set(this.session,saved?.draft_id??randomUUID());if(selected?.origin_draft_sha256)this.ownedDrafts.set(this.session,selected.origin_draft_sha256);
      this.wizardStep=saved?.step??'assistant';if(saved){this.session.draft=saved.draft;this.ownedDrafts.set(this.session,saved);this.folder=saved.folder?vscode.Uri.file(saved.folder):undefined;this.source=saved.source;}
      const panel=vscode.window.createWebviewPanel('modelbuilder.newProject','New Model Project',vscode.ViewColumn.One,{enableScripts:true,retainContextWhenHidden:true,localResourceRoots:[vscode.Uri.joinPath(this.context.extensionUri,'media')]});
      this.panel=panel;panel.webview.html=newProjectHtml(panel.webview,this.context.extensionUri);
      panel.onDidDispose(()=>{if(this.panel===panel)this.panel=undefined;});
      panel.webview.onDidReceiveMessage(message=>void this.handle(message,panel));
      return true;
    }finally{this.opening=false;}
  }
  private async post(error?:string){if(this.panel&&this.session)await this.panel.webview.postMessage({type:'state',...this.session.snapshot(),wizardStep:this.wizardStep,folder:this.folder?.fsPath,source:this.source,harnesses:HARNESS_CAPABILITIES,nativeBusy:this.nativeBusy,baseModelAvailable:!!this.hooks.downloadBaseModel,sourceRegistrationAvailable:!!this.hooks.registerSource,scaffoldReady:this.scaffoldReady,scaffoldPaths:SCAFFOLD_PATHS,error});}
  private async handle(message:unknown,panel:vscode.WebviewPanel){
    if(this.panel!==panel||!this.session)return;
    const session=this.session;
    try{
      if(!message||typeof message!=='object'||Array.isArray(message)||JSON.stringify(message).length>65536)throw new Error('Invalid New Project request.');
      const m=message as RecordValue;
      if(Object.keys(m).some(key=>!['type','revision','draft','step'].includes(key))||!['draftChanged','ready','review','back','cancel','create','chooseFolder','bindSource','setupAssistant','open','downloadBaseModel','registerSource'].includes(m.type))throw new Error('Unsupported New Project request.');
      if(m.type==='ready'){await this.post();return;}
      if(!Number.isSafeInteger(m.revision)||m.revision!==session.revision)throw new Error('This New Project page changed. Review its current state.');
      if(m.type==='draftChanged'){if(session.stage!=='details')return;const draft=validateProjectDraftFields(m.draft);if(!['assistant','goal','start','project'].includes(m.step))throw new Error('Invalid wizard screen.');session.draft=draft;this.wizardStep=m.step;await this.saveDraft();return;}
      if(this.nativeBusy)throw new Error('Finish or cancel the open native dialog first.');
      const current=()=>{if(this.panel!==panel||this.session!==session||session.revision!==m.revision)throw new Error('The New Project page changed or closed. Review its current state before continuing.');};
      switch(m.type){
        case 'review':if(!this.folder)throw new Error('Choose a local project folder for the versioned project structure.');this.nativeBusy=true;await this.post();await checkScaffoldTarget(this.folder.fsPath);current();session.edit(m.draft,m.revision);session.review(this.source,session.revision);break;
        case 'back':session.back(m.revision);break;
        case 'cancel':session.cancel(m.revision);panel.dispose();return;
        case 'chooseFolder':{
          if(session.stage!=='details')throw new Error('Go back to project details to choose a folder.');
          this.nativeBusy=true;await this.post();const selected=await vscode.window.showOpenDialog({title:'Choose this model project’s local folder',canSelectFiles:false,canSelectFolders:true,canSelectMany:false,openLabel:'Use project folder'});current();
          if(selected?.[0]){if(selected[0].scheme!=='file')throw new Error('Choose a local project folder.');this.folder=selected[0];this.source=undefined;session.revision++;await this.saveDraft();}break;
        }
        case 'bindSource':{
          if(session.stage!=='details')throw new Error('Go back to project details to bind a source.');
          this.nativeBusy=true;await this.post();const choice=await chooseLocalSource(this.client);current();if(!choice)break;
          const approved=await acknowledgeSource(choice);current();if(approved){this.folder=choice.folder;this.source={source_alias:choice.source.alias,source_repository_id:choice.source.repository_id,trusted:true};session.revision++;await this.saveDraft();}break;
        }
        case 'create':{
          const selectedFolder=this.folder;
          if(!selectedFolder)throw new Error('Choose the project folder before creating it.');
          const promise=session.create(this.client,m.revision,request=>this.drafts.approve(async origin=>{await this.recovery.save(selectedFolder.fsPath,request,origin);},()=>this.ownedDrafts.get(session)));await this.post();const project=await promise;
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
  private async recoverDraft(current:()=>boolean):Promise<WizardDraft|undefined|false>{
    while(current()){
      try{return await this.drafts.load();}
      catch(error){
        if(!current())return false;
        if(error instanceof UnreadableWizardDraftError){
          const choice=await vscode.window.showWarningMessage('The encrypted unfinished project draft is unreadable. Retry keeps it intact. Discard deletes only that unfinished draft; approved creation recovery is preserved.',{modal:true},'Retry','Discard unfinished draft');
          if(!current())return false;
          if(choice==='Retry')continue;
          if(choice!=='Discard unfinished draft')return false;
          await this.drafts.discardUnreadable(error);return undefined;
        }
        const choice=await vscode.window.showErrorMessage('Encrypted project draft storage is unavailable. Saved data has not been deleted. Retry when local storage access returns.',{modal:true},'Retry');
        if(choice!=='Retry'||!current())return false;
      }
    }
    return false;
  }
  private async saveDraft(){if(!this.session)return;const session=this.session,record:WizardDraft={schema_version:'1',draft_id:this.draftIds.get(session),draft:session.draft,step:this.wizardStep,...(this.folder?{folder:this.folder.fsPath}:{}),...(this.source?{source:this.source}:{})};await this.drafts.save(record);this.ownedDrafts.set(session,structuredClone(record));}
  dispose(){this.panel?.dispose();}
}
export function newProjectHtml(webview:vscode.Webview,extension:vscode.Uri):string{
  const nonce=randomBytes(18).toString('base64');const resource=(name:string)=>escapeHtml(webview.asWebviewUri(vscode.Uri.joinPath(extension,'media',name)).toString());
  return `<!doctype html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';"><link rel="stylesheet" href="${resource('newProject.css')}"></head><body><main><p class="eyebrow">MODELBUILDER · NEW PROJECT</p><p id="progress" aria-live="polite">Step 1 of 5</p><h1 id="stepTitle" tabindex="-1">Choose your coding assistant</h1><p id="stepIntro" class="intro">Choose how you want to work. Setup and sign-in happen separately after creation.</p><div id="error" role="alert" hidden></div>
<section id="details">
<section data-step="assistant"><label class="toggle"><input id="quickstart" type="checkbox" checked>Use guided setup</label><div id="guided"><label>Optional coding assistant<select id="assistant"><option value="none">No assistant</option><option value="codex">Codex</option><option value="claude">Claude Code</option><option value="triggerfish">Triggerfish · compatibility pending</option></select></label><p id="assistantNote" class="muted"></p></div><p class="muted">You can also skip guidance and create a blank project. No assistant is installed or activated by choosing it here.</p></section>
<section data-step="goal" hidden><label>Model goal<textarea id="goal" maxlength="8000" rows="6" placeholder="Describe what your model should do, its constraints, and how you will evaluate it. Do not enter credentials."></textarea></label></section>
<section data-step="start" hidden><fieldset><legend>Choose your starting point</legend><label class="choice"><input type="radio" name="start" value="research">Research first<span>Gather evidence and plan a reviewed implementation.</span></label><label class="choice"><input type="radio" name="start" value="base_model">Start with an existing base model<span>Choose an optional base model after creation. Nothing downloads now.</span></label><label class="choice"><input type="radio" name="start" value="scratch" checked>Build from scratch<span>Write architecture and training code using the native editor.</span></label></fieldset></section>
<section data-step="project" hidden><label>Model name<input id="modelName" maxlength="200" autocomplete="off" placeholder="My model"></label><label>Project slug<input id="slug" maxlength="80" autocomplete="off" placeholder="my-model"></label><label>Project folder<span id="folder" class="muted">Choose a local folder for the versioned project structure. Source binding and execution stay separate.</span></label><div class="actions"><button data-action="chooseFolder" class="secondary">Choose local folder</button><button data-action="bindSource" class="secondary">Bind approved source</button></div><p id="source" class="muted"></p></section>
</section><section id="review" hidden><h2>Confirm your choices</h2><dl id="summary"></dl><p>The reviewed folder receives a versioned project descriptor, source/tests folders, recipe/revision JSON snapshots, research notes and immutable artifact-manifest schema. Existing files are preserved; .gitignore gains a reviewed block excluding large data, weights, checkpoints, logs and caches. No Git repository or remote is created.</p><p>Creates one model project with one non-executable draft recipe. No model download, training, vendor sign-in or paid call occurs.</p></section><section id="created" hidden><p id="createdDescription"></p><p>The recipe is a draft. Add and validate real source, datasets and components before execution.</p><div class="actions"><button data-action="registerSource" id="registerSource" class="secondary">Enable trusted local source</button><button data-action="setupAssistant" id="setup" class="secondary">Set up selected assistant</button><button data-action="downloadBaseModel" id="download" class="secondary">Choose optional base model</button></div><p id="capability" class="muted"></p></section><footer id="controls"></footer><p class="footnote">Unfinished wizard choices are saved in encrypted local storage. Assistant installation, sign-in and working MCP transport are separate checks. Triggerfish compatibility remains pending.</p></main><script nonce="${nonce}" src="${resource('projectWizard.js')}"></script><script nonce="${nonce}" src="${resource('newProject.js')}"></script></body></html>`;
}
