import * as vscode from 'vscode';
import {createHash,randomUUID} from 'node:crypto';
import {constants} from 'node:fs';
import {lstat,open} from 'node:fs/promises';
import {basename} from 'node:path';
import {RecordValue,ServiceTransport} from './types';

const MAX_BYTES=8*1024*1024;
const sameFile=(left:import('node:fs').BigIntStats,right:import('node:fs').BigIntStats)=>
  left.dev===right.dev&&left.ino===right.ino&&left.size===right.size&&left.mtimeNs===right.mtimeNs&&left.ctimeNs===right.ctimeNs;

/** Bounded, same-handle local read. Bytes and the local path never enter a webview. */
export async function readResearchPdf(path:string,maxBytes:number):Promise<Buffer>{
  if(!Number.isSafeInteger(maxBytes)||maxBytes<1024||maxBytes>MAX_BYTES)throw new Error('Invalid document byte limit.');
  const before=await lstat(path,{bigint:true});
  if(!before.isFile()||before.isSymbolicLink())throw new Error('Choose a regular local PDF file, not a symlink or device.');
  if(before.size<=0n||before.size>BigInt(maxBytes))throw new Error(`The PDF exceeds the ${maxBytes}-byte document limit or is empty.`);
  const file=await open(path,constants.O_RDONLY|(constants.O_NOFOLLOW??0)|(constants.O_NONBLOCK??0));
  try{
    const opened=await file.stat({bigint:true});if(!opened.isFile()||!sameFile(before,opened))throw new Error('The selected PDF changed before it could be read.');
    const chunks:Buffer[]=[];let size=0;
    while(size<=maxBytes){const buffer=Buffer.alloc(Math.min(65536,maxBytes+1-size));const {bytesRead}=await file.read(buffer,0,buffer.length,null);if(!bytesRead)break;size+=bytesRead;if(size>maxBytes)throw new Error('The selected PDF exceeded its byte limit while reading.');chunks.push(buffer.subarray(0,bytesRead));}
    const after=await file.stat({bigint:true});if(!sameFile(opened,after)||BigInt(size)!==opened.size)throw new Error('The selected PDF changed while it was being read.');
    const bytes=Buffer.concat(chunks,size);if(bytes.subarray(0,5).toString('ascii')!=='%PDF-')throw new Error('The selected file does not contain a supported PDF header.');return bytes;
  }finally{await file.close();}
}

/** Explicit native review before the only byte-bearing service request. */
export async function importResearchPdf(client:ServiceTransport,selection:RecordValue,endpoint:string,assertCurrent:()=>void):Promise<RecordValue|undefined>{
  assertCurrent();
  const capabilities=await client.invoke('research.document.capabilities',{project_id:selection.project_id});assertCurrent();
  if(!capabilities.available)throw new Error('This service cannot extract PDFs. It requires Linux with local Poppler tools. You can still import authorized source text.');
  const limit=capabilities.max_document_bytes;
  if(!Number.isSafeInteger(limit)||limit<1024||limit>MAX_BYTES)throw new Error('The service returned an invalid document limit.');
  const evidence=await client.invoke('evidence.get',{project_id:selection.project_id,evidence_id:selection.evidence_id});assertCurrent();
  if(evidence.project_id!==selection.project_id||evidence.evidence_id!==selection.evidence_id||evidence.revision!==selection.expected_evidence_revision)throw new Error('The citation changed. Refresh saved evidence before importing a PDF.');
  const picked=await vscode.window.showOpenDialog({title:`Import authorized PDF for ${evidence.title}`,openLabel:'Review selected PDF',canSelectMany:false,canSelectFiles:true,canSelectFolders:false,filters:{'PDF documents':['pdf']}});assertCurrent();
  if(!picked?.[0])return;
  if(picked[0].scheme!=='file')throw new Error('Choose a local PDF file. URLs and remote filesystem providers are not imported.');
  const filename=basename(picked[0].fsPath);
  if(filename.length>200||!filename.toLowerCase().endsWith('.pdf')||!/^([^/\\\u0000-\u001f\u007f])+\.[pP][dD][fF]$/.test(filename))throw new Error('Choose a PDF with a filename of at most 200 characters.');
  const bytes=await readResearchPdf(picked[0].fsPath,limit);assertCurrent();
  const rights=await vscode.window.showQuickPick([
    {label:'I authored this document',value:'self_authored'},
    {label:'This document is public domain',value:'public_domain'},
    {label:'Its license permits this retention and extraction',value:'licensed'},
    {label:'I have permission to retain and extract it',value:'permission'}
  ],{title:'Confirm document rights',placeHolder:'Public availability alone does not establish permission'});assertCurrent();if(!rights)return;
  const license=await vscode.window.showInputBox({title:'Document license or permission',prompt:'Record the exact license, public-domain basis or permission you have.',ignoreFocusOut:true,validateInput:value=>!value.trim()||value.length>300?'Enter a nonblank license or permission description, up to 300 characters.':undefined});assertCurrent();if(license===undefined)return;
  if(!license.trim()||license.length>300)throw new Error('A nonblank license or permission description is required.');
  const modes=[{label:'Extract embedded text only',value:'disabled',detail:'Scanned/image-only pages may contain no readable text.'}];
  if(capabilities.ocr_available)modes.push({label:'Local OCR for pages without embedded text',value:'auto',detail:'English Tesseract on this service. Maximum 20 OCR pages. Review recognition errors.'},{label:'Local OCR for every page',value:'always',detail:'English Tesseract on this service. Maximum 20 pages. May replace accurate embedded text.'});
  const mode=await vscode.window.showQuickPick(modes,{title:'PDF extraction mode',placeHolder:'OCR is optional and never sent to a cloud OCR provider'});assertCurrent();if(!mode)return;
  const sha=createHash('sha256').update(bytes).digest('hex');
  const consent=await vscode.window.showWarningMessage(`Import “${filename}” (${bytes.length} bytes) for citation “${evidence.title}”? The PDF bytes will be sent to your configured ModelBuilder service at ${endpoint} for local extraction. Its extracted text, page hashes, citation, and rights declaration will remain in that service’s project store; the original PDF is temporary. Rights: ${license.trim()}. Mode: ${mode.label}. SHA-256: ${sha}. You confirm that this is the cited document and that you may retain and extract it. OCR and passage matches do not verify scientific claims.`,{modal:true},'Import authorized PDF');assertCurrent();
  if(consent!=='Import authorized PDF')return;
  const currentBytes=await readResearchPdf(picked[0].fsPath,limit);assertCurrent();
  if(createHash('sha256').update(currentBytes).digest('hex')!==sha)throw new Error('The selected PDF changed during review. Choose and review it again before importing.');
  // One request key survives the whole request. Never retry an uncertain upload.
  return client.invoke('research.document.import',{project_id:selection.project_id,evidence_id:evidence.evidence_id,
    expected_evidence_revision:evidence.revision,filename,media_type:'application/pdf',content_base64:bytes.toString('base64'),
    expected_sha256:sha,license:license.trim(),rights_basis:rights.value,rights_confirmed:true,ocr_mode:mode.value,idempotency_key:randomUUID()});
}
