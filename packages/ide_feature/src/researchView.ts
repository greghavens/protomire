import { RecordValue } from './types';
export interface ResearchCollections {watches?:RecordValue[];findings?:RecordValue[];proposals?:RecordValue[];errors?:Record<string,string>}
export function renderResearchWorkflow(container:HTMLElement,data:ResearchCollections,available:Set<string>,openOperation:(name:string,defaults?:RecordValue)=>void):void{
  const doc=container.ownerDocument;
  const el=<K extends keyof HTMLElementTagNameMap>(tag:K,text?:string,className?:string)=>{const element=doc.createElement(tag);if(text!==undefined)element.textContent=text;if(className)element.className=className;return element;};
  const action=(label:string,name:string,payload:RecordValue)=>{const control=el('button',label);control.disabled=!available.has(name);control.addEventListener('click',()=>openOperation(name,payload));return control;};
  container.replaceChildren();
  for(const [key,title]of [['watches','Research watches'],['findings','New and changed findings'],['proposals','Staged recipe proposals']] as const){
    const section=el('section',undefined,'research-workflow-section');section.append(el('h3',title));
    if(key==='watches')section.append(el('p','Watches store bounded queries. Refresh is explicit; no background scheduler starts from this screen. Retrieved metadata is a citation, not proof of a claim.','muted'));
    if(data.errors?.[key])section.append(el('p',`Unavailable: ${data.errors[key]}`,'warning'));
    const records=data[key]??[];
    if(!records.length)section.append(el('p',`No ${key} recorded.`, 'muted'));
    for(const record of records){
      const card=el('article',undefined,'research-card');card.dataset.resourceId=record.proposal_id??record.finding_id??record.watch_id;
      const heading=el('div',undefined,'toolbar');heading.append(el('h4',record.name??record.title??record.citation?.title??'Untitled finding'),el('span',record.status??(record.enabled?'enabled':'disabled'),'pill'));card.append(heading);
      const controls=el('div',undefined,'toolbar');
      if(key==='watches'){
        card.append(el('p',record.query),el('p',`Next due: ${record.next_due_at??'unknown'} · ${record.refresh_count??0} refreshes · revision ${record.revision}`,'muted'));
        controls.append(action('Refresh now','research.watch.refresh',{watch_id:record.watch_id,expected_revision:record.revision}),action('Edit watch','research.watch.update',{watch_id:record.watch_id,expected_revision:record.revision,changes:{enabled:record.enabled,name:record.name,query:record.query,interval_seconds:record.interval_seconds}}));
      }else if(key==='findings'){
        card.append(el('p',`${record.kind??'new'} citation · Evidence ${record.evidence_id}`,'mono'),el('p','Scientific claim not verified. Review the source and evaluation evidence separately.','muted'));
        if(record.change_diff){const details=el('details');details.append(el('summary','Changed source metadata'),el('pre',record.change_diff));card.append(details);}
        controls.append(action('Review finding','research.finding.review',{finding_id:record.finding_id,expected_revision:record.revision,status:'reviewed'}),action('Dismiss finding','research.finding.review',{finding_id:record.finding_id,expected_revision:record.revision,status:'dismissed'}),action('Inspect citation','evidence.get',{evidence_id:record.evidence_id}));
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
