export function serviceUrl(raw: string): string {
  const url = new URL(raw);
  if (url.username || url.password || url.search || url.hash) throw new Error('Service URL must not contain credentials, a query or a fragment.');
  const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback)) throw new Error('Use HTTPS for remote services; HTTP is allowed only on loopback.');
  return url.toString().replace(/\/$/, '');
}
export function escapeHtml(value: string): string { return value.replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]!)); }
export const UI_OPERATIONS = new Set([
  'component.list', 'component.get',
  'scientific.methods', 'scientific.policy.register', 'scientific.policy.get', 'scientific.policy.activate', 'scientific.sample.plan', 'run.smoke.plan', 'recipe.smoke.prepare',
  'dataset.register', 'dataset.version', 'dataset.bind', 'dataset.get', 'dataset.list', 'dataset.validate', 'dataset.preview',
  'research.watch.create', 'research.watch.get', 'research.watch.list', 'research.watch.update', 'research.watch.refresh', 'research.watch.refresh_due', 'research.watch.schedule_enable', 'research.watch.schedule_disable',
  'research.finding.list', 'research.finding.review', 'research.text.import', 'research.text.get', 'research.text.fetch', 'research.text.list', 'research.text.search', 'research.text.inspect',
  'research.document.capabilities',
  'research.proposal.create', 'research.proposal.list', 'research.proposal.get', 'research.proposal.review', 'research.proposal.apply',
  'project.history', 'project.update', 'recipe.resolve_dependencies', 'recipe.history', 'recipe.get', 'recipe.diff',
  'evidence.add', 'evidence.get', 'evidence.search', 'claim.create', 'claim.get', 'claim.list', 'claim.update',
  'hypothesis.create', 'hypothesis.get', 'hypothesis.list', 'hypothesis.update', 'decision.record', 'decision.list',
  'execution.targets', 'policy.get', 'cost.estimate', 'run.plan', 'run.submit', 'run.status', 'run.logs', 'run.cancel',
  'run.evaluations.list', 'run.events.list', 'run.resume', 'run.artifacts', 'run.compare', 'model.version.list', 'model.version.get', 'model.version.lineage',
  'model.version.promote', 'model.checkpoint.get'
]);
export function checkMessage(raw: unknown): Record<string, any> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('Invalid workbench message.');
  const message = raw as Record<string, any>;
  if (typeof message.type !== 'string' || JSON.stringify(message).length > 2_000_000) throw new Error('Invalid or oversized workbench message.');
  if(message.type==='importResearchDocument'){
    const allowed=new Set(['type','projectId','evidenceId','expectedEvidenceRevision']);
    if(Object.keys(message).some(key=>!allowed.has(key))||typeof message.projectId!=='string'||!message.projectId||typeof message.evidenceId!=='string'||!message.evidenceId||!Number.isSafeInteger(message.expectedEvidenceRevision)||message.expectedEvidenceRevision<1)throw new Error('Select a current saved citation before importing a PDF.');
  }
  if (message.type === 'recipeHistory') {
    const allowed = new Set(['type', 'action', 'projectId', 'recipeId', 'expectedRevision', 'requestId', 'sourceRevision', 'fromRevision', 'toRevision']);
    const positive = (value: unknown) => Number.isSafeInteger(value) && Number(value) > 0;
    if (Object.keys(message).some(key => !allowed.has(key)) || !['list', 'inspect', 'diff', 'restore'].includes(message.action)
      || typeof message.projectId !== 'string' || !message.projectId || typeof message.recipeId !== 'string' || !message.recipeId
      || !positive(message.expectedRevision) || !positive(message.requestId)
      || (['inspect', 'restore'].includes(message.action) && !positive(message.sourceRevision))
      || (message.action === 'diff' && (!positive(message.fromRevision) || !positive(message.toRevision)))) {
      throw new Error('Invalid recipe history request. Reopen revision history in the current model project.');
    }
  }
  if(message.type==='pinRecipeSource'){
    const allowed=new Set(['type','projectId','recipeId','expectedRevision','expectedProjectRevision','sourceRepositoryId']);
    if(Object.keys(message).some(key=>!allowed.has(key))||typeof message.projectId!=='string'||!message.projectId||typeof message.recipeId!=='string'||!message.recipeId||!Number.isSafeInteger(message.expectedRevision)||message.expectedRevision<1||!Number.isSafeInteger(message.expectedProjectRevision)||message.expectedProjectRevision<1||typeof message.sourceRepositoryId!=='string'||!/^sha256:[0-9a-f]{64}$/.test(message.sourceRepositoryId))throw new Error('Invalid source pin request. Reopen the current model project.');
  }
  return message;
}
