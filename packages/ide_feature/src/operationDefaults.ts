import { Recipe, RecordValue } from './types';

/** Convenience form values; the operation service remains the schema authority. */
export function operationDefaults(recipe?:Recipe, activeRun?:string):RecordValue {
  return {recipe_id:recipe?.recipe_id,expected_revision:recipe?.revision,recipe_revision:recipe?.revision,
    operation_id:activeRun,target:'local-cpu',trusted:true,mode:'exact_state',
    acceptance_metric:{name:'validation_loss',direction:'minimize'},
    selection_rule:{metric:'validation_loss',direction:'minimize'},outcome:'inconclusive',
    evidence_ids:[],claim_ids:[],run_ids:activeRun?[activeRun]:[],changes:{}};
}
