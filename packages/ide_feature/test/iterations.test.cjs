const {test}=require('node:test');const assert=require('node:assert/strict');
const {iterationIdentity,withIterationDraft,iterationNodeLabel,iterationRunSummary}=require('../out/iterations');
const {removeNode}=require('../out/document');
const recipe={recipe_id:'r',project_id:'p',revision:3,nodes:[{id:'train'}],edges:[],future:{retained:true}};
const blocks=[{id:'refine',node_ids:['train'],max_iterations:3,state:[]}];
test('iteration edits preserve one canonical recipe and unknown fields',()=>{const changed=withIterationDraft(recipe,JSON.stringify(blocks),iterationIdentity(recipe));assert.deepEqual(changed.bounded_iterations,blocks);assert.deepEqual(changed.future,recipe.future);assert.equal(changed.recipe_id,'r');assert.equal(recipe.bounded_iterations,undefined);assert.match(iterationNodeLabel(changed,'train'),/refine.*3 iterations/);});
test('stale iteration panel and malformed JSON preserve draft',()=>{assert.throws(()=>withIterationDraft({...recipe,bounded_iterations:blocks},'[]','[]'),/changed in source/);assert.throws(()=>withIterationDraft(recipe,'{}','[]'),/array/);assert.throws(()=>withIterationDraft(recipe,'bad','[]'),SyntaxError);});
test('deleting an iteration node requires explicit block change',()=>{assert.throws(()=>removeNode({...recipe,bounded_iterations:blocks},'train'),/iteration block/);});
test('summary uses native observed iterations without declaring pending complete',()=>{assert.equal(iterationRunSummary({}),'');assert.match(iterationRunSummary({bounded_iterations:{refine:{max_iterations:3,iterations:[{status:'completed'},{status:'incomplete'}]}}}),/1\/3 iterations completed/);assert.match(iterationRunSummary({bounded_iterations:{refine:{max_iterations:3,iterations:[{status:'completed',stop_reason:'until'}]}}}),/stopped by until/);});
