(function(root){'use strict';
const screens=guided=>guided?['assistant','goal','start','project']:['assistant','project'];
function validate(step,draft,folder){
 if(step==='assistant'&&!['none','codex','claude','triggerfish','opencode','kilocode'].includes(draft.assistant))return 'Choose a supported coding assistant, or continue without one.';
 if(step==='goal'&&(!draft.goal.trim()||draft.goal.length>8000))return 'Describe your model’s goal in 1–8,000 characters.';
 if(step==='start'&&!['research','base_model','scratch'].includes(draft.startingPoint))return 'Choose research, an existing base model, or building from scratch.';
 if(step==='project'){
  if(!draft.modelName.trim()||draft.modelName.length>200)return 'Enter a model name of 1–200 characters.';
  if(!/^[a-z0-9][a-z0-9-]{0,79}$/.test(draft.slug))return 'Use a lowercase project slug with letters, numbers and hyphens (up to 80 characters).';
  if(!folder)return 'Choose this project’s local folder before reviewing it.';
 }
}
function move(step,direction,draft,folder){const route=screens(draft.quickstart),index=route.indexOf(step);if(index<0)throw Error('Unknown wizard screen.');const error=direction==='next'?validate(step,draft,folder):undefined;if(error)return {step,error};return {step:route[Math.max(0,Math.min(route.length-1,index+(direction==='next'?1:-1)))]};}
const api={screens,validate,move};if(typeof module==='object'&&module.exports)module.exports=api;else root.ModelbuilderProjectWizard=api;
})(globalThis);
