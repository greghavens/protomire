/** Source identity is local evidence, separate from execution trust and rights. */
import {RecordValue} from './types';
export type SourceKind='git'|'imported_snapshot';

export function sourceIdentityLines(source:RecordValue):string[]{
  if(source.source_kind==='imported_snapshot')return [
    'Source kind: imported_snapshot (imported snapshot)',
    `Local snapshot identity: ${source.repository_id??source.binding?.repository_id??'unavailable'}`,
    `Declared source commit (not authenticated Git): ${source.declared_source_commit??'unavailable'}`,
    `Snapshot digest: ${source.snapshot_digest??'unavailable'}`,
    `Transport recipe digest: ${source.transport_recipe_digest??'unavailable'}`,
    'Imported-byte evidence: the extracted recipe.json and inventoried file bytes are checked locally. This does not authenticate a Git repository or commit, establish ownership, or grant a license.'
  ];
  return [`Source kind: ${source.source_kind??(source.available===false?'unavailable':'git')}`,`Repository identity: ${source.repository_id??source.binding?.repository_id??'unavailable'}`];
}

/** Render with textContent only; source labels and diagnostics are untrusted text. */
export function renderSourceStatus(root:HTMLElement,status:RecordValue|undefined,recipeCommit?:string):boolean{
  root.replaceChildren();
  const append=(tag:string,text:string,className='')=>{const element=root.ownerDocument.createElement(tag);element.textContent=text;element.className=className;root.append(element);};
  if(!status){append('p','Source binding status is not provided by this service.','muted');return false;}
  const imported=status.source_kind==='imported_snapshot',unknown=status.source_kind===undefined&&status.available!==true;
  append('h3',status.bound?(imported?'Bound imported source snapshot':unknown?'Bound project source':'Bound source checkout'):'Project source not bound');
  if(status.binding)append('p',`Alias: ${status.binding.alias}`);
  for(const line of sourceIdentityLines({...status,...(unknown?{available:false}:{})}))append('p',line);
  if(status.workspace_path)append('p',`${imported?'Imported snapshot folder':'Service checkout'}: ${status.workspace_path}`);
  if(!imported&&status.head_commit)append('p',`Checkout HEAD (review default): ${status.head_commit}`);
  if(recipeCommit)append('p',`${imported?'Recipe declared source commit (not authenticated Git)':'Recipe source commit'}: ${recipeCommit}`);
  if(imported)append('p',status.pinned_source_matches===true?'Imported source bytes match the current recipe inventory.':'Imported source bytes have not been validated against the current recipe; review the diagnostics.','muted');
  append('p',status.operator_trusted?'Operator-approved trusted local execution; each run requires confirmation.':'Execution is blocked until the operator approves this source.','muted');
  append('p','Source binding requires explicit owner trust. Source trust and byte validation grant no asset rights; applicable terms need a separate owner-approved rights review.','muted');
  for(const diagnostic of status.diagnostics??[])append('p',`${diagnostic.code}: ${diagnostic.message}`,'warning');
  if(imported)append('p','This imported snapshot retains its declared source inventory. Review local source rights from the Hub workflow; Git source pinning is unavailable for snapshots.','muted');
  return !unknown&&!imported&&(status.source_kind===undefined||status.source_kind==='git');
}
