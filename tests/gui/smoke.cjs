/* Real Extension Host integration tests. No mock vscode module or fake kernels. */
const vscode = require('vscode');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');

exports.run = async function () {
  const out = process.env.MODELBUILDER_GUI_EVIDENCE;
  const workspace = vscode.workspace.workspaceFolders?.[0];
  assert(out && workspace, 'Evidence path and real workspace required');
  await fs.mkdir(out, {recursive:true});
  const report = {editor:vscode.version, app:vscode.env.appName, status:'RUNNING', tests:[]};
  report.trusted = vscode.workspace.isTrusted;
  report.extensions = vscode.extensions.all.map(e=>e.id);
  const save = () => fs.writeFile(path.join(out,'smoke-results.json'), JSON.stringify(report,null,2));
  const wait = async (fn, timeout=90000) => {
    const end=Date.now()+timeout;
    while(Date.now()<end){const value=await fn(); if(value)return value;await new Promise(r=>setTimeout(r,500));}
    throw new Error('Timed out waiting for observed runtime result');
  };
  const check = async (name, fn) => {
    if(process.env.MODELBUILDER_GUI_ONLY && !process.env.MODELBUILDER_GUI_ONLY.split(',').includes(name))return;
    await fs.writeFile(path.join(out,'stage.txt'),'running-'+name);
    try {const evidence=await fn();report.tests.push({name,status:'PASS',evidence});}
    catch(e){report.tests.push({name,status:'FAIL',error:String(e.stack||e)});}
    await save();await fs.writeFile(path.join(out,'stage.txt'),name);await new Promise(r=>setTimeout(r,2500));
  };
  const fixture = (f) => path.join(workspace.uri.fsPath,f);
  const python = process.env.MODELBUILDER_GUI_PYTHON || 'python3';
  await vscode.commands.executeCommand('workbench.action.closeAllEditors');
  await vscode.commands.executeCommand('workbench.action.joinAllGroups');
  for(const terminal of vscode.window.terminals)if(terminal.name==='Modelbuilder smoke')terminal.dispose();
  await save();
  await check('python-configuration',async()=>{
    await vscode.workspace.getConfiguration('python').update('defaultInterpreterPath',python,vscode.ConfigurationTarget.Workspace);
    await vscode.workspace.getConfiguration('python').update('languageServer','None',vscode.ConfigurationTarget.Workspace);
    await vscode.workspace.getConfiguration('basedpyright').update('analysis.typeCheckingMode','standard',vscode.ConfigurationTarget.Workspace);
    return {python};
  });
  await check('branded-app',async()=>{assert.match(vscode.env.appName,/Modelbuilder/);return vscode.env.appName;});
  await check('builtin-workbench',async()=>{const e=vscode.extensions.getExtension('modelbuilder.modelbuilder-workbench');assert(e,'Permanent built-in feature absent');await e.activate();assert(e.isActive);return {id:e.id,extensionPath:e.extensionPath};});
  await check('python-edit-and-lsp',async()=>{
    const e=vscode.extensions.getExtension('detachhead.basedpyright');assert(e,'Pinned LSP absent');await e.activate();
    const uri=vscode.Uri.file(fixture('diagnostics.py'));
    await fs.writeFile(uri.fsPath,'count: int = "type_error"\nprint(count)\n');
    const doc=await vscode.workspace.openTextDocument(uri);const editor=await vscode.window.showTextDocument(doc);
    assert(await editor.edit(b=>b.insert(new vscode.Position(2,0),'# edited in real GUI extension host\n')));await doc.save();
    const diagnostics=await wait(()=>vscode.languages.getDiagnostics(uri).filter(d=>d.severity===vscode.DiagnosticSeverity.Error).length && vscode.languages.getDiagnostics(uri));
    return diagnostics.map(d=>({message:d.message,source:d.source,line:d.range.start.line}));
  });
  await check('integrated-terminal',async()=>{
    const marker=fixture('terminal-result.txt');await fs.rm(marker,{force:true});
    const terminal=vscode.window.createTerminal({name:'Modelbuilder smoke',cwd:workspace.uri.fsPath});terminal.show();
    terminal.sendText("printf 'modelbuilder-terminal-ok\\n' > terminal-result.txt");
    await wait(async()=>fs.readFile(marker,'utf8').then(v=>v==='modelbuilder-terminal-ok\n').catch(()=>false),15000);
    return {terminalName:terminal.name,output:await fs.readFile(marker,'utf8')};
  });
  await check('git-integration',async()=>{
    const git=vscode.extensions.getExtension('vscode.git');assert(git);const exports=await git.activate();const api=exports.getAPI(1);
    const repo=await wait(()=>api.repositories.find(r=>r.rootUri.fsPath===workspace.uri.fsPath),15000);
    await repo.status();assert(repo.state.workingTreeChanges.length>0);await vscode.commands.executeCommand('workbench.view.scm');
    return {root:repo.rootUri.toString(),changedFiles:repo.state.workingTreeChanges.map(c=>c.uri.toString())};
  });
  await check('python-debugger',async()=>{
    const debuggerExtension=vscode.extensions.getExtension('ms-python.debugpy');assert(debuggerExtension);await debuggerExtension.activate();
    const program=fixture('debug_smoke.py');const marker=fixture('debug-result.txt');
    await fs.writeFile(program,'from pathlib import Path\nanswer = 6 * 7\nPath(__file__).with_name("debug-result.txt").write_text(str(answer))\n');
    await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(program));
    let started=false,finished=false,stopped;const a=vscode.debug.onDidStartDebugSession(s=>{if(s.name==='Modelbuilder Python debug')started=true;});
    const b=vscode.debug.onDidTerminateDebugSession(s=>{if(s.name==='Modelbuilder Python debug')finished=true;});
    const breakpoint=new vscode.SourceBreakpoint(new vscode.Location(vscode.Uri.file(program),new vscode.Position(2,0)));
    vscode.debug.addBreakpoints([breakpoint]);
    const tracker=vscode.debug.registerDebugAdapterTrackerFactory('debugpy',{createDebugAdapterTracker:()=>({onDidSendMessage:message=>{if(message.type==='event' && message.event==='stopped')stopped=message.body;}})});
    try{assert(await vscode.debug.startDebugging(workspace,{name:'Modelbuilder Python debug',type:'debugpy',request:'launch',program,python,console:'internalConsole',justMyCode:true}));
      await wait(()=>stopped,60000);const session=vscode.debug.activeDebugSession;assert(session);
      const stack=await session.customRequest('stackTrace',{threadId:stopped.threadId,startFrame:0,levels:5});
      const evaluated=await session.customRequest('evaluate',{expression:'answer',frameId:stack.stackFrames[0].id,context:'watch'});
      assert.equal(evaluated.result,'42');
      await fs.writeFile(path.join(out,'stage.txt'),'python-debugger-breakpoint');await new Promise(r=>setTimeout(r,3500));
      await session.customRequest('continue',{threadId:stopped.threadId});
      await wait(()=>finished,60000);assert(started);assert.equal(await fs.readFile(marker,'utf8'),'42');return {started,finished,stoppedReason:stopped.reason,evaluatedAnswer:evaluated.result,result:'42'};
    } finally{a.dispose();b.dispose();tracker.dispose();vscode.debug.removeBreakpoints([breakpoint]);}
  });
  await check('jupyter-kernel',async()=>{
    const jupyter=vscode.extensions.getExtension('ms-toolsai.jupyter');assert(jupyter);await jupyter.activate();
    const file=fixture('smoke.ipynb');
    await fs.writeFile(file,JSON.stringify({cells:[{cell_type:'code',execution_count:null,metadata:{},outputs:[],source:['print(6 * 7)']}],metadata:{kernelspec:{display_name:'Python 3',language:'python',name:'python3'},language_info:{name:'python',version:'3.12'}},nbformat:4,nbformat_minor:5}));
    const notebook=await vscode.workspace.openNotebookDocument(vscode.Uri.file(file));await vscode.window.showNotebookDocument(notebook);
    await vscode.commands.executeCommand('notebook.execute');
    const outputs=await wait(()=>{const values=notebook.cellAt(0).outputs.flatMap(o=>o.items.map(i=>Buffer.from(i.data).toString()));return values.some(v=>v.includes('42'))?values:null;},120000);
    await notebook.save();return {uri:notebook.uri.toString(),outputs};
  });
  report.status=report.tests.every(t=>t.status==='PASS')?'PASS':'FAIL';await save();
  if(report.status!=='PASS')throw new Error('Real GUI smoke failed; inspect smoke-results.json');
};
