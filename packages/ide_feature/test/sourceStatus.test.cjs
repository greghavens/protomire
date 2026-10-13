/* Rendered DOM contract tests; these do not claim native GUI acceptance. */
const {test}=require('node:test'),assert=require('node:assert/strict');
const {renderSourceStatus}=require('../out/sourceStatus');
class Element{constructor(tag,doc){this.tagName=tag;this.ownerDocument=doc;this.children=[];this._text='';}set textContent(value){this._text=String(value);this.children=[];}get textContent(){return this._text+this.children.map(child=>child.textContent).join('\n');}append(...children){this.children.push(...children);}replaceChildren(...children){this.children=children;this._text='';}}
const identity='sha256:'+'a'.repeat(64),commit='b'.repeat(40),snapshot='c'.repeat(64),transport='d'.repeat(64);
function setup(status){const doc={createElement:tag=>new Element(tag,doc)},root=new Element('section',doc);return {root,canPin:renderSourceStatus(root,status,commit)};}
const imported={bound:true,available:true,source_kind:'imported_snapshot',binding:{alias:'snapshot',repository_id:identity},repository_id:identity,workspace_path:'/approved/snapshot',declared_source_commit:commit,snapshot_digest:snapshot,transport_recipe_digest:transport,pinned_source_matches:true,operator_trusted:false};
test('snapshot source status visibly separates declared provenance, imported bytes, owner trust and license rights',()=>{
 const {root,canPin}=setup(imported);assert.equal(canPin,false);assert.match(root.textContent,/Bound imported source snapshot/);assert.match(root.textContent,/Source kind: imported_snapshot/);assert.match(root.textContent,/Local snapshot identity/);assert.match(root.textContent,/Declared source commit \(not authenticated Git\)/);assert(root.textContent.includes(identity));assert(root.textContent.includes(snapshot));assert(root.textContent.includes(transport));assert.match(root.textContent,/extracted recipe.json and inventoried file bytes/);assert.match(root.textContent,/Imported source bytes match the current recipe inventory/);assert.match(root.textContent,/Execution is blocked/);assert.match(root.textContent,/explicit owner trust/);assert.match(root.textContent,/grant no asset rights/);assert.match(root.textContent,/separate owner-approved rights review/);assert(!/Checkout HEAD/.test(root.textContent));
});
test('failed imported-byte checks remain visible and do not infer execution or license permission',()=>{
 const {root,canPin}=setup({...imported,pinned_source_matches:false,diagnostics:[{code:'dependency_mismatch',message:'Changed inventoried file'}]});assert.equal(canPin,false);assert.match(root.textContent,/have not been validated/);assert.match(root.textContent,/Changed inventoried file/);assert(!/Imported source bytes match/.test(root.textContent));
});
test('operator trust does not remove the distinct owner-approved rights requirement',()=>{
 const {root}=setup({...imported,operator_trusted:true});assert.match(root.textContent,/each run requires confirmation/);assert.match(root.textContent,/grant no asset rights/);assert.match(root.textContent,/explicit owner trust/);
});
test('Git status retains its pin path and checkout labels while switching source kind clears stale metadata',()=>{
 const {root}=setup(imported);const git={...imported,source_kind:'git',head_commit:commit};assert.equal(renderSourceStatus(root,git,commit),true);assert.match(root.textContent,/Bound source checkout/);assert.match(root.textContent,/Checkout HEAD/);assert.match(root.textContent,/Recipe source commit/);assert(!/Snapshot digest|Imported-byte evidence/.test(root.textContent));assert.equal(renderSourceStatus(root,undefined),false);assert.match(root.textContent,/not provided/);assert(!root.textContent.includes(identity));
});
test('source diagnostics and labels remain inert text and unknown source kinds cannot offer Git pinning',()=>{
 const {root,canPin}=setup({...imported,source_kind:'unknown',binding:{alias:'<script>unsafe</script>',repository_id:identity},diagnostics:[{code:'<img>',message:'<script>never run</script>'}]});assert.equal(canPin,false);assert.match(root.textContent,/<script>unsafe<\/script>/);assert(!root.children.some(item=>item.tagName==='script'||item.tagName==='img'));
});
test('unavailable source without kind metadata never silently becomes a Git checkout',()=>{
 const {root,canPin}=setup({bound:true,available:false,binding:{alias:'snapshot',repository_id:identity},diagnostics:[{code:'source_unavailable',message:'Snapshot missing'}]});assert.equal(canPin,false);assert.match(root.textContent,/Bound project source/);assert.match(root.textContent,/Source kind: unavailable/);assert(!/Bound source checkout|Source kind: git|Checkout HEAD/.test(root.textContent));
});
