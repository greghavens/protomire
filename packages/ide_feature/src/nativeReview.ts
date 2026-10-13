/** Native-only review. Neither webview messages nor MCP flags approve a request. */
import * as vscode from 'vscode';
import { ManagedCore, NativeReview } from './managedCore';

export function reviewSummary(request:NativeReview):string {
  const action=request.operation==='desktop.publish.reviewed'?'Publish the complete frozen Hub snapshot':request.operation;
  const ongoing=request.required_scopes.includes('execute')?'Already submitted jobs are not undone by closing this review.':'';
  return `${request.project_name}\nRequested by: ${request.requested_by}\nAction: ${action}\nExact request: ${request.id}\nPayload SHA256: ${request.payload_digest}\nAuthorization lasts at most ${Math.round(request.action_ttl_seconds/60)} minutes for this action only.\n${ongoing}`;
}
export class NativeReviewController implements vscode.Disposable {
  private readonly seen=new Set<string>();
  private readonly command:vscode.Disposable;
  private readonly timer:NodeJS.Timeout;
  private disposed=false;
  private busy=false;
  private reading=false;
  constructor(private readonly core:Pick<ManagedCore,'url'|'pendingReviews'|'decideReview'>){
    this.command=vscode.commands.registerCommand('modelbuilder.reviewAgentRequests',()=>this.choose());
    this.timer=setInterval(()=>void this.poll(),1500);
  }
  dispose(){this.disposed=true;clearInterval(this.timer);this.command.dispose();}
  async poll(){
    if(this.disposed||this.reading||this.busy||!this.core.url)return;
    this.reading=true;
    try{
      const pending=(await this.core.pendingReviews()).find(request=>request.status==='pending'&&request.requested_by!=='desktop-owner'&&!this.seen.has(request.id));
      if(!pending||this.disposed)return;this.seen.add(pending.id);
      const selected=await vscode.window.showInformationMessage(`${pending.project_name}: an assistant requests ${pending.operation}.`,'Review request');
      if(selected==='Review request'&&!this.disposed)await this.review(pending);
    }catch{/* A disconnected local session does not trigger repeated error toasts. */}
    finally{this.reading=false;}
  }
  async choose(){
    if(this.busy||this.disposed)return;
    const requests=await this.core.pendingReviews();if(this.disposed)return;
    const picked=await vscode.window.showQuickPick(requests.filter(item=>['pending','executing','active','revoking'].includes(item.status)).map(request=>({label:`${request.project_name}: ${request.operation}`,description:request.status,request})),{title:'Agent requests',placeHolder:'Review one exact action or revoke the remaining authority for an active transfer.'});
    if(picked&&!this.disposed)await this.review(picked.request);
  }
  async review(input:NativeReview,beforeDecision:()=>void=()=>{}):Promise<NativeReview|undefined>{
    if(this.busy||this.disposed)return;this.busy=true;
    try{
      const request=structuredClone(input);
      if(request.status==='active'||request.status==='executing'||request.status==='revoking'){
        const confirmation=await vscode.window.showWarningMessage('Revoke remaining authority for this exact transfer?',{modal:true,detail:reviewSummary(request)+'\nAn in-flight publication may require reconciliation. This does not undo already transmitted data or cancel submitted native jobs.'},'Revoke remaining authority');
        if(confirmation==='Revoke remaining authority'&&!this.disposed){beforeDecision();const result=await this.core.decideReview(request,'revoke');void vscode.window.showInformationMessage(result.notice??'Revocation requested. Inspect the transfer result.');return result;}return;
      }
      if(request.status!=='pending')return;
      const review=await vscode.workspace.openTextDocument({language:'json',content:JSON.stringify({request_id:request.id,project_id:request.project_id,operation:request.operation,requested_by:request.requested_by,payload:request.payload,service_disclosure:request.disclosure,review_digest:request.review_digest},null,2)});
      const originalVersion=review.version;
      await vscode.window.showTextDocument(review,{preview:true});if(this.disposed)return;
      const label=request.operation==='desktop.publish.reviewed'?'Publish these exact files':'Execute this exact request';
      const selection=await vscode.window.showWarningMessage('Approve the exact request shown in the review?',{modal:true,detail:reviewSummary(request)+'\nApproval comes only from this native button. Editing the displayed JSON does not edit the request.'},label,'Deny request');
      if(this.disposed||selection===undefined)return;
      if(review.version!==originalVersion)throw new Error('The displayed review was edited. Close it and reopen the original request before deciding.');
      const latest=(await this.core.pendingReviews()).find(item=>item.id===request.id);
      if(this.disposed)return;
      if(!latest||latest.status!=='pending'||latest.review_digest!==request.review_digest)throw new Error('The request changed or expired while its review was open.');
      beforeDecision();
      const result=await this.core.decideReview(request,selection===label?'approve':'deny');
      if(['failed','indeterminate','stale','expired'].includes(result.status))throw new Error(result.error?.message??result.notice??'This request did not complete. Inspect its exact result before retrying.');
      void vscode.window.showInformationMessage(result.status==='active'?'The approved transfer started. Its exact authority remains revocable.':result.status==='denied'?'Request denied.':'The exact reviewed request completed.');
      return result;
    }catch(error){if(!this.disposed)void vscode.window.showErrorMessage(error instanceof Error?error.message:'Native request review failed.');}
    finally{this.busy=false;}
  }
}
