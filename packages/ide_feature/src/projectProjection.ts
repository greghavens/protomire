import { Project, Recipe, RecordValue, ServiceTransport } from './types';
import { syncProjectScaffold } from './projectScaffold';
/** Add local projection to the existing client instance without another service
 * dispatcher. Failed projection never changes success of an accepted mutation.
 * The resolver must return only folders explicitly chosen in native New Project.
 */
export function attachProjectProjection(client:ServiceTransport,folderFor:(projectId:string)=>string|undefined,notify:(message:string)=>void):()=>void{
  const invoke=client.invoke.bind(client),catalog=client.catalog.bind(client);let specs:Promise<Set<string>>|undefined;const queue=new Map<string,Promise<void>>();let active=true;
  async function project(projectId:string,snapshot?:RecordValue){
    const folder=folderFor(projectId);if(!folder||!active)return;
    let state:RecordValue|undefined=snapshot;
    for(let attempt=0;attempt<3;attempt++){
      state??=await invoke<RecordValue>('project.open',{project_id:projectId});
      if(!state?.project||!state?.recipe)throw new Error('Missing canonical project snapshot.');
      const history=await invoke<RecordValue>('recipe.history',{project_id:projectId});
      const latest=await invoke<RecordValue>('project.open',{project_id:projectId});
      if(history.current_revision!==state.recipe.revision||history.recipe_id!==state.recipe.recipe_id||JSON.stringify(latest.project)!==JSON.stringify(state.project)||latest.recipe?.revision!==state.recipe.revision||latest.recipe?.digest!==state.recipe.digest){state=latest;continue;}
      const result=await syncProjectScaffold(folder,state.project as Project,state.recipe as Recipe,history.revisions as Recipe[]);
      if(result.status==='drift')notify(`Project files were preserved because they differ from their last canonical projection: ${result.drift.join(', ')}. The service edit is saved; resolve local drift before projection continues.`);
      return;
    }
    throw new Error('Project or recipe changed during projection review. Existing local files were preserved; refresh when the current edit finishes.');
  }
  client.invoke=async<T=RecordValue>(operation:string,payload:RecordValue):Promise<T>=>{
    const result=await invoke<T>(operation,payload);if(!active)return result;
    try{specs??=catalog().then(list=>new Set(list.filter(item=>item.mutating).map(item=>item.name)));const mutating=(await specs).has(operation);const state=result as RecordValue;const projectId=payload.project_id??state.project_id??state.project?.project_id;
      if(typeof projectId==='string'&&folderFor(projectId)&&(mutating||operation==='project.open')){const pending=(queue.get(projectId)??Promise.resolve()).then(()=>project(projectId,operation==='project.open'?state:undefined)).catch(error=>notify(`Canonical project files could not be projected: ${error instanceof Error?error.message:'unknown local error'}. The service result is preserved.`));queue.set(projectId,pending);await pending;if(queue.get(projectId)===pending)queue.delete(projectId);}
    }catch{specs=undefined;notify('The service result is saved, but project projection capabilities could not be checked. Refresh to retry.');}
    return result;
  };
  return ()=>{active=false;client.invoke=invoke;};
}
