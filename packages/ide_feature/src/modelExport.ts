import * as vscode from 'vscode';
import {createHash,randomUUID} from 'node:crypto';
import {open,rename,unlink,lstat,realpath} from 'node:fs/promises';
import {dirname,join,basename} from 'node:path';
import {RecordValue,ServiceTransport} from './types';

const MAX_BYTES=16*1024**3+16*1024**2,CHUNK_BYTES=4*1024**2;
const fileIdentity=async(path:string)=>{try{const stat=await lstat(path,{bigint:true});if(stat.isSymbolicLink())throw new Error('Choose a regular local file, not a symlink.');return {device:stat.dev,inode:stat.ino,size:stat.size,modified:stat.mtimeNs,changed:stat.ctimeNs,mode:stat.mode};}catch(error){if((error as NodeJS.ErrnoException).code==='ENOENT')return undefined;throw error;}};
const sameIdentity=(left:Awaited<ReturnType<typeof fileIdentity>>,right:Awaited<ReturnType<typeof fileIdentity>>)=>left===undefined?right===undefined:right!==undefined&&Object.entries(left).every(([key,value])=>right[key as keyof typeof right]===value);
const sha=(bytes:Uint8Array)=>createHash('sha256').update(bytes).digest('hex');
export async function saveNativeModel(client:ServiceTransport,selection:RecordValue,assertCurrent:()=>void):Promise<string|undefined>{
  assertCurrent();
  const listed=await client.invoke('model.export.list',selection);assertCurrent();
  const exports:RecordValue[]=listed.exports??[];
  if(!exports.length)throw new Error(`This selection has no verified existing model export. ${(listed.diagnostics??[]).map((item:RecordValue)=>item.message).join('; ')}`);
  const choice=await vscode.window.showQuickPick<vscode.QuickPickItem&{item:RecordValue}>(exports.map((item:RecordValue)=>({label:`${item.provenance.node_id} · ${item.provenance.artifact_id}`,description:`${item.total_asset_bytes} bytes · checkpoint ${item.manifest.selected_checkpoint}`,item})),{title:'Save an existing model export',placeHolder:'Choose the verified artifact and selected checkpoint'});
  if(!choice)return;assertCurrent();
  const selected=(choice as {item:RecordValue}).item,p=selected.provenance;
  const identity={project_id:selection.project_id,backend_run_id:p.backend_run_id,model_version_id:p.model_version_id,artifact_id:p.artifact_id};
  if(p.project_id!==selection.project_id||(selection.backend_run_id&&p.backend_run_id!==selection.backend_run_id)||(selection.model_version_id&&p.model_version_id!==selection.model_version_id))throw new Error('Export ownership differs from the selected run or version.');
  const destination=await vscode.window.showSaveDialog({title:'Save model package',saveLabel:'Save model package',filters:{'Model package':['zip']}});
  if(!destination)return;assertCurrent();
  if(destination.scheme!=='file')throw new Error('Choose a local file destination for this model package.');
  const parent=await realpath(dirname(destination.fsPath)),parentStat=await lstat(parent,{bigint:true}),target=join(parent,basename(destination.fsPath));
  const original=await fileIdentity(target);assertCurrent();
  const assertDestination=async()=>{const currentParent=await realpath(dirname(destination.fsPath));const stat=await lstat(parent,{bigint:true});if(currentParent!==parent||stat.isSymbolicLink()||stat.dev!==parentStat.dev||stat.ino!==parentStat.ino)throw new Error('Save directory changed during transfer. Choose the destination again.');if(!sameIdentity(original,await fileIdentity(target)))throw new Error('The destination changed during transfer. Its contents were preserved; choose a new destination.');};
  const temporary=join(parent,`.${basename(destination.fsPath)}.${randomUUID()}.partial`);
  let handle:Awaited<ReturnType<typeof open>>|undefined;
  try{
    return await vscode.window.withProgress({location:vscode.ProgressLocation.Notification,title:'Saving verified model package',cancellable:true},async(progress,cancellation)=>{
      const check=()=>{assertCurrent();if(cancellation.isCancellationRequested)throw new Error('Model save cancelled.');};
      check();const transfer=await client.invoke('model.export.package',{...identity,expected_export_digest:selected.export_digest});check();
      if(transfer.chunk_bytes!==CHUNK_BYTES||!Number.isSafeInteger(transfer.size_bytes)||transfer.size_bytes<=0||transfer.size_bytes>MAX_BYTES||!Array.isArray(transfer.chunk_sha256)||transfer.chunk_sha256.length!==Math.ceil(transfer.size_bytes/CHUNK_BYTES)||!/^[a-f0-9]{64}$/.test(transfer.sha256)||!/^[a-f0-9]{64}$/.test(transfer.package_id))throw new Error('Invalid or oversized model transfer descriptor.');
      for(const [key,value]of Object.entries(identity))if(transfer.provenance?.[key]!==value)throw new Error('Package ownership changed during save.');
      if(transfer.export_digest!==selected.export_digest)throw new Error('Export changed during save. Inspect it again.');
      handle=await open(temporary,'wx',0o600);const full=createHash('sha256');let total=0;
      for(let index=0;index<transfer.chunk_sha256.length;index++){
        check();const chunk=await client.invoke('model.export.read',{...identity,package_id:transfer.package_id,chunk_index:index});check();
        if(typeof chunk.data_base64!=='string'||chunk.data_base64.length>Math.ceil(CHUNK_BYTES/3)*4)throw new Error('Oversized model transfer chunk.');
        const bytes=Buffer.from(chunk.data_base64,'base64'),expected=Math.min(CHUNK_BYTES,transfer.size_bytes-total);
        if(chunk.package_id!==transfer.package_id||chunk.chunk_index!==index||bytes.length!==expected||sha(bytes)!==transfer.chunk_sha256[index]||chunk.sha256!==transfer.chunk_sha256[index])throw new Error('Model package chunk failed integrity verification.');
        let offset=0;while(offset<bytes.length){const {bytesWritten}=await handle.write(bytes,offset);if(!bytesWritten)throw new Error('Model package write made no progress.');offset+=bytesWritten;}
        full.update(bytes);total+=bytes.length;progress.report({increment:100/transfer.chunk_sha256.length,message:`${Math.round(total/1024)} KiB verified`});
      }
      if(total!==transfer.size_bytes||full.digest('hex')!==transfer.sha256)throw new Error('Model package failed final integrity verification.');
      await handle.sync();await handle.close();handle=undefined;check();await assertDestination();check();await rename(temporary,target);
      return destination.fsPath;
    });
  }finally{await handle?.close();await unlink(temporary).catch(()=>{});}
}
