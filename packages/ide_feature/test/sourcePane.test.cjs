/* Layout controller unit tests; actual Electron coverage lives in guiDom.cjs. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {bindRecipeSourcePane}=require('../out/sourcePane');

function harness(hidden=false) {
  const classes=new Set();
  const source={value:'{"nodes": [',selectionStart:4,selectionEnd:9,scrollTop:160};
  const graph={transform:'translate(50 25) scale(1.2)',selectedNode:'custom'};
  const doc={activeElement:source};
  const pane={hidden,ownerDocument:doc,contains:element=>element===source};
  const attributes=new Map();
  const listeners=new Map();
  const toggle={textContent:'',setAttribute:(key,value)=>attributes.set(key,value),
    focus:()=>{doc.activeElement=toggle;},
    addEventListener:(name,listener)=>listeners.set(name,listener)};
  const grid={classList:{toggle:(name,enabled)=>{if(enabled)classes.add(name);else classes.delete(name);}}};
  bindRecipeSourcePane(grid,pane,toggle);
  return {source,graph,doc,pane,toggle,attributes,classes,click:()=>listeners.get('click')()};
}

test('visible pane has an accessible collapse control and expands the graph on collapse',()=>{
  const h=harness();
  assert.equal(h.attributes.get('aria-expanded'),'true');
  assert.equal(h.toggle.textContent,'Hide JSON');
  h.click();
  assert.equal(h.pane.hidden,true);
  assert.equal(h.classes.has('source-collapsed'),true);
  assert.equal(h.attributes.get('aria-expanded'),'false');
  assert.equal(h.toggle.textContent,'Show JSON');
  assert.equal(h.doc.activeElement,h.toggle);
});

test('repeated collapse and restore leave invalid source, selection, scroll and graph state untouched',()=>{
  const h=harness();
  const sourceBefore={...h.source},graphBefore={...h.graph};
  for(let i=0;i<10;i++){
    h.click();
    assert.equal(h.pane.hidden,true);
    h.click();
    assert.equal(h.pane.hidden,false);
    assert.equal(h.classes.has('source-collapsed'),false);
    assert.equal(h.attributes.get('aria-expanded'),'true');
    assert.equal(h.toggle.textContent,'Hide JSON');
    assert.deepEqual(h.source,sourceBefore);
    assert.deepEqual(h.graph,graphBefore);
  }
});

test('initially hidden pane can be expanded without moving unrelated focus',()=>{
  const h=harness(true);
  const otherControl={};
  h.doc.activeElement=otherControl;
  h.click();
  assert.equal(h.pane.hidden,false);
  assert.equal(h.doc.activeElement,otherControl);
  h.click();
  assert.equal(h.pane.hidden,true);
  assert.equal(h.doc.activeElement,otherControl);
});

test('collapsed CSS has a single flexible track and the toggle stays outside the pane',()=>{
  const fs=require('node:fs'),path=require('node:path');
  const css=fs.readFileSync(path.join(__dirname,'../media/workbench.css'),'utf8');
  const webview=fs.readFileSync(path.join(__dirname,'../src/webview.ts'),'utf8');
  assert.match(css,/\.editor-grid\.source-collapsed\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\)\s*\}/);
  const toggle=webview.indexOf('id="toggle-source"');
  const pane=webview.indexOf('id="recipe-source-pane"');
  assert(toggle>0&&toggle<pane);
  assert.match(webview,/id="toggle-source"[^>]*type="button"[^>]*aria-expanded="true"[^>]*aria-controls="recipe-source-pane"/);
  assert(webview.indexOf('id="dirty"')<pane);
});
