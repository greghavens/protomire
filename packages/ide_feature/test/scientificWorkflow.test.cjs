const assert=require('node:assert/strict');
const {test}=require('node:test');
class Element{constructor(tag,doc){this.tagName=tag;this.ownerDocument=doc;this.children=[];this.value='';this.listeners={};this._text='';}set textContent(value){this._text=String(value);this.children=[];}get textContent(){return this._text+this.children.map(item=>item.textContent).join(' ');}append(...children){this.children.push(...children);}replaceChildren(...children){this.children=children;this._text='';}setAttribute(){}addEventListener(name,fn){this.listeners[name]=fn;}click(){this.listeners.click();}}
const flatten=node=>[node,...node.children.flatMap(flatten)];
const {smokeRequest,scientificDecisionFields,renderScientificWorkflow}=require('../out/scientificWorkflow');
const recipe={project_id:'p',recipe_id:'r',revision:7,nodes:[{id:'train',component:{capabilities:{smoke_budget:{limits:[{id:'steps',unit:'updates',minimum:1}]}}}}]};
test('smoke requests preserve immutable revision and reject incomplete budgets',()=>{
  assert.deepEqual(smokeRequest(recipe,[{node_id:'train',limit_id:'steps',unit:'updates',maximum:3}]),{project_id:'p',recipe_id:'r',expected_revision:7,limits:[{node_id:'train',limit_id:'steps',unit:'updates',maximum:3}]});
  assert.throws(()=>smokeRequest(recipe,[]));assert.throws(()=>smokeRequest(recipe,[{node_id:'train',limit_id:'steps',maximum:1.2}]));
});
test('failed or unverified scientific evidence cannot become accepted decision fields',()=>{
  assert.throws(()=>scientificDecisionFields({compatible:true,scientific_result:{passed:false}}));
  assert.throws(()=>scientificDecisionFields({compatible:true,scientific_result:{passed:true,native_verified:false}}));
});
test('native action panel dispatches declared smoke plan and methods without guessed thresholds',()=>{
  const doc={createElement:tag=>new Element(tag,doc)},root=new Element('main',doc),calls=[];
  renderScientificWorkflow(root,{project:{project_id:'p'},recipe,runs:[]},(op,payload)=>calls.push({op,payload}));
  flatten(root).find(x=>x.tagName==='input'&&x.type==='checkbox').checked=true;flatten(root).find(x=>x.tagName==='input'&&x.type==='number').value='3';
  flatten(root).filter(x=>x.tagName==='button').find(b=>b.textContent==='Plan smoke run').click();
  assert.equal(calls[0].op,'run.smoke.plan');assert.equal(calls[0].payload.expected_revision,7);
  flatten(root).filter(x=>x.tagName==='button').find(b=>b.textContent==='Inspect scientific methods').click();assert.equal(calls[1].op,'scientific.methods');
  assert.equal(flatten(root).find(x=>x.tagName==='textarea').value,'');
});
