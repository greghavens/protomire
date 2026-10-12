import {Recipe,RecordValue,BoundedIteration} from './types';
/** Canonical JSON editing only. Shared service validation remains authoritative. */
export function iterationIdentity(recipe:Recipe):string{return JSON.stringify(recipe.bounded_iterations??[]);}
export function withIterationDraft(recipe:Recipe,source:string,baseline:string):Recipe{
  if(iterationIdentity(recipe)!==baseline)throw new Error('Iteration settings changed in source. Your panel draft is preserved; discard/reload before applying.');
  const blocks:unknown=JSON.parse(source);
  if(!Array.isArray(blocks))throw new Error('Bounded iterations must be a JSON array.');
  return {...recipe,bounded_iterations:structuredClone(blocks) as BoundedIteration[]};
}
export function iterationNodeLabel(recipe:Recipe,nodeId:string):string{
  const block=recipe.bounded_iterations?.find(item=>Array.isArray(item.node_ids)&&item.node_ids.includes(nodeId));
  return block?`${block.id} · at most ${block.max_iterations} iterations`:'';
}
export function iterationRunSummary(status:RecordValue):string{
  return Object.entries(status?.bounded_iterations??{}).map(([id,value])=>{
    const block=value as RecordValue,rows=Array.isArray(block.iterations)?block.iterations:[],last=rows.at(-1);
    return `${id}: ${rows.filter((row:RecordValue)=>row.status==='completed').length}/${block.max_iterations} iterations completed${last?.stop_reason?`; stopped by ${last.stop_reason}`:''}. Checkpoints and artifacts retain each native invocation ID.`;
  }).join(' ');
}
export class IterationEditor{
  private textarea:HTMLTextAreaElement; private baseline='[]'; private changed=false;
  constructor(root:HTMLElement,private current:()=>Recipe|undefined,private apply:(recipe:Recipe)=>boolean,private dirty:()=>void,private report:(message:string)=>void){
    const details=document.createElement('details'),summary=document.createElement('summary');summary.textContent='Bounded iteration blocks';
    const help=document.createElement('p');help.className='muted';help.textContent='Declare id, node_ids, max_iterations and typed state feedback. Optional until reads a body bool output after each iteration. Select whole blocks. Final outputs feed downstream; every invocation retains native artifacts. Set state.resume_mode to warm_start explicitly for training checkpoint feedback. Exact loop continuation and repeated-trainer live sidecars are unavailable. Save and validate this canonical recipe before running.';
    this.textarea=document.createElement('textarea');this.textarea.rows=8;this.textarea.spellcheck=false;this.textarea.setAttribute('aria-label','Canonical bounded iteration blocks JSON');
    this.textarea.placeholder='[{"id":"refine","node_ids":["train","evaluate"],"max_iterations":3,"state":[]}]';
    const applyButton=document.createElement('button');applyButton.type='button';applyButton.textContent='Apply iteration blocks to draft';
    const reset=document.createElement('button');reset.type='button';reset.textContent='Discard iteration panel edits';
    this.textarea.addEventListener('input',()=>{this.changed=true;this.dirty();});
    applyButton.onclick=()=>{try{const recipe=this.current();if(!recipe)return;const updated=withIterationDraft(recipe,this.textarea.value,this.baseline);if(this.apply(updated)){this.changed=false;this.update(updated);this.dirty();}}catch(error){this.report((error as Error).message);}};
    reset.onclick=()=>{this.changed=false;this.update(this.current());this.dirty();};
    details.append(summary,help,this.textarea,applyButton,reset);root.append(details);
  }
  update(recipe?:Recipe):void{if(this.changed)return;this.baseline=recipe?iterationIdentity(recipe):'[]';this.textarea.value=JSON.stringify(JSON.parse(this.baseline),null,2);}
  reset(recipe?:Recipe):void{this.changed=false;this.update(recipe);}
  capture():RecordValue{return {source:this.textarea.value,baseline:this.baseline,changed:this.changed};}
  restore(value?:RecordValue):void{if(value?.changed){this.textarea.value=String(value.source);this.baseline=String(value.baseline);this.changed=true;}}
}
