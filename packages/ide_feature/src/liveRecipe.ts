import { Recipe } from './types';
export interface LiveEvaluationSettings {enabled:boolean;max_concurrency:number;timeout_seconds:number}
export function liveEvaluationSettings(recipe?:Recipe):LiveEvaluationSettings {
  const value=recipe?.evaluation?.live??{};
  return {enabled:value.enabled===true,max_concurrency:value.max_concurrency??1,timeout_seconds:value.timeout_seconds??300};
}
/** Edit only declared live settings; preserve the rest of the recipe's protocol. */
export function withLiveEvaluationSettings(recipe:Recipe,settings:LiveEvaluationSettings,cadences:Record<string,number>):Recipe {
  if(!Number.isInteger(settings.max_concurrency)||settings.max_concurrency<1||settings.max_concurrency>4)throw new Error('Choose 1 to 4 concurrent evaluations.');
  if(!Number.isFinite(settings.timeout_seconds)||settings.timeout_seconds<=0||settings.timeout_seconds>3600)throw new Error('Evaluation timeout must be greater than 0 and at most 3600 seconds.');
  for(const value of Object.values(cadences))if(!Number.isInteger(value)||value<1||value>100000)throw new Error('Fixture checkpoint cadence must be 1 to 100000 training steps.');
  return {...recipe,evaluation:{...recipe.evaluation,live:{...recipe.evaluation?.live,...settings}},nodes:recipe.nodes.map(node=>node.kind==='model.train'&&node.implementation_ref==='builtin:model.train'&&cadences[node.id]!==undefined?{...node,parameters:{...node.parameters,checkpoint_interval_steps:cadences[node.id]}}:node)};
}
export function liveSettingsIdentity(recipe?:Recipe):string{
  return JSON.stringify({live:recipe?.evaluation?.live??null,cadences:(recipe?.nodes??[]).filter(node=>node.kind==='model.train'&&node.implementation_ref==='builtin:model.train').map(node=>[node.id,node.parameters.checkpoint_interval_steps??1])});
}
export function assertLiveSettingsCurrent(recipe:Recipe,baseline:string):void{
  if(liveSettingsIdentity(recipe)!==baseline)throw new Error('Live evaluation settings changed in the source after this form opened. Copy your form edits if needed, then discard the form edits before applying.');
}
