import { Recipe, RecipeNode, RecordValue } from './types';
import { parameterSchema } from './parameterControls';

export const COMPONENT_PROTOCOL='modelbuilder.component/v1';
export const COMPONENT_ROLES=['training','evaluation','export','dataset','model','utility'];
export const COMPONENT_PORT_TYPES=['dict','Path','str','int','float','bool','DatasetArtifact','CheckpointArtifact','EvaluationArtifact','ExportArtifact'];
const reserved=new Set('False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield spec inputs parameters context'.split(' '));
const object=(value:unknown):value is RecordValue=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const identifier=(value:unknown)=>typeof value==='string'&&/^[A-Za-z_]\w*$/.test(value)&&!reserved.has(value);
const pythonName=(value:unknown)=>typeof value==='string'&&value.split('.').every(part=>/^[A-Za-z_]\w*$/.test(part)&&(!reserved.has(part)||['spec','inputs','parameters','context'].includes(part)));
const strings=(value:unknown):value is string[]=>Array.isArray(value)&&value.every(item=>typeof item==='string');
const sourceIdentity=(recipe:Recipe)=>JSON.stringify(recipe.source??null);
const pretty=(value:unknown)=>JSON.stringify(value,null,2);

/** An inert, deliberately incomplete draft. No capabilities or source identities are guessed. */
export function blankComponent():RecordValue {
  return {id:'',version:'',protocol:COMPONENT_PROTOCOL,role:'',parameter_schema:{type:'object',properties:{}},capabilities:{resume_modes:[],export_formats:[],frameworks:[]}};
}
export function componentSourceFiles(recipe:Recipe):{path:string;sha256:string}[] {
  const files=recipe.source?.files;
  return Array.isArray(files)?files.filter(file=>object(file)&&typeof file.path==='string'&&file.path.endsWith('.py')&&!file.path.startsWith('/')&&!file.path.split('/').includes('..')&&/^[a-f0-9]{64}$/.test(file.sha256)):[];
}
/** Completeness hints only. The service remains the authority for schemas and execution admission. */
export function componentDraftProblems(recipe:Recipe,node:RecipeNode):string[] {
  if(node.kind!=='component.run')return [];
  const issues:string[]=[],component=node.component,implementation=node.implementation_ref;
  if(!object(component)){issues.push('Declare a component/v1 contract.');return issues;}
  if(typeof component.id!=='string'||!component.id.trim())issues.push('Enter a component ID.');
  if(typeof component.version!=='string'||!component.version.trim())issues.push('Enter a component version.');
  if(component.protocol!==COMPONENT_PROTOCOL)issues.push('Use the component/v1 protocol.');
  if(!COMPONENT_ROLES.includes(component.role))issues.push('Choose the component role.');
  if(!object(component.parameter_schema))issues.push('Declare a parameter schema JSON object.');
  const caps=component.capabilities;
  if(!object(caps)||!strings(caps.resume_modes)||!strings(caps.export_formats)||!strings(caps.frameworks))issues.push('Declare resume_modes, export_formats and frameworks arrays.');
  else if(caps.resume_modes.some(mode=>!['exact_state','warm_start'].includes(mode))||new Set(caps.resume_modes).size!==caps.resume_modes.length||(caps.checkpoint_events!==undefined&&typeof caps.checkpoint_events!=='boolean'))issues.push('Use supported explicit capabilities.');
  if(!object(implementation)||!pythonName(implementation.module)||!pythonName(implementation.entrypoint))issues.push('Enter a Python module and entrypoint.');
  if(!object(implementation)||!componentSourceFiles(recipe).some(file=>file.path===implementation.path&&file.sha256===implementation.sha256)||implementation.commit!==recipe.source?.commit||!(/^[a-f0-9]{40}(?:[a-f0-9]{24})?$/.test(implementation.commit))||/^0+$/.test(implementation.commit))issues.push('Choose a Python file from this recipe’s pinned source closure at a real commit.');
  for(const direction of ['inputs','outputs'] as const){const ports=node[direction];if(!Array.isArray(ports)||(direction==='outputs'&&!ports.length))issues.push(direction==='outputs'?'Declare at least one supported output port.':'Declare an input port array.');else if(ports.some(port=>!object(port)||!identifier(port.name)||!COMPONENT_PORT_TYPES.includes(port.type))||new Set(ports.map(port=>port.name)).size!==ports.length)issues.push(`Use unique Python names and supported types for ${direction}.`);}
  return issues;
}
export type ComponentFields={id:string;version:string;role:string;sourcePath:string;module:string;entrypoint:string;parameterSchema:string;capabilities:string};
export type ComponentDraft={sourceBaseline:string;fields:ComponentFields};
function jsonObject(text:string,label:string):RecordValue {let value;try{value=JSON.parse(text);}catch{throw new Error(`Fix ${label} JSON. Your panel draft is preserved.`);}if(!object(value))throw new Error(`${label} must be a JSON object. Your panel draft is preserved.`);return value;}
export function componentFields(node:RecipeNode):ComponentFields {
  const component=object(node.component)?node.component:blankComponent(),ref=object(node.implementation_ref)?node.implementation_ref:{};
  return {id:component.id??'',version:component.version??'',role:component.role??'',sourcePath:ref.path??'',module:ref.module??'',entrypoint:ref.entrypoint??'',parameterSchema:pretty(component.parameter_schema??{}),capabilities:pretty(component.capabilities??blankComponent().capabilities)};
}
/** The same field-to-node transform is used by the panel and real HTTP acceptance. */
export function applyComponentFields(recipe:Recipe,node:RecipeNode,fields:ComponentFields,sourceBaseline=sourceIdentity(recipe)):RecipeNode {
  if(node.kind!=='component.run')throw new Error('Component fields require a component.run node.');
  if(sourceIdentity(recipe)!==sourceBaseline)throw new Error('Recipe source pins changed after this panel opened. Your draft is preserved; discard panel edits / reload node before applying.');
  parameterSchema(node); // Never reconcile conflicting direct/component declarations silently.
  const file=componentSourceFiles(recipe).find(file=>file.path===fields.sourcePath);
  if(!file)throw new Error('Select a Python entrypoint from recipe.source.files. Pin source files in Code first; no hash is invented.');
  const schema=jsonObject(fields.parameterSchema,'component parameter schema'),caps=jsonObject(fields.capabilities,'component capabilities');
  const next:RecipeNode={...node,component:{...(object(node.component)?node.component:{}),id:fields.id.trim(),version:fields.version.trim(),protocol:COMPONENT_PROTOCOL,role:fields.role,parameter_schema:schema,capabilities:caps},implementation_ref:{...(object(node.implementation_ref)?node.implementation_ref:{}),module:fields.module.trim(),entrypoint:fields.entrypoint.trim(),path:file.path,sha256:file.sha256,commit:recipe.source.commit}};
  if(node.parameter_schema!==undefined)next.parameter_schema=structuredClone(schema);
  const problems=componentDraftProblems(recipe,next);if(problems.length)throw new Error(`Incomplete component draft: ${problems.join(' ')}`);
  return next;
}

