// Run with a fresh empty user-data directory and no downloaded extensions.
const vscode=require('vscode');const fs=require('node:fs/promises');const path=require('node:path');const assert=require('node:assert/strict');
exports.run=async()=>{
  const feature=vscode.extensions.getExtension('modelbuilder.modelbuilder-workbench');assert(feature);await feature.activate();
  const expected={'chat.disableAIFeatures':true,'workbench.startupEditor':'none','workbench.secondarySideBar.defaultVisibility':'hidden'};
  const settings={};
  for(const [key,value] of Object.entries(expected)){
    const state=vscode.workspace.getConfiguration().inspect(key);
    assert.equal(state.defaultValue,value);assert.equal(state.globalValue,undefined);assert.equal(vscode.workspace.getConfiguration().get(key),value);
    settings[key]={defaultValue:state.defaultValue,effectiveValue:vscode.workspace.getConfiguration().get(key),userOverride:state.globalValue??null};
  }
  const result={status:'PASS',editor:vscode.version,app:vscode.env.appName,settings,downloadedAssistants:vscode.extensions.all.filter(e=>['openai.chatgpt','Anthropic.claude-code'].includes(e.id)).map(e=>e.id)};
  assert.equal(result.downloadedAssistants.length,0);
  await fs.writeFile(path.join(process.env.MODELBUILDER_GUI_EVIDENCE,'fresh-profile-defaults.json'),JSON.stringify(result,null,2));
  await vscode.commands.executeCommand('modelbuilder.openWorkbench');
};
