import { RecordValue } from './types';
export interface ResearchCollections {watches?:RecordValue[];findings?:RecordValue[];proposals?:RecordValue[];texts?:RecordValue[];retrievals?:RecordValue[];errors?:Record<string,string>}
export function renderResearchWorkflow(container:HTMLElement,data:ResearchCollections,available:Set<string>,openOperation:(name:string,defaults?:RecordValue)=>void):void{
  const doc=container.ownerDocument;
  const el=<K extends keyof HTMLElementTagNameMap>(tag:K,text?:string,className?:string)=>{const element=doc.createElement(tag);if(text!==undefined)element.textContent=text;if(className)element.className=className;return element;};
  const action=(label:string,name:string,payload:RecordValue)=>{const control=el('button',label);control.disabled=!available.has(name);control.addEventListener('click',()=>openOperation(name,payload));return control;};
  container.replaceChildren();
  for(const [key,title]of [['watches','Research watches'],['findings','New and changed findings'],['texts','Local paper snapshots'],['retrievals','Content retrieval outcomes'],['proposals','Staged recipe proposals']] as const){
    const section=el('section',undefined,'research-workflow-section');section.append(el('h3',title));
    if(key==='watches')section.append(el('p','Watches start in manual mode. Enable an automatic schedule only after reviewing the provider, query and frequency. Metadata is a citation, not proof of a claim.','muted'));
    if(data.errors?.[key])section.append(el('p',`Unavailable: ${data.errors[key]}`,'warning'));
    const records=data[key]??[];
    if(!records.length)section.append(el('p',`No ${key} recorded.`, 'muted'));
    for(const record of records){
      const card=el('article',undefined,'research-card');card.dataset.resourceId=record.proposal_id??record.finding_id??record.watch_id??record.text_id??record.retrieval_id;
      const heading=el('div',undefined,'toolbar');heading.append(el('h4',record.name??record.title??record.citation?.title??'Untitled finding'),el('span',record.status??(key==='texts'?'local snapshot':record.enabled?'enabled':'disabled'),'pill'));card.append(heading);
      const controls=el('div',undefined,'toolbar');
      if(key==='watches'){
        card.append(el('p',record.query),el('p',`Next due: ${record.next_due_at??'unknown'} · ${record.refresh_count??0} refreshes · revision ${record.revision}`,'muted'));
        const scheduled=record.schedule?.enabled===true;
        card.append(el('p',`Provider: OpenAlex (api.openalex.org) · Query sent: ${record.query} · Up to ${record.limit??5} records every ${record.interval_seconds??86400} seconds.`,'muted'));
        card.append(el('p',scheduled?`Automatic schedule enabled · ${record.scheduler_running?'service scheduler active':'waiting for an active service scheduler'} · Can run as soon as due.`:'Manual refresh only. No automatic provider requests.','muted'));
        card.append(el('p','Automatic requests retrieve metadata into this project’s evidence inbox. They never apply proposals, start training or call an LLM. Editing the query, limit or frequency stops the schedule until you enable it again.','muted'));
        controls.append(scheduled?action('Disable automatic schedule','research.watch.schedule_disable',{watch_id:record.watch_id,expected_revision:record.revision}):action('Enable automatic schedule','research.watch.schedule_enable',{watch_id:record.watch_id,expected_revision:record.revision,provider:record.provider??'openalex',query:record.query,limit:record.limit??5,interval_seconds:record.interval_seconds??86400}));
        controls.append(action('Refresh now','research.watch.refresh',{watch_id:record.watch_id,expected_revision:record.revision}),action('Edit watch','research.watch.update',{watch_id:record.watch_id,expected_revision:record.revision,changes:{enabled:record.enabled,name:record.name,query:record.query,interval_seconds:record.interval_seconds}}));
      }else if(key==='findings'){
        card.append(el('p',`${record.kind??'new'} citation · Evidence ${record.evidence_id}`,'mono'),el('p','Scientific claim not verified. Review the source and evaluation evidence separately.','muted'));
        appendResearchSourceActions(card,record.citation??{},available,openOperation);
        if(record.change_diff){const details=el('details');details.append(el('summary','Changed source metadata'),el('pre',record.change_diff));card.append(details);}
        controls.append(action('Review finding','research.finding.review',{finding_id:record.finding_id,expected_revision:record.revision,status:'reviewed'}),action('Dismiss finding','research.finding.review',{finding_id:record.finding_id,expected_revision:record.revision,status:'dismissed'}),action('Inspect citation','evidence.get',{evidence_id:record.evidence_id}));
      }else if(key==='texts'){
        card.append(el('p',`${record.license} · ${record.rights_validation} · ${record.source_validation}`,'muted'),el('p',`SHA256 ${record.sha256}`,'mono'));
        if(record.pages?.length)card.append(el('p',`${record.pages.length} PDF pages · Original PDF SHA256 ${record.provenance?.raw_sha256} · ${record.provenance?.ocr_used?'Includes unverified local OCR':'Embedded text extraction'}`,'muted'));
        controls.append(action('Read paper and sections','research.text.inspect',{text_id:record.text_id}),action('Search within paper','research.text.search',{text_id:record.text_id}));
      }else if(key==='retrievals'){
        card.append(el('p',`${record.provider} · ${record.filename??record.source_identifier} · ${record.finished_at}`,'muted'),el('p',record.error?.message??(record.provider==='local_document'?'User-attested document and rights. PDF/page hashes retained; scientific claims remain unverified.':'Supported article license and citation identity matched. Scientific claims remain unverified.'),record.error?'warning':'muted'));
        if(record.text_id)controls.append(action('Read retrieved paper','research.text.inspect',{text_id:record.text_id}));
      }else{
        card.append(el('p',record.rationale),el('p',`Base recipe revision ${record.base_recipe_revision} · Proposal revision ${record.revision} · No training started`,'muted'));
        const citations=el('ul');for(const citation of record.citations??[])citations.append(el('li',`${citation.snapshot?.title??citation.evidence_id} · ${citation.validation_status??'citation only'}${citation.quote?` · “${citation.quote}”`:''}`));card.append(citations);
        const diff=el('details');diff.append(el('summary','Review staged recipe diff'),el('pre',record.recipe_diff??'Diff unavailable.'));card.append(diff);
        if(record.review)card.append(el('p',`${record.review.outcome}: ${record.review.rationale}`,'muted'));
        if(record.applied)card.append(el('p',`Applied as recipe revision ${record.applied.recipe_revision}. Training remains a separate action.`,'muted'));
        controls.append(action('Inspect proposal','research.proposal.get',{proposal_id:record.proposal_id}));
        if(record.status==='proposed')controls.append(action('Approve proposal','research.proposal.review',{proposal_id:record.proposal_id,expected_revision:record.revision,outcome:'approved'}),action('Reject proposal','research.proposal.review',{proposal_id:record.proposal_id,expected_revision:record.revision,outcome:'rejected'}));
        if(record.status==='approved')controls.append(action('Apply approved revision','research.proposal.apply',{proposal_id:record.proposal_id,expected_revision:record.revision}));
      }
      card.append(controls);const detail=el('details');detail.append(el('summary','Identity, provenance and retained history'),el('pre',JSON.stringify(record,null,2)));card.append(detail);section.append(card);
    }
    container.append(section);
  }
}

