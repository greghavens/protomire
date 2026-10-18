/** Notification is not consent: the existing reviewed setup owns every write. */
export const PROJECT_MCP_NOTICE='Enable ModelBuilder tools for assistants in this project? Approved project setup lets supported assistants start the bundled MCP service even when the IDE is closed. Access is project-scoped; changes require separate review. Disconnect through Set Up Optional Coding Tool. Platform compatibility must be verified.';
export interface NoticeState {get<T>(key:string):T|undefined;update(key:string,value:unknown):PromiseLike<void>}
export async function offerProjectAssistantSetup(projectId:string,state:NoticeState,ask:(message:string,...choices:string[])=>PromiseLike<string|undefined>,configure:()=>PromiseLike<unknown>,current:()=>boolean):Promise<void>{
  const key=`modelbuilder.assistantNotice.v1.${projectId}`;
  if(!current()||state.get<boolean>(key))return;
  // Persist before showing so refreshes cannot produce duplicate prompts.
  await state.update(key,true);
  if(!current())return;
  const choice=await ask(PROJECT_MCP_NOTICE,'Set up assistant','Not now');
  if(choice==='Set up assistant'&&current())await configure();
}
