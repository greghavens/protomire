export function serviceUrl(raw: string): string {
  const url = new URL(raw);
  if (url.username || url.password || url.search || url.hash) throw new Error('Service URL must not contain credentials, a query or a fragment.');
  const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback)) throw new Error('Use HTTPS for remote services; HTTP is allowed only on loopback.');
  return url.toString().replace(/\/$/, '');
}
export function escapeHtml(value: string): string { return value.replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]!)); }
export const UI_OPERATIONS = new Set([
  'project.history', 'project.update', 'recipe.clone', 'recipe.export', 'recipe.import', 'recipe.resolve_dependencies',
  'evidence.add', 'evidence.get', 'evidence.search', 'claim.create', 'claim.get', 'claim.list', 'claim.update',
  'hypothesis.create', 'hypothesis.get', 'hypothesis.list', 'hypothesis.update', 'decision.record', 'decision.list',
  'execution.targets', 'policy.get', 'cost.estimate', 'run.plan', 'run.submit', 'run.status', 'run.logs', 'run.cancel',
  'run.resume', 'run.artifacts', 'run.compare', 'model.version.list', 'model.version.get', 'model.version.lineage',
  'model.version.promote', 'model.checkpoint.get'
]);
export function checkMessage(raw: unknown): Record<string, any> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('Invalid workbench message.');
  const message = raw as Record<string, any>;
  if (typeof message.type !== 'string' || JSON.stringify(message).length > 2_000_000) throw new Error('Invalid or oversized workbench message.');
  return message;
}
