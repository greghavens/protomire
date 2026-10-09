// Compatibility probe only. Never signs in, sends prompts, or claims MCP parity.
const vscode = require('vscode');
const fs = require('node:fs/promises');
const path = require('node:path');
exports.run = async () => {
  const out = process.env.MODELBUILDER_GUI_EVIDENCE;
  const report = {app:vscode.env.appName,editor:vscode.version,status:'UNVERIFIED_AUTHENTICATED_MCP',extensions:[]};
  for (const id of ['openai.chatgpt','Anthropic.claude-code']) {
    const extension = vscode.extensions.getExtension(id);
    const result = {id,installed:!!extension,authenticated:false,mcp_calls:'UNVERIFIED'};
    if (extension) {
      result.version=extension.packageJSON.version;
      result.requiredEngine=extension.packageJSON.engines.vscode;
      try {
        await extension.activate(); result.activated=extension.isActive;
        result.registeredCommands=(await vscode.commands.getCommands(true)).filter(command=>command.startsWith(id==='openai.chatgpt'?'chatgpt.':'claude-vscode.'));
      } catch(error) {result.activated=false;result.error=String(error.stack||error);}
    }
    report.extensions.push(result);
  }
  await fs.writeFile(path.join(out,'assistant-activation-results.json'),JSON.stringify(report,null,2));
  await vscode.commands.executeCommand('chatgpt.openSidebar');
};
