import { Recipe, RecordValue } from './types';

/** This edits the canonical recipe only. Service admission is authoritative. */
export function executionSettingsIdentity(recipe?:Recipe):string {
  return JSON.stringify(recipe?.execution??null);
}
export function stageParallelism(recipe?:Recipe):number {
  return recipe?.execution?.max_parallel_stages??1;
}
export function withStageParallelism(recipe:Recipe,limit:number,baseline:string):Recipe {
  if(executionSettingsIdentity(recipe)!==baseline)throw new Error('Stage execution settings changed in source. Your panel edits are preserved; discard/reload before applying.');
  if(!Number.isInteger(limit)||limit<1||limit>8)throw new Error('Choose a whole number from 1 to 8 parallel stages.');
  if(recipe.execution!==undefined&&(!recipe.execution||typeof recipe.execution!=='object'||Array.isArray(recipe.execution)))throw new Error('Fix the execution object in source before applying stage settings.');
  // Merely opening/applying the default must not rewrite historical manifests.
  if(limit===1&&recipe.execution?.max_parallel_stages===undefined)return {...recipe};
  return {...recipe,execution:{...recipe.execution,max_parallel_stages:limit}};
}

export class ExecutionSettingsEditor {
  private input:HTMLInputElement; private status:HTMLElement; private baseline='null'; private changed=false;
  constructor(root:HTMLElement,private current:()=>Recipe|undefined,private apply:(recipe:Recipe)=>boolean,private dirty:()=>void,private report:(message:string)=>void) {
    const details=document.createElement('details');details.className='run-settings';details.id='stage-execution-settings';
    const summary=document.createElement('summary');summary.textContent='Stage execution';
    const help=document.createElement('p');help.className='muted';help.textContent='Set the maximum independent recipe stages that may run at once. One keeps sequential execution. Data and branch dependencies still wait for their producers; bounded iterations remain sequential barriers. This limit does not reserve CPU, memory or GPU resources. Checkpoint-evaluation concurrency is configured separately below.';
    const form=document.createElement('form');form.id='stage-execution-form';
    const label=document.createElement('label');label.textContent='Maximum parallel stages';
    this.input=document.createElement('input');this.input.id='stage-parallelism';this.input.type='number';this.input.min='1';this.input.max='8';this.input.step='1';this.input.required=true;this.input.value='1';
    label.append(this.input);
    this.status=document.createElement('p');this.status.id='stage-execution-capability';this.status.className='muted';this.capabilities();
    const applyButton=document.createElement('button');applyButton.type='submit';applyButton.textContent='Apply stage settings to draft';
    const reset=document.createElement('button');reset.type='button';reset.id='reset-stage-execution';reset.textContent='Discard stage settings edits';
    const saveHelp=document.createElement('span');saveHelp.className='muted';saveHelp.textContent='Save the recipe revision and preview its execution plan before running.';
    this.input.addEventListener('input',()=>{this.changed=true;this.dirty();});
    form.onsubmit=event=>{event.preventDefault();try{const recipe=this.current();if(!recipe)return;const updated=withStageParallelism(recipe,Number(this.input.value),this.baseline);if(this.apply(updated)){this.changed=false;this.update(updated);this.dirty();}}catch(error){this.report((error as Error).message);}};
    reset.onclick=()=>{try{const recipe=this.current();this.changed=false;this.update(recipe);this.dirty();}catch(error){this.report((error as Error).message);}};
    form.append(label,applyButton,reset,saveHelp);details.append(summary,help,this.status,form);root.append(details);
  }
  isDirty():boolean{return this.changed;}
  capabilities(capabilities?:RecordValue):void {
    const settings=capabilities?.recipe_settings?.execution?.max_parallel_stages;
    this.status.textContent=capabilities?.control_flow?.independent_stage_concurrency===true&&Number.isInteger(settings?.supported_maximum)&&settings.supported_maximum>1
      ?`This backend supports up to ${settings.supported_maximum} independent native stages${settings.platforms?.includes('linux')&&settings.devices?.includes('cpu')?' on Linux CPU':''}. Run planning checks the saved recipe and selected device.`
      :capabilities?'This backend does not advertise parallel stage execution. Existing sequential routes remain available; preview the plan before running.':'Backend stage-concurrency support has not been checked. Preview the plan before running.';
  }
  update(recipe?:Recipe):void{if(this.changed)return;this.baseline=executionSettingsIdentity(recipe);this.input.value=String(stageParallelism(recipe));}
  reset(recipe?:Recipe):void{this.changed=false;this.update(recipe);}
  capture():RecordValue{return {value:this.input.value,baseline:this.baseline,changed:this.changed};}
  restore(value?:RecordValue):void{if(value?.changed){this.input.value=String(value.value);this.baseline=String(value.baseline);this.changed=true;}}
}