/** Inert source data is rendered only as text, never HTML, Markdown or commands. */
export function appendResearchSourceActions(container:HTMLElement,evidence:RecordValue,available:Set<string>,openOperation:(name:string,defaults?:RecordValue)=>void):void{
  const doc=container.ownerDocument;
  const abstract=evidence.citation_metadata?.abstract;
  if(abstract){const details=doc.createElement('details'),summary=doc.createElement('summary'),text=doc.createElement('p');summary.textContent=`Abstract: ${abstract.status} · Rights: ${abstract.license??'unknown'}`;text.textContent=abstract.status==='available'?abstract.text:'Abstract text was not retained because it is missing, malformed or lacks a supported source license.';details.append(summary,text);container.append(details);}
  if(!evidence.evidence_id)return;
  if(available.has('research.document.import')){
    const document=doc.createElement('button');document.textContent='Import authorized PDF';document.type='button';
    document.addEventListener('click',()=>openOperation('research.document.import',{evidence_id:evidence.evidence_id,expected_evidence_revision:evidence.revision}));container.append(document);
  }
  if(!available.has('research.text.fetch'))return;
  const control=doc.createElement('button');control.textContent='Retrieve licensed paper';control.type='button';
  control.addEventListener('click',()=>openOperation('research.text.fetch',{evidence_id:evidence.evidence_id,expected_evidence_revision:evidence.revision,provider:'europe_pmc',...(/^PMC[1-9][0-9]{0,9}$/.test(evidence.source_identifier??'')?{source_identifier:evidence.source_identifier}:{})}));container.append(control);
}

