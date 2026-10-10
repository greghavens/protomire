import { Recipe, RecipeNode, RecordValue, ServiceTransport } from './types';
import { blankComponent } from './componentAuthoring';
/** Keeps the revision read by this editor. Saving never silently re-reads/rebases. */
export class RecipeDocument {
  current?: Recipe;
  constructor(private readonly client: ServiceTransport) {}
  async load(projectId: string, recipeId: string): Promise<Recipe> {
    return this.current = await this.client.invoke<Recipe>('recipe.get', {project_id:projectId, recipe_id:recipeId});
  }
  async save(draft: Recipe, key: string): Promise<Recipe> {
    if (!this.current) throw new Error('Open a recipe first.');
    if (draft.recipe_id !== this.current.recipe_id || draft.project_id !== this.current.project_id) throw new Error('Recipe identity cannot be changed in an editor.');
    const saved = await this.client.invoke<Recipe>('recipe.update', {project_id:this.current.project_id, recipe_id:this.current.recipe_id, expected_revision:this.current.revision, recipe:draft, idempotency_key:key});
    this.current = saved;
    return saved;
  }
  async validate(draft: Recipe): Promise<RecordValue> {
    if (!this.current) throw new Error('Open a recipe first.');
    return this.client.invoke('recipe.validate',{project_id:this.current.project_id,recipe_id:this.current.recipe_id,recipe:draft});
  }
  async diff(draft: Recipe): Promise<RecordValue> {
    if (!this.current) throw new Error('Open a recipe first.');
    return this.client.invoke('recipe.diff',{project_id:this.current.project_id,recipe_id:this.current.recipe_id,recipe:draft});
  }
}
/** Pure authoring transforms preserve unknown fields and full Python references. */
export function replaceNode(recipe: Recipe, node: RecipeNode): Recipe { return {...recipe,nodes:recipe.nodes.map(existing => existing.id === node.id ? structuredClone(node) : existing)}; }
export function removeNode(recipe: Recipe, nodeId: string): Recipe { if(recipe.nodes.some(node=>node.condition?.source_node===nodeId))throw new Error('Remove the terminal condition before deleting its boolean gate.');return {...recipe,nodes:recipe.nodes.filter(node => node.id !== nodeId),edges:recipe.edges.filter(edge => edge.source_node !== nodeId && edge.target_node !== nodeId)}; }
export function newNode(id: string, kind: string, commit: string): RecipeNode {
  return {id,kind,...(kind==='component.run'?{component:blankComponent()}:{}),implementation_ref:kind==='component.run'?{module:'',entrypoint:'',commit}:kind === 'code.python' ? {module:'my_model',entrypoint:'run',commit} : `builtin:${kind}`,inputs:[],outputs:[],parameters:{},environment_ref:'default',resource_request:{},retry:{max_attempts:1,backoff_seconds:0},timeout:300,cache_policy:'disabled',provenance:{}};
}
export function addNode(recipe:Recipe,id:string,kind:string):Recipe {
  if(!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(id))throw new Error('Choose a valid unique node ID.');
  if(recipe.nodes.some(node=>node.id===id))throw new Error('That node ID is already in this recipe. Choose a fresh node ID.');
  return {...recipe,nodes:[...recipe.nodes,newNode(id,kind,recipe.source?.commit??'')]};
}
/** Visual edits cannot erase an invalid source draft, nor commit a stale node panel. */
export function readVisualSource(source: string): Recipe {
  let parsed: Recipe;
  try { parsed=JSON.parse(source); } catch { throw new Error('Fix the source JSON before changing the graph. Your source draft has been preserved.'); }
  if (!parsed || !Array.isArray(parsed.nodes) || !Array.isArray(parsed.edges)) throw new Error('Source must contain nodes and edges arrays before graph editing.');
  return parsed;
}
export function assertNodePanelCurrent(current: RecipeNode, baseline: string): void {
  if (JSON.stringify(current)!==baseline) throw new Error('This node changed in the source after the panel opened. Copy your panel changes if needed, then use Discard panel edits / reload node before applying.');
}
/** The response digest is computed metadata, not a field in the editable body schema. */
export function editableRecipeSource(recipe: Recipe): string {
  const body:RecordValue={...recipe};delete body.digest;
  return JSON.stringify(body,null,2)+'\n';
}
/** Resolve only the model's one current recipe, never choose among unrelated recipes. */
export function currentProjectRecipe(snapshot: RecordValue): Recipe {
  const current = snapshot.recipe ?? (snapshot.recipes?.length === 1 ? snapshot.recipes[0] : undefined);
  if (!current || current.project_id !== snapshot.project?.project_id || (snapshot.project.active_recipe_id && current.recipe_id !== snapshot.project.active_recipe_id)) {
    throw new Error('The service must provide the one current recipe for this model project.');
  }
  return current;
}
