import { Recipe, RecordValue } from './types';
import { addNode, replaceNode } from './document';

const descriptorKeys=['id','version','protocol','role','description','parameter_schema','capabilities'];
/** A library is an inert declaration. Source pins always come from the user's project. */
export function addLibraryComponent(recipe:Recipe,entry:RecordValue,id:string,recipeId:string,revision:number):Recipe {
  if(recipe.recipe_id!==recipeId||recipe.revision!==revision)throw new Error('The recipe changed after loading this catalog. Reload reusable components; your draft is preserved.');
  if(entry.protocol!=='modelbuilder.component/v1'||!entry.adapter||!Array.isArray(entry.inputs)||!Array.isArray(entry.outputs))throw new Error('This entry is not a reusable source-bound component.');
  const next=addNode(recipe,id,'component.run'),node=next.nodes[next.nodes.length-1];
  return replaceNode(next,{...node,component:Object.fromEntries(descriptorKeys.filter(key=>key in entry).map(key=>[key,structuredClone(entry[key])])),
    inputs:structuredClone(entry.inputs),outputs:structuredClone(entry.outputs),
    provenance:{library_component:entry.id,library_version:entry.version,library_component_digest:entry.component_digest}});
}

export function renderComponentLibrary(root:HTMLElement,result:RecordValue,onAdd:(entry:RecordValue,id:string)=>void):void {
  const doc=root.ownerDocument;root.replaceChildren();root.hidden=false;
  const heading=doc.createElement('h3');heading.textContent='Reusable local components';root.append(heading);
  const help=doc.createElement('p');help.textContent='Add a declaration to this recipe draft, then select a pinned wrapper in the node panel and configure local dependency hashes. This does not save, execute, download a model, register generated data, or start training. Judge outputs are model predictions, not ground truth.';root.append(help);
  for(const entry of (result.components??[]).filter((item:RecordValue)=>item.protocol==='modelbuilder.component/v1'&&item.adapter)){
    const section=doc.createElement('section'),title=doc.createElement('h4'),description=doc.createElement('p');title.textContent=`${entry.id} · ${entry.version}`;description.textContent=entry.description??'';section.append(title,description);
    const details=doc.createElement('details'),summary=doc.createElement('summary'),body=doc.createElement('pre');summary.textContent='Parameters, ports and source binding';body.textContent=JSON.stringify({adapter:entry.adapter,inputs:entry.inputs,outputs:entry.outputs,parameter_schema:entry.parameter_schema},null,2);details.append(summary,body);section.append(details);
    const label=doc.createElement('label');label.textContent='New node ID';const input=doc.createElement('input');input.setAttribute('aria-label',`New node ID for ${entry.id}`);label.append(input);section.append(label);
    const button=doc.createElement('button');button.type='button';button.textContent='Add declaration to draft';button.addEventListener('click',()=>onAdd(entry,input.value.trim()));section.append(button);root.append(section);
  }
}
