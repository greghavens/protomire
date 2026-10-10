import { RecordValue } from './types';

function numeric(value:unknown):value is number {return typeof value==='number'&&Number.isFinite(value)&&value>=0;}
export function formatBytes(value:unknown):string {
  if(!numeric(value))return 'Unknown';
  if(value===0)return '0 B';
  const units=['B','KiB','MiB','GiB','TiB','PiB'];const index=Math.max(0,Math.min(Math.floor(Math.log(value)/Math.log(1024)),units.length-1));
  return `${(value/1024**index).toFixed(index?1:0)} ${units[index]}`;
}
function percent(value:unknown):string {return numeric(value)&&value<=100?`${value.toFixed(1)}%`:'Unknown';}
function count(value:unknown):string {return numeric(value)&&Number.isInteger(value)?String(value):'Unknown';}

/** Hardware observations never become target choices or resource reservations. */
export function renderComputeDiagnostics(container:HTMLElement,result:RecordValue):void {
  const doc=container.ownerDocument;container.replaceChildren();
  const add=(tag:string,text:string,className?:string)=>{const element=doc.createElement(tag);element.textContent=text;if(className)element.className=className;container.append(element);};
  add('h3','Discovered service-host hardware');
  const hardware=result.hardware;
  if(!hardware){add('p','Hardware inventory is unavailable from this service. Inspect targets to request a fresh snapshot.','muted');return;}
  add('p',`Scope: operation service host · ${hardware.platform??'Unknown platform'} ${hardware.architecture??''}. This may be a different machine from your IDE.`);
  const freshness=hardware.freshness??{};
  add('p',`Sample: ${hardware.sampled_at??'Unknown'} · ${freshness.cached?'cached':'new'} snapshot · cache age ${numeric(freshness.age_seconds)?freshness.age_seconds.toFixed(1):'Unknown'} s · refresh cache ${count(freshness.cache_ttl_seconds)} s. Values are point-in-time observations.`, 'muted');
  const cpu=hardware.cpu??{},memory=hardware.memory??{};
  add('h4','CPU');add('p',`Logical cores: ${count(cpu.logical_cores)} · Physical cores: ${count(cpu.physical_cores)} · Process affinity cores: ${count(cpu.process_affinity_cores)}`);
  add('p',`CPU utilization: ${percent(cpu.utilization_percent)} · sample window: ${numeric(cpu.sample_window_seconds)?cpu.sample_window_seconds:'Unknown'} s`);
  add('h4','RAM');add('p',`Total: ${formatBytes(memory.total_bytes)} · Available: ${formatBytes(memory.available_bytes)} · Used: ${formatBytes(memory.used_bytes)} · Utilization: ${percent(memory.utilization_percent)}`);
  add('p',hardware.limits?.note??'Host-visible capacity can exceed container quotas and is not reserved for this recipe.','muted');
  const devices=hardware.gpu?.devices??[];
  add('h4',`GPU / display adapters (${devices.length} observed)`);
  if(!devices.length)add('p','No GPU was detected by the available inventory probes. This does not establish that no GPU is installed.','muted');
  for(const gpu of devices){
    add('h4',String(gpu.name??'Unknown GPU'));
    add('p',`Vendor: ${gpu.vendor??'Unknown'} · Source: ${gpu.source??'Unknown'} · Memory type: ${gpu.memory_kind??'unknown'}`);
    add('p',`VRAM total: ${formatBytes(gpu.vram_total_bytes)} · Used: ${formatBytes(gpu.vram_used_bytes)} · Free: ${formatBytes(gpu.vram_free_bytes)} · GPU utilization: ${percent(gpu.utilization_percent)}`);
    if(gpu.memory_kind==='unified')add('p','Unified memory shares system RAM; dedicated VRAM capacity and utilization are unknown.','muted');
  }
  add('p','Unknown means the installed driver, OS or probe did not supply a reliable measurement. Unknown utilization is not zero.','muted');
  add('h4','Runtime and probe diagnostics');
  for(const probe of hardware.probes??[]){
    let text=`${probe.source??'Probe'}: ${probe.status??'unknown'}`;
    if(probe.source==='torch'&&probe.status==='ok')text+=` · GPU API ${probe.cuda_api??'unknown'} · runtime-visible device count: ${count(probe.cuda_visible_devices)} · MPS available: ${probe.mps_available===true?'yes':probe.mps_available===false?'no':'unknown'} · runtime visibility only`;
    if(probe.reason)text+=` · ${probe.reason}`;
    if(probe.unknown_fields?.length)text+=` · unknown: ${probe.unknown_fields.join(', ')}`;
    add('p',text,probe.status==='ok'?'muted':'warning');
  }
  add('h4','Executable targets and resource controls');
  for(const target of result.targets??[])add('p',`${target.id}: ${target.available===true?'available':'unavailable'}${target.unavailable_reason?` · ${target.unavailable_reason}`:''}`);
  add('p',hardware.resource_control?.note??'Hardware discovery does not establish executable targets or enforce reservations.','warning');
  add('p','CPU reservations: unsupported · RAM reservations: unsupported · GPU reservations: unsupported · GPU execution: disabled','warning');
}
