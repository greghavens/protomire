// Use a normal development window: upstream --extensionTestsPath intentionally
// discards persisted workspace trust and cannot validate installed Python tools.
const vscode = require('vscode');
const path = require('node:path');
const fs = require('node:fs/promises');
exports.activate = () => {
  if (process.env.MODELBUILDER_GUI_SMOKE_RUN !== '1') return;
  setTimeout(async () => {
    try {
      if (!vscode.workspace.isTrusted) {
        await new Promise(resolve => {const d=vscode.workspace.onDidGrantWorkspaceTrust(()=>{d.dispose();resolve();});});
      }
      const suite=process.env.MODELBUILDER_GUI_SUITE === 'workbench'
        ? require('../../packages/ide_feature/test/guiParity.cjs')
        : process.env.MODELBUILDER_GUI_SUITE === 'execution' ? require('../../packages/ide_feature/test/guiRun.cjs')
        : process.env.MODELBUILDER_GUI_SUITE === 'defaults' ? require('./defaults.cjs')
        : process.env.MODELBUILDER_GUI_SUITE === 'assistants' ? require('./assistants.cjs') : require('./smoke.cjs');
      await suite.run();
    } catch (error) {
      await fs.writeFile(path.join(process.env.MODELBUILDER_GUI_EVIDENCE,'runner-error.json'),JSON.stringify({status:'FAIL',error:String(error.stack||error)},null,2));
    } finally {
      if (process.env.MODELBUILDER_GUI_HOLD_OPEN !== '1') await vscode.commands.executeCommand('workbench.action.quit');
    }
  },3000);
};
