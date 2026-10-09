import { RecordValue } from './types';
/** Preserve exact passage metadata when selecting existing citations. */
export function selectedCitations(source:string,selectedIds:string[]):RecordValue[]{
  let previous:unknown;try{previous=JSON.parse(source||'[]');}catch{throw new Error('Citations must be valid JSON before changing selection.');}
  if(!Array.isArray(previous)||previous.some(item=>!item||typeof item!=='object'||typeof item.evidence_id!=='string'))throw new Error('Citations must be an array of evidence references.');
  return [...new Set(selectedIds)].map(id=>structuredClone(previous.find(item=>item.evidence_id===id)??{evidence_id:id}));
}
