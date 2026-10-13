/* Actual Chromium DOM with simulated host messages, not native/backend acceptance. */
const {chromium}=require('playwright-core');
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
  const evidence=process.env.MODELBUILDER_GUI_EVIDENCE||path.resolve(__dirname,'../../../evidence/gui/stage-execution-browser');
  await fs.mkdir(evidence,{recursive:true});
  const report={scope:'Chromium DOM with simulated host messages. No native execution, HTTP service, or Code - OSS acceptance is claimed.',status:'RUNNING',checks:[]};
  let browser,page;
  try{
    browser=await chromium.launch({executablePath:process.env.MODELBUILDER_CHROMIUM||'/usr/bin/chromium',headless:true,chromiumSandbox:true,ignoreDefaultArgs:['--password-store=basic','--use-mock-keychain','--unsafely-disable-devtools-self-xss-warnings']});
    page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];page.on('pageerror',error=>errors.push(String(error)));
    await page.setContent('<!doctype html><html><body><div id="app"></div></body></html>');
    await page.evaluate(()=>{window.sent=[];window.acquireVsCodeApi=()=>({postMessage:value=>window.sent.push(value),setState(){},getState(){return {};}});});
    await page.addStyleTag({path:path.join(__dirname,'../media/workbench.css')});await page.addScriptTag({path:path.join(__dirname,'../media/workbench.js')});
    const message=value=>page.evaluate(value=>window.dispatchEvent(new MessageEvent('message',{data:value})),value);
    const recipe={project_id:'p',recipe_id:'r',revision:1,name:'One model recipe',digest:'old',nodes:[],edges:[],environment:{device:'cpu'},evaluation:{live:{enabled:false,max_concurrency:3}}};
    await message({type:'init',catalog:[],projects:[],trusted:true});
    await message({type:'project',snapshot:{project:{project_id:'p',model_name:'One model',revision:1}},section:'Recipe'});
    await message({type:'recipe',recipe});
    await page.locator('#stage-execution-settings summary').click();
    assert.equal(await page.locator('#stage-parallelism').inputValue(),'1');
    assert.match(await page.locator('#stage-execution-capability').textContent(),/has not been checked/);
    await page.locator('#stage-execution-form button[type=submit]').click();
    assert.equal(JSON.parse(await page.locator('#source').inputValue()).execution,undefined);
    report.checks.push('Omitted stage settings display sequential execution and applying the default preserves canonical content.');

    await page.locator('#stage-parallelism').fill('3');
    let before=await page.evaluate(()=>window.sent.filter(value=>value.type==='saveRecipe').length);
    await page.locator('#save').click();await page.keyboard.press('Control+s');
    assert.equal(await page.evaluate(()=>window.sent.filter(value=>value.type==='saveRecipe').length),before);
    assert.match(await page.locator('#error').textContent(),/Apply or discard stage execution settings/);
    await page.locator('#stage-execution-form button[type=submit]').click();
    let draft=JSON.parse(await page.locator('#source').inputValue());assert.deepEqual(draft.execution,{max_parallel_stages:3});
    assert.deepEqual(draft.evaluation,recipe.evaluation);assert.equal(draft.recipe_id,'r');
    await page.locator('#save').click();assert.equal(await page.evaluate(()=>window.sent.filter(value=>value.type==='saveRecipe').length),before+1);
    report.checks.push('The one recipe stores the bound; unapplied settings block button and keyboard saves and leave live evaluation unchanged.');

    await page.locator('#stage-parallelism').fill('4');
    await page.locator('#source').fill(JSON.stringify({...draft,execution:{max_parallel_stages:2},name:'New source title'}));
    const newer=await page.locator('#source').inputValue();
    await page.locator('#stage-execution-form button[type=submit]').click();
    assert.equal(await page.locator('#source').inputValue(),newer);assert.equal(await page.locator('#stage-parallelism').inputValue(),'4');
    assert.match(await page.locator('#error').textContent(),/Stage execution settings changed in source/);
    await page.locator('#reset-stage-execution').click();assert.equal(await page.locator('#stage-parallelism').inputValue(),'2');
    await page.locator('#stage-parallelism').fill('8');await page.locator('#stage-execution-form button[type=submit]').click();
    draft=JSON.parse(await page.locator('#source').inputValue());assert.equal(draft.name,'New source title');assert.equal(draft.execution.max_parallel_stages,8);
    report.checks.push('Stale panels cannot replace source edits; explicit discard reloads the latest bound while unrelated source fields survive.');

    await page.locator('#stage-parallelism').fill('');
    const state=await page.evaluate(()=>window.sent.filter(value=>value.type==='draftState').at(-1).state);
    await message({type:'recipe',recipe:draft});await message({type:'restoreDraft',state});
    assert.equal(await page.locator('#stage-parallelism').inputValue(),'');
    await page.locator('#save').click();assert.match(await page.locator('#error').textContent(),/Apply or discard stage execution settings/);
    await page.locator('#source').fill('{"unfinished":');await page.locator('#reset-stage-execution').click();
    assert.equal(await page.locator('#source').inputValue(),'{"unfinished":');assert.equal(await page.locator('#stage-parallelism').inputValue(),'');
    assert.match(await page.locator('#error').textContent(),/Fix the source JSON/);
    await page.locator('#source').fill(JSON.stringify(draft));await page.locator('#reset-stage-execution').click();assert.equal(await page.locator('#stage-parallelism').inputValue(),'8');
    report.checks.push('Incomplete panel input survives recovery, and invalid source cannot be overwritten by apply or discard.');

    for(const bad of ['0','9','1.5']){
      await page.locator('#stage-parallelism').fill(bad);
      await page.locator('#stage-execution-form').evaluate(form=>form.dispatchEvent(new Event('submit',{cancelable:true})));
      assert.equal(JSON.parse(await page.locator('#source').inputValue()).execution.max_parallel_stages,8);
      assert.match(await page.locator('#error').textContent(),/whole number from 1 to 8/);
    }
    await message({type:'busy',busy:true});assert.equal(await page.locator('#stage-parallelism').isDisabled(),true);
    await message({type:'busy',busy:false});assert.equal(await page.locator('#stage-parallelism').isDisabled(),false);
    report.checks.push('Both programmatic submissions and HTML controls reject invalid bounds; busy state disables and restores the panel.');

    await message({type:'capabilities',projectId:'other',result:{capabilities:{control_flow:{independent_stage_concurrency:true},recipe_settings:{execution:{max_parallel_stages:{supported_maximum:8}}}}}});
    assert.match(await page.locator('#stage-execution-capability').textContent(),/has not been checked/);
    await message({type:'capabilities',projectId:'p',result:{capabilities:{control_flow:{independent_stage_concurrency:false}}}});
    assert.match(await page.locator('#stage-execution-capability').textContent(),/does not advertise/);
    await message({type:'capabilities',projectId:'p',result:{capabilities:{control_flow:{independent_stage_concurrency:true},recipe_settings:{execution:{max_parallel_stages:{supported_maximum:8}}}}}});
    assert.match(await page.locator('#stage-execution-capability').textContent(),/supports up to 8/);
    await page.screenshot({path:path.join(evidence,'stage-execution-settings.png'),fullPage:true});
    await message({type:'project',snapshot:{project:{project_id:'new',model_name:'New model',revision:1}}});
    assert.match(await page.locator('#stage-execution-capability').textContent(),/has not been checked/);
    await message({type:'recipe',recipe:{...recipe,recipe_id:'new-recipe',project_id:'new'}});
    assert.equal(await page.locator('#stage-parallelism').inputValue(),'1');assert.match(await page.locator('#stage-execution-capability').textContent(),/has not been checked/);
    assert.equal(await page.locator('#recipe-select,#model-select').count(),0);
    report.checks.push('Capabilities remain truthful and project-scoped; switching projects drops stale drafts/capabilities without adding model or recipe choosers.');
    assert.deepEqual(errors,[]);report.status='PASS';
  }catch(error){report.status=page?'FAIL':'BLOCKED';report.error=String(error);throw error;}
  finally{await fs.writeFile(path.join(evidence,'results.json'),JSON.stringify(report,null,2)+'\n');await browser?.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
