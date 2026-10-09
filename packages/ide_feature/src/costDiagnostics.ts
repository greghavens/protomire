import { RecordValue } from './types';
/** Never round ledger money through JavaScript Number. Currency scale is provider-declared. */
export function exactMinorUnits(value:unknown):string {
  if(typeof value!=='string'||!/^\d+$/.test(value))return 'Unavailable';
  return BigInt(value).toString().replace(/\B(?=(\d{3})+(?!\d))/g,',');
}
export function renderCostDiagnostics(container:HTMLElement,result:RecordValue):void {
  const doc=container.ownerDocument;container.replaceChildren();const add=(tag:string,text:string,className?:string)=>{const el=doc.createElement(tag);el.textContent=text;if(className)el.className=className;container.append(el);return el;};
  add('h3','Remote admission and cost ledger');add('p','Read-only diagnostics. Remote execution is disabled; no provider spend or budget enforcement is claimed.','warning');
  const providers=result.providers;if(providers){add('p',`Provider: ${providers.configured?'configured':'not configured'} · adapter: ${providers.adapter_kind??'unavailable'} · remote acceptance: ${providers.remote_acceptance??'BLOCKED'}`);for(const blocker of providers.blockers??[])add('p',typeof blocker==='string'?blocker:`${blocker.code??''}: ${blocker.message??JSON.stringify(blocker)}`,'muted');}
  for(const [key,error]of Object.entries(result.errors??{}))add('p',`${key}: ${error}`,'warning');
  const reservations=result.ledger?.reservations??[];add('h4',`Reservations (${reservations.length})`);
  if(!reservations.length)add('p','No visible reservations.','muted');
  for(const entry of reservations){add('h4',String(entry.reservation_id??'Reservation'));add('p',`Execution: ${entry.execution_state??'unknown'} · cleanup: ${entry.cleanup_state??'unknown'} · billing: ${entry.billing_state??'unknown'} · evidence: ${entry.evidence_kind??'unspecified'}`);add('pre',JSON.stringify(entry,null,2));}
  const balances=result.policy?.balances??[];for(const balance of balances){add('h4','Exact ledger balances (minor units)');for(const key of ['exposure_minor_units','actual_minor_units','held_minor_units','available_minor_units','debt_minor_units','authorization_overrun_minor_units'])if(key in balance)add('p',`${key.replaceAll('_',' ')}: ${exactMinorUnits(balance[key])}`);}
  const events=result.history?.events??[];add('h4',`Recent ledger events (${events.length})`);add('pre',JSON.stringify(events,null,2));
}