/** Human fields in the existing node panel, with exact intermediate text recovery. */
export class ComponentAuthoringControls {
  private fields=new Map<keyof ComponentFields,HTMLInputElement|HTMLTextAreaElement|HTMLSelectElement>();
  private sourceBaseline:string;
  private status:HTMLElement;
  private pins:HTMLElement;
  constructor(private root:HTMLElement,private recipe:Recipe,node:RecipeNode,onDirty:()=>void){
    this.sourceBaseline=sourceIdentity(recipe);root.replaceChildren();root.hidden=node.kind!=='component.run';
    const doc=root.ownerDocument;this.status=doc.createElement('p');this.status.setAttribute('role','status');this.pins=doc.createElement('p');this.pins.className='muted identity';
    if(root.hidden)return;
    const heading=doc.createElement('h4');heading.textContent='Source-bound component contract';root.append(heading,this.status);
    const values=componentFields(node);
    const specs:[keyof ComponentFields,string,string][]=[['id','Component ID','input'],['version','Component version','input'],['role','Component role','select'],['sourcePath','Pinned Python source file','select'],['module','Python module','input'],['entrypoint','Python entrypoint','input'],['parameterSchema','Component parameter schema (JSON)','textarea'],['capabilities','Declared capabilities (JSON)','textarea']];
    for(const [key,title,tag]of specs){const label=doc.createElement('label');label.textContent=title;const input=doc.createElement(tag) as HTMLInputElement|HTMLTextAreaElement|HTMLSelectElement;input.id=`component-${key}`;input.dataset.componentField=key;if(tag==='textarea')(input as HTMLTextAreaElement).rows=5;if(tag==='select'){const choices=key==='role'?COMPONENT_ROLES:componentSourceFiles(recipe).map(file=>file.path);for(const value of ['',...choices]){const option=doc.createElement('option');option.value=value;option.textContent=value||(key==='role'?'Choose a role':'Choose a pinned Python file');input.append(option);}if(values[key]&&!choices.includes(values[key])){const option=doc.createElement('option');option.value=values[key];option.textContent=`${values[key]} (not in current declaration)`;input.append(option);}}
      input.value=values[key];input.addEventListener('input',()=>{this.status.textContent='Unapplied component fields. Apply to check completeness, then validate and save the recipe.';this.showPins();onDirty();});label.append(input);root.append(label);this.fields.set(key,input);
    }
    root.append(this.pins);const help=doc.createElement('p');help.className='muted';help.textContent=`Protocol: ${COMPONENT_PROTOCOL}. Capabilities are your explicit declaration; empty arrays grant none. Parameter schema changes appear in typed controls after Apply. Declare at least one output below. Supported port types: ${COMPONENT_PORT_TYPES.join(', ')}. Source trust and run admission require separate service checks.`;root.append(help);
    const problems=componentDraftProblems(recipe,node);this.status.className=problems.length?'warning':'muted';this.status.textContent=problems.length?`Incomplete component draft. ${problems.join(' ')}`:'Contract fields complete. Service validation and source admission are still required.';this.showPins();
  }
  private values():ComponentFields{return Object.fromEntries([...this.fields].map(([key,input])=>[key,input.value])) as ComponentFields;}
  private showPins():void{const path=this.fields.get('sourcePath')?.value,file=componentSourceFiles(this.recipe).find(file=>file.path===path);this.pins.textContent=file?`Commit: ${this.recipe.source?.commit}\nSHA256: ${file.sha256}`:'No pinned source file selected. Use Code to pin the existing source closure first.';}
  read(recipe:Recipe,node:RecipeNode):RecipeNode{return applyComponentFields(recipe,node,this.values(),this.sourceBaseline);}
  capture():ComponentDraft{return {sourceBaseline:this.sourceBaseline,fields:this.values()};}
  restore(draft:ComponentDraft|undefined):void{if(!draft)return;this.sourceBaseline=draft.sourceBaseline;for(const [key,input]of this.fields){const value=draft.fields?.[key];if(typeof value==='string'){if(input.tagName==='SELECT'&&!Array.from((input as HTMLSelectElement).options).some(option=>option.value===value)){const option=this.root.ownerDocument.createElement('option');option.value=value;option.textContent=`${value} (recovered draft)`;input.append(option);}input.value=value;}}this.status.textContent='Recovered unapplied component fields. Apply or explicitly discard them before saving.';this.showPins();}
}
