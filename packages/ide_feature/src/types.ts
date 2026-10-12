/** Transport projections, not alternate validators. The service owns the schema. */
export type Json = null | boolean | number | string | Json[] | {[key: string]: Json};
export type RecordValue = Record<string, any>;
export interface Port { name: string; type: string }
export interface NodeCondition { source_node: string; source_port: string; equals: boolean }
export interface RecipeNode extends RecordValue { id: string; kind: string; inputs: Port[]; outputs: Port[]; parameters: RecordValue; condition?: NodeCondition }
export interface RecipeEdge { source_node: string; source_port: string; target_node: string; target_port: string }
export interface BoundedIteration {id:string;node_ids:string[];max_iterations:number;state:(RecipeEdge & {resume_mode?:"warm_start"})[];until?:NodeCondition}
export interface Recipe extends RecordValue { recipe_id: string; project_id: string; name: string; revision: number; digest: string; nodes: RecipeNode[]; edges: RecipeEdge[]; bounded_iterations?:BoundedIteration[] }
export interface OperationSpec { authorized?:boolean; name: string; scope: string; description: string; mutating: boolean; input_schema: { properties: RecordValue; required: string[] } }
export interface ServiceTransport { invoke<T = RecordValue>(operation: string, payload: RecordValue): Promise<T>; catalog(): Promise<OperationSpec[]> }
export interface Project extends RecordValue { project_id: string; model_name: string; revision: number }
