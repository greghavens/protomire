import { RecordValue } from './types';
/** Authoring affordances only. Execution admission remains exclusively service-owned. */
export const AUTHORABLE_KINDS=['dataset.import','dataset.version.import','dataset.synthetic','dataset.validate','model.initialize','model.train','model.evaluate','model.compare','model.checkpoint','model.export','code.python','component.run'];
export function executableKindChoices(capabilities?:RecordValue):string[]{
  const declared=capabilities?.executable_kinds;
  return Array.isArray(declared)?AUTHORABLE_KINDS.filter(kind=>declared.includes(kind)):[...AUTHORABLE_KINDS];
}
export function admissionMessages(plan:RecordValue):string[]{
  const messages:string[]=[];
  for(const issue of plan.blocked_reasons??[]){messages.push(`${issue.details?.node_id?`Node ${issue.details.node_id}: `:''}${issue.message??issue.code??'Execution is blocked.'}`);}
  const resources=plan.capabilities?.node_settings?.resource_request;
  if(resources?.cpu==='unsupported'||resources?.memory_mb==='unsupported')messages.push('This local target cannot enforce CPU or memory reservations. Recipes requesting those limits are rejected. Edit and save a new recipe revision to remove unsupported requests; historical revisions remain unchanged.');
  if(plan.capabilities?.unsupported_schema_kinds?.['training.kl_anchor'])messages.push(`Standalone KL-anchor nodes are not executable: ${plan.capabilities.unsupported_schema_kinds['training.kl_anchor']}. Existing recipe nodes remain visible for review.`);
  return messages;
}
