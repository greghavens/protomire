/** A bundled app's first empty session uses the existing New Project view. */
export const FIRST_RUN_KEY='modelbuilder.firstRunQuickstartShown';
export interface FirstRunState {
  get<T>(key:string,defaultValue:T):T;
  update(key:string,value:unknown):PromiseLike<void>;
}
export interface FirstRunSession {folders:number;workspaceFile:boolean;editors:number;pendingProject:boolean;projectOpen:boolean}
export const emptyFirstRunSession=(s:FirstRunSession):boolean=>s.folders===0&&!s.workspaceFile&&s.editors===0&&!s.pendingProject&&!s.projectOpen;
export class FirstRunQuickstart {
  private opening=false;
  constructor(private state:FirstRunState,private bundled:boolean,private emptySession:()=>boolean,private open:(current:()=>boolean)=>Promise<boolean>){}
  async show():Promise<boolean>{
    const current=()=>this.bundled&&!this.state.get(FIRST_RUN_KEY,false)&&this.emptySession();
    if(this.opening||!current())return false;
    this.opening=true;
    try{
      // The view checks again after recovery awaits: newer navigation wins.
      if(!await this.open(current))return false;
      // Opening, not completion, consumes first show. Cancel/Close stays closed.
      await this.state.update(FIRST_RUN_KEY,true);
      return true;
    }finally{this.opening=false;}
  }
}
