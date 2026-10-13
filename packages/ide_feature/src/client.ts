import { serviceUrl } from './security';
import { OperationSpec, RecordValue, ServiceTransport } from './types';
export class OperationError extends Error {
  constructor(readonly status: number, readonly code: string, message: string, readonly details: any = {}) { super(message); this.name = 'OperationError'; }
}
export class OperationClient implements ServiceTransport {
  constructor(private readonly getUrl: () => string, private readonly getToken: (url: string) => PromiseLike<string | undefined>, private readonly request: typeof fetch = fetch, private readonly getConnection?: () => Promise<{url:string;token:string}>) {}
  endpoint(): string { return serviceUrl(this.getUrl()); }
  private async send(path: string, payload?: RecordValue): Promise<any> {
    const managed = await this.getConnection?.();
    const base = serviceUrl(managed?.url ?? this.getUrl());
    const token = managed?.token ?? await this.getToken(base);
    if (!token) throw new OperationError(401, 'not_configured', 'Configure the ModelBuilder service token first.');
    let response: Response;
    try { response = await this.request(`${base}${path}`, {method:payload ? 'POST' : 'GET', redirect:'error', headers:{'Authorization':`Bearer ${token}`,'Content-Type':'application/json'}, body:payload ? JSON.stringify(payload) : undefined, signal:AbortSignal.timeout(['/model.export.package','/research.document.import'].some(suffix=>path.endsWith(suffix))?120_000:30_000)}); }
    catch { throw new OperationError(0, 'connection_failed', this.getConnection ? 'The bundled core connection ended. Retry after the local session restarts.' : 'Cannot reach the operation service. Check its URL and that the service is running.'); }
    let body: any;
    try { body = await response.json(); } catch { throw new OperationError(response.status, 'invalid_response', 'Operation service returned a non-JSON response.'); }
    if (!response.ok) throw new OperationError(response.status, body.error?.code ?? 'operation_failed', body.error?.message ?? `Operation failed (${response.status})`, body.error?.details);
    return body;
  }
  async invoke<T = RecordValue>(operation: string, payload: RecordValue): Promise<T> {
    if (!/^[a-z][a-z0-9_.]+$/.test(operation)) throw new Error('Invalid operation name.');
    return this.send(`/v1/operations/${operation}`, payload);
  }
  async catalog(): Promise<OperationSpec[]> { const result=await this.send('/v1/catalog');return Array.isArray(result.granted_scopes)?result.operations.map((item:RecordValue)=>({...item,authorized:(item.required_scopes??[item.scope]).every((scope:string)=>result.granted_scopes.includes(scope))})):result.operations; }
}
