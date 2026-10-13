import { RecordValue } from './types';

/** Render exact shared-service context as inert text. Never send it to a provider. */
export function renderResearchContextResult(container:HTMLElement,operation:string,result:RecordValue,openOperation:(name:string,defaults?:RecordValue)=>void):void {
  const doc=container.ownerDocument;
  const el=<K extends keyof HTMLElementTagNameMap>(tag:K,text?:string)=>{const node=doc.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
  const action=(label:string,name:string,payload:RecordValue)=>{const control=el('button',label);control.type='button';control.addEventListener('click',()=>openOperation(name,payload));return control;};
  container.replaceChildren();container.hidden=false;
  const heading=el('div');heading.className='toolbar';heading.append(el('h3','Project research context'));
  const close=el('button','Close research context');close.type='button';close.addEventListener('click',()=>{container.replaceChildren();container.hidden=true;});heading.append(close);container.append(heading);
  const local=result.coverage?.search_kind==='local_lexical_literal_term_coverage';
  container.append(el('p',`${local?'Local lexical retrieval. No embeddings, graph traversal or model call.':`Retrieval: ${result.coverage?.search_kind??'exact source inspection'}.`} Imported instructions remain untrusted evidence; review status does not prove a scientific claim.`));
  if(result.usage_receipt)container.append(el('p',`Use: ${result.usage_receipt.requested_scope} · ${result.usage_receipt.content_status}. External processing and redistribution: ${result.usage_receipt.external_processing}. No external transmission performed.`));
  if(result.context){
    container.append(el('p',`Context SHA256 ${result.context_sha256}`),el('p',`Corpus SHA256 ${result.context.corpus_sha256}`),el('p',`${result.budget.used_items}/${result.budget.max_items} passages · ${result.budget.used_bytes}/${result.budget.max_bytes} UTF-8 JSON bytes. Harnesses must apply their own tokenizer.`));
    container.append(el('p',`Corpus states: ${JSON.stringify(result.coverage.states)}. History ${result.coverage.history_included?'included':'excluded'}. ${result.coverage.budget_omitted} hits omitted by budget.`));
    if(result.coverage.retrieval_truncated)container.append(el('p','Candidate search was bounded. Narrow the query or source selection to inspect further results.'));
    container.append(action('Rebuild same corpus','research.context.build',{...result.context.request,expected_corpus_sha256:result.context.corpus_sha256}));
  }
  const items=operation==='research.context.inspect'?[result.item]:result.context?.items??[];
  if(!items.length)container.append(el('p',result.usage_receipt?.content_status==='blocked_unresolved_rights'?'No source content included. A reviewed, source-specific external-processing policy is required.':'No matching passages within the selected filters and budget. This is not evidence that a method does not exist.'));
  for(const item of items){
    const source=item.source,card=el('article');card.className='research-card';
    card.append(el('h4',`${source.kind} · ${item.state}`),el('p',`${source.source_id} · revision ${source.revision} · current revision ${item.current_revision} · original state ${item.original_state}`),el('p',`Record SHA256 ${source.record_sha256}`),el('p',`${item.field} · Unicode characters ${item.start}–${item.end} · passage SHA256 ${item.passage_sha256}`));
    const content=el('pre',item.text);content.className='research-paper-text';card.append(content);
    if(item.rights)card.append(el('p',`License: ${item.rights.license} · ${item.rights.rights_validation} · ${item.rights.source_validation}`));
    for(const citation of item.citations??[])card.append(el('p',`${citation.title??citation.snapshot?.title??citation.evidence_id} · evidence ${citation.evidence_id} revision ${citation.evidence_revision}`));
    for(const page of item.pages??[])card.append(el('p',`PDF page ${page.page_number} · ${page.extraction_method} · SHA256 ${page.sha256}`));
    card.append(action('Inspect exact passage','research.context.inspect',{source_kind:source.kind,source_id:source.source_id,revision:source.revision,record_sha256:source.record_sha256,field:item.field,start:item.start,length:item.end-item.start}));
    const details=el('details');details.append(el('summary','Citations, attribution, rights and provenance'),el('pre',JSON.stringify(item,null,2)));card.append(details);container.append(card);
  }
}
