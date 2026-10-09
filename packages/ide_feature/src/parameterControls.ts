import { RecipeNode, RecordValue } from './types';

type Field = {path:string[];schema:RecordValue;input:HTMLInputElement|HTMLTextAreaElement|HTMLSelectElement;present:HTMLInputElement;kind:string;changed:boolean;row:HTMLElement;values?:unknown[];children?:Field[]};
export type ParameterDraft = {schema:string;baseline:string;dirty:boolean;fields:{path:string[];value:string;checked:boolean;present:boolean;changed:boolean}[]};
const object=(value:unknown):value is RecordValue=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const owns=(value:RecordValue,key:string)=>Object.prototype.hasOwnProperty.call(value,key);
const same=(a:unknown,b:unknown):boolean=>{if(Array.isArray(a)&&Array.isArray(b))return a.length===b.length&&a.every((v,i)=>same(v,b[i]));if(object(a)&&object(b))return Object.keys(a).length===Object.keys(b).length&&Object.keys(a).every(key=>owns(b,key)&&same(a[key],b[key]));return Object.is(a,b);};
/** The canonical declaration belongs to the existing node/harness, never a UI framework. */
export function parameterSchema(node:RecipeNode):RecordValue|undefined {
  const direct=node.parameter_schema,component=node.component?.parameter_schema;
  if(direct!==undefined&&component!==undefined&&!same(direct,component))throw new Error('Node and component parameter schemas disagree. Correct the canonical JSON declaration before using typed controls.');
  const schema=component??direct;
  return object(schema)?schema:undefined;
}
function resolveSchema(schema:unknown,root:RecordValue,seen=new Set<string>()):RecordValue {
  if(!object(schema))return {};
  if(typeof schema.$ref!=='string')return schema;
  const ref=schema.$ref;if(!ref.startsWith('#/')||seen.has(ref)||seen.size>=16)return {};
  seen.add(ref);let result:any=root;
  for(const part of ref.slice(2).split('/').map(x=>x.replaceAll('~1','/').replaceAll('~0','~')))result=object(result)&&owns(result,part)?result[part]:undefined;
  return {...resolveSchema(result,root,seen),...Object.fromEntries(Object.entries(schema).filter(([key])=>key!=='$ref'))};
}
function parseObject(source:string):RecordValue {let value;try{value=JSON.parse(source);}catch{throw new Error('Fix parameter JSON before applying typed changes. Your JSON and typed drafts are preserved.');}if(!object(value))throw new Error('Parameters must be a JSON object. Your draft is preserved.');return value;}
/** Fields remain independent until Apply node; incomplete values survive refresh/recovery. */
export class ParameterControls {
  private fields:Field[]=[];
  private dirty=false;
  private baseline:string;
  private schemaKey:string;
  private rejectedDraft?:ParameterDraft;
  private message:HTMLElement;
  private fieldRoot:HTMLElement;
  constructor(private root:HTMLElement,private source:HTMLTextAreaElement,private schema:RecordValue|undefined,private onDirty:()=>void,private report:(message:string)=>void) {
    this.baseline=source.value;this.schemaKey=JSON.stringify(schema??null);
    const doc=root.ownerDocument;root.replaceChildren();this.message=doc.createElement('p');this.message.className='muted';this.fieldRoot=doc.createElement('div');this.fieldRoot.className='parameter-fields';root.append(this.message,this.fieldRoot);this.render();
  }
  sourceChanged():void {if(this.dirty){this.message.textContent='Parameter JSON changed while typed fields have edits. Both drafts are preserved. Discard the node panel edits to reload before applying.';return;}this.baseline=this.source.value;this.render();}
  private render():void {
    this.fieldRoot.replaceChildren();this.fields=[];
    if(!this.schema){this.message.textContent='This node has no canonical parameter schema. Edit its parameters in the JSON escape hatch below.';return;}
    let values:RecordValue;try{values=parseObject(this.source.value);}catch(error){this.message.textContent=(error as Error).message;return;}
    const resolved=resolveSchema(this.schema,this.schema);
    if(!object(resolved.properties)){this.message.textContent='This schema requires JSON authoring. The service validates the complete canonical declaration.';return;}
    this.message.textContent='Fields come from this node’s canonical parameter schema. Optional fields stay absent until enabled. Apply node changes to update the recipe draft; validation remains authoritative on the service.';
    this.fields=this.build(resolved,values,[],this.fieldRoot,0);
  }
  private build(schema:RecordValue,values:RecordValue,path:string[],parent:HTMLElement,depth:number):Field[] {
    const doc=this.root.ownerDocument,fields:Field[]=[];
    for(const [key,raw]of Object.entries(schema.properties??{})){
      if(fields.length>=200||depth>8){parent.append(Object.assign(doc.createElement('p'),{textContent:'Additional schema fields remain available in JSON.'}));break;}
      const child=resolveSchema(raw,this.schema!),fieldPath=[...path,key],required=(schema.required??[]).includes(key),present=owns(values,key),value=values[key],types=Array.isArray(child.type)?child.type:[child.type];
      const complex=child.oneOf||child.anyOf||child.allOf||child.not||child.if;
      let kind=child.enum?'enum':child.const!==undefined?'const':!complex&&types.length===1&&types[0]==='object'&&object(child.properties)?'group':!complex&&types.length===1&&['string','integer','number','boolean'].includes(types[0])?types[0]:'json';
      if(kind==='group'&&present&&!object(value))kind='json';
      const row=doc.createElement(kind==='group'?'fieldset':'div');row.className=kind==='group'?'parameter-group':'parameter-field';const title=doc.createElement(kind==='group'?'legend':'label');title.textContent=`${child.title??key}${required?' *':''}`;
      const enable=doc.createElement('input');enable.type='checkbox';enable.checked=required||present;enable.disabled=required;enable.dataset.parameterPresent=JSON.stringify(fieldPath);enable.setAttribute('aria-label',`Include ${fieldPath.join('.')}`);title.prepend(enable);row.append(title);
      let input:Field['input'];
      if(kind==='enum'){input=doc.createElement('select');for(const [i,choice]of child.enum.entries()){const option=doc.createElement('option');option.value=String(i);option.textContent=typeof choice==='string'?choice:JSON.stringify(choice);input.append(option);}input.value=String(child.enum.findIndex((choice:unknown)=>same(choice,value)));if(input.value==='-1'||!present){const absent=doc.createElement('option');absent.value='';absent.textContent=present?'Current JSON value is outside the enum':'Choose a value';input.prepend(absent);input.value='';}}
      else if(kind==='json'||kind==='group'){input=doc.createElement('textarea');input.rows=3;input.spellcheck=false;input.value=present?JSON.stringify(value,null,2):'';if(kind==='group')input.hidden=true;}
      else {input=doc.createElement('input');input.type=kind==='boolean'?'checkbox':kind==='number'||kind==='integer'?'number':'text';if(kind==='boolean')input.checked=value===true;else input.value=present?String(value):'';if(kind==='const')input.readOnly=true;if(kind==='number'||kind==='integer'){input.step=kind==='integer'?'1':'any';if(child.minimum!==undefined)input.min=String(child.minimum);if(child.maximum!==undefined)input.max=String(child.maximum);}if(child.maxLength!==undefined)input.maxLength=child.maxLength;}
      input.dataset.parameterPath=JSON.stringify(fieldPath);input.setAttribute('aria-label',fieldPath.join('.'));input.disabled=!enable.checked;title.append(input);
      const field:Field={path:fieldPath,schema:child,input,present:enable,kind,changed:false,row,values:child.enum};
      if(kind==='group'){field.children=this.build(child,object(value)?value:{},fieldPath,row,depth+1);(row as HTMLFieldSetElement).disabled=!enable.checked;}
      if(child.description){const help=doc.createElement('small');help.textContent=String(child.description);help.className='muted';row.append(help);}
      const changed=()=>{field.changed=true;this.dirty=true;input.disabled=!enable.checked;if(kind==='group')(row as HTMLFieldSetElement).disabled=!enable.checked;this.onDirty();};input.addEventListener('input',changed);input.addEventListener('change',changed);enable.addEventListener('change',changed);fields.push(field);parent.append(row);
    }
    return fields;
  }
  capture():ParameterDraft {if(this.rejectedDraft)return structuredClone(this.rejectedDraft);const flatten=(fields:Field[]):Field[]=>fields.flatMap(field=>[field,...flatten(field.children??[])]);return {schema:this.schemaKey,baseline:this.baseline,dirty:this.dirty,fields:flatten(this.fields).map(field=>({path:field.path,value:field.input.value,checked:(field.input as HTMLInputElement).checked===true,present:field.present.checked,changed:field.changed}))};}
  restore(state:ParameterDraft|undefined):void {if(!state)return;if(state.schema!==this.schemaKey){this.rejectedDraft=structuredClone(state);this.report('Recovered typed fields have a different schema. The preserved JSON/panel draft must be reconciled before applying.');this.dirty=true;this.baseline='schema-conflict:'+state.baseline;return;}if(state.dirty&&!this.fields.length){const raw=this.source.value;try{this.source.value=state.baseline;this.render();}finally{this.source.value=raw;}}const flatten=(fields:Field[]):Field[]=>fields.flatMap(field=>[field,...flatten(field.children??[])]);for(const field of flatten(this.fields)){const saved=state.fields.find(value=>same(value.path,field.path));if(saved){field.changed=saved.changed??state.dirty;field.input.value=saved.value;(field.input as HTMLInputElement).checked=saved.checked;field.present.checked=saved.present;field.input.disabled=!saved.present;if(field.kind==='group')(field.row as HTMLFieldSetElement).disabled=!saved.present;}}this.dirty=state.dirty;this.baseline=state.baseline;if(this.dirty&&this.source.value!==this.baseline)this.sourceChanged();}
  read():RecordValue {
    if(this.rejectedDraft)throw new Error('Recovered typed fields have a different canonical schema. Reconcile the preserved draft before applying.');const original=parseObject(this.source.value);if(!this.dirty)return original;
    if(this.source.value!==this.baseline)throw new Error('Parameter JSON changed after typed edits began. Both drafts are preserved. Copy your changes, then discard panel edits and reload before applying.');
    const touched=(field:Field):boolean=>field.changed||(field.children??[]).some(touched);
    const apply=(fields:Field[],source:RecordValue):RecordValue=>{const result={...source};for(const field of fields){if(!touched(field))continue;const key=field.path.at(-1)!;if(!field.present.checked){delete result[key];continue;}let value:unknown;
      if(field.kind==='group')value=apply(field.children??[],object(source[key])?source[key]:{});
      else if(field.kind==='enum'){const index=field.input.value===''?-1:Number(field.input.value);if(index<0||!field.values||index>=field.values.length)throw new Error(`Choose a value for ${field.path.join('.')}.`);value=structuredClone(field.values[index]);}
      else if(field.kind==='boolean')value=(field.input as HTMLInputElement).checked;
      else if(field.kind==='number'||field.kind==='integer'){const text=field.input.value;value=Number(text);if(!text.trim()||!Number.isFinite(value)||(field.kind==='integer'&&!Number.isSafeInteger(value)))throw new Error(`Enter a finite ${field.kind} for ${field.path.join('.')}.`);if(field.schema.minimum!==undefined&&(value as number)<field.schema.minimum||field.schema.maximum!==undefined&&(value as number)>field.schema.maximum)throw new Error(`${field.path.join('.')} is outside its declared range.`);}
      else if(field.kind==='json'){try{value=JSON.parse(field.input.value);}catch{throw new Error(`Fix JSON for ${field.path.join('.')}. All drafts are preserved.`);}}
      else if(field.kind==='const')value=structuredClone(field.schema.const);
      else value=field.input.value;
      Object.defineProperty(result,key,{value,writable:true,enumerable:true,configurable:true});}return result;};
    return apply(this.fields,original);
  }
}