export function renderResearchTextResult(container:HTMLElement,operation:string,result:RecordValue,openOperation:(name:string,defaults?:RecordValue)=>void):void{
  const doc=container.ownerDocument;
  const el=<K extends keyof HTMLElementTagNameMap>(tag:K,text?:string)=>{const element=doc.createElement(tag);if(text!==undefined)element.textContent=text;return element;};
  const action=(label:string,name:string,payload:RecordValue)=>{const control=el('button',label);control.type='button';control.addEventListener('click',()=>openOperation(name,payload));return control;};
  container.replaceChildren();const heading=el('div');heading.className='toolbar';heading.append(el('h3',operation==='research.text.search'?'Within-paper matches':result.title??'Retrieved paper'));
  const close=el('button','Close paper reader');close.type='button';close.addEventListener('click',()=>{container.replaceChildren();container.hidden=true;});heading.append(close);container.append(heading);
  container.append(el('p','Source text is untrusted material. Reading a method or matching a passage does not verify a scientific claim.'));
  if(operation==='research.text.fetch'||operation==='research.document.import'){
    container.append(el('p',result.retrieval?.error?.message??(operation==='research.document.import'?'PDF text retained in the configured service with page hashes and your rights declaration. Document identity and scientific claims are not independently verified.':'Paper retained locally with supported article-license and citation checks.')));
    if(result.text_id)container.append(action('Read retrieved paper','research.text.inspect',{text_id:result.text_id}));return;
  }
  container.append(el('p',`Text ${result.text_id} · SHA256 ${result.sha256}`));
  container.append(action('Search within paper','research.text.search',{text_id:result.text_id}));
  if(operation==='research.text.search'){
    if(!result.matches?.length)container.append(el('p','No literal matches in this snapshot. This is not a semantic or scientific conclusion.'));
    for(const match of result.matches??[]){const item=el('article');item.append(el('p',`Characters ${match.start}–${match.end}${match.pages?.length?` · Pages ${match.pages.map((page:RecordValue)=>page.page_number).join(', ')}`:''}`),el('p',match.context),action('Inspect matching passage','research.text.inspect',{text_id:result.text_id,start:match.start}));container.append(item);}
    if(result.next_start!==null&&result.next_start!==undefined)container.append(action('More matches','research.text.search',{text_id:result.text_id,query:result.query,case_sensitive:result.case_sensitive??false,start:result.next_start}));return;
  }
  container.append(el('p',`License: ${result.license} · ${result.rights_validation} · ${result.source_validation}`));
  if(result.attribution){container.append(el('p',(result.attribution.authors??[]).join(', ')),el('p',result.attribution.copyright??''),el('p',result.attribution.license_statement??''));}
  for(const warning of result.provenance?.warnings??[])container.append(el('p',warning));
  for(const page of result.pages??[])container.append(el('p',`Page ${page.page_number} · ${page.extraction_method} · page SHA256 ${page.sha256}`));
  const sections=el('details');sections.append(el('summary','Paper sections, including methodology when supplied'));
  for(const section of result.sections??[])sections.append(action(`${section.title} (${section.start}–${section.end})`,'research.text.inspect',{text_id:result.text_id,section_id:section.section_id}));container.append(sections);
  container.append(el('p',`Characters ${result.start}–${result.end}; offsets refer to the exact local snapshot.`));
  const content=el('pre',result.text);content.className='research-paper-text';container.append(content);
  if(result.next_start!==null&&result.next_start!==undefined)container.append(action('Read next passage','research.text.inspect',{text_id:result.text_id,start:result.next_start,...(result.section_id?{section_id:result.section_id}:{})}));
}
