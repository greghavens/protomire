import {Recipe,RecipeNode,RecordValue} from './types';
/** Editor projections of the service-owned typed branch-condition contract. */
export function conditionChoices(recipe:Recipe,nodeId:string):{value:string;label:string}[]{
  const downstream=new Set<string>([nodeId]);
  const edges=[...recipe.edges,...conditionEdges(recipe)];
  let changed=true;while(changed){changed=false;for(const edge of edges)if(downstream.has(edge.source_node)&&!downstream.has(edge.target_node)){downstream.add(edge.target_node);changed=true;}}
  return [{value:'',label:'Always run'},...recipe.nodes.filter(n=>!downstream.has(n.id)).flatMap(n=>(n.outputs??[]).filter(p=>p.type==='bool').map(p=>({value:JSON.stringify([n.id,p.name]),label:`${n.id}.${p.name} (bool)`})))];
}
export function conditionEdges(recipe:Recipe):{source_node:string;source_port:string;target_node:string;equals:boolean}[]{
  return recipe.nodes.filter(n=>n.condition&&typeof n.condition.source_node==='string'&&typeof n.condition.source_port==='string'&&typeof n.condition.equals==='boolean').map(n=>({...n.condition!,target_node:n.id}));
}
export function conditionSelection(node:RecipeNode):string{return node.condition?JSON.stringify([node.condition.source_node,node.condition.source_port]):'';}
export function withTerminalCondition(recipe:Recipe,node:RecipeNode,selection:string,expected:string):RecipeNode{
  const updated={...node};
  if(!selection){delete updated.condition;return updated;}
  if(!conditionChoices(recipe,node.id).some(choice=>choice.value===selection))throw new Error('Choose an existing bool output that does not create a cycle. The source may have changed.');
  if(!['true','false'].includes(expected))throw new Error('Choose a boolean condition value.');
  const [source_node,source_port]=JSON.parse(selection);updated.condition={source_node,source_port,equals:expected==='true'};
  return updated;
}
export function conditionalRunSummary(status:RecordValue):string{
  const skipped=Array.isArray(status?.skipped_node_ids)?status.skipped_node_ids.filter((id:unknown)=>typeof id==='string'):[];
  return skipped.length?`Skipped by recorded native branch dependencies: ${skipped.join(', ')}. These stages produced no outputs.`:'';
}
