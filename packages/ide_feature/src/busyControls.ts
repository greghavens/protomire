type Control=HTMLButtonElement|HTMLInputElement|HTMLTextAreaElement|HTMLSelectElement;
/** Busy state must not enable semantically disabled controls when an operation finishes. */
export class BusyControls{
  private prior=new Map<Control,boolean>();
  private busy=false;
  constructor(private root:Pick<Document,'querySelectorAll'>){}
  set(value:boolean){
    this.busy=value;
    if(value){this.refresh();return;}
    for(const [control,disabled]of this.prior)control.disabled=disabled;
    this.prior.clear();
  }
  refresh(){if(!this.busy)return;for(const control of this.root.querySelectorAll<Control>('button,input,textarea,select')){if(!this.prior.has(control))this.prior.set(control,control.disabled);control.disabled=true;}}
}
