import * as vscode from 'vscode';
import {HubSourceControl} from './hubRepository';
import {HubReview,openHubWorkflow,publishStagedHubFiles} from './hubWorkflow';
import {RecordValue,ServiceTransport} from './types';

/** Host lifecycle: an SCM instance and its staged state belong to one project/connection. */
export class HubController implements vscode.Disposable {
  private scm?:HubSourceControl;
  private view?:vscode.Disposable;
  private projectId?:string;
  private connection?:string;
  private generation=0;
  private disposed=false;
  constructor(private readonly client:ServiceTransport,private readonly endpoint:()=>string,private readonly review?:HubReview){}
  setProject(projectId?:string):void{
    if(this.disposed)return;
    const connection=this.endpoint();
    if(projectId===this.projectId&&connection===this.connection)return;
    this.generation++;this.view?.dispose();this.view=undefined;this.scm?.dispose();this.scm=undefined;
    this.projectId=projectId;this.connection=connection;
    if(projectId){this.scm=new HubSourceControl(projectId);this.view=vscode.window.registerTreeDataProvider('modelbuilder.hubRepository',this.scm);}
  }
  connectionChanged():void{this.setProject(undefined);}
  private guard():{projectId:string;check:()=>void}{
    if(this.disposed||!this.projectId||!this.scm)throw new Error('Open a model project before using its Hub repository.');
    const projectId=this.projectId,generation=this.generation,connection=this.connection;
    const check=()=>{if(this.disposed||generation!==this.generation||projectId!==this.projectId||connection!==this.endpoint())throw new Error('The model project or service connection changed; this Hub action was stopped.');};check();
    return {projectId,check};
  }
  async open():Promise<RecordValue|undefined>{
    const {projectId,check}=this.guard();
    const result=await openHubWorkflow(this.client,projectId,check,this.review);check();
    if(result?.changes)this.scm!.setComparison(result);
    return result;
  }
  stage(path:string,projectId:string,commit:string,unstage=false):void{
    const current=this.guard();current.check();
    if(projectId!==current.projectId||commit!==this.scm!.repositoryIdentity()?.commit)throw new Error('This Hub file belongs to a stale project or repository revision. Inspect again.');
    if(unstage)this.scm!.unstage(path);else this.scm!.stage(path);
  }
  async publish():Promise<RecordValue|undefined>{
    const {projectId,check}=this.guard();
    const result=await publishStagedHubFiles(this.client,projectId,this.scm!.stagedSelection(),check,this.review);check();
    if(result?.state==='succeeded')this.scm!.clear();
    return result;
  }
  dispose():void{if(this.disposed)return;this.setProject(undefined);this.disposed=true;this.generation++;}
}
