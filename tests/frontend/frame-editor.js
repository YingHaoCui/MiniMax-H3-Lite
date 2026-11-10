const {JSDOM}=require('jsdom');
const fs=require('fs');
const assert=require('node:assert/strict');
(async()=>{
 const dom=new JSDOM('<button id="origin">edit</button>',{runScripts:'dangerously'}),w=dom.window;
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
 w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
 w.HTMLCanvasElement.prototype.getContext=()=>new Proxy({},{get:()=>()=>{}});
 w.HTMLCanvasElement.prototype.setPointerCapture=()=>{};
 w.HTMLCanvasElement.prototype.getBoundingClientRect=()=>({left:0,top:0,width:900,height:500});
 w.Image=class{constructor(){this.width=this.naturalWidth=1200;this.height=this.naturalHeight=800;}set src(v){this.onload();}};
 w.eval(fs.readFileSync('assets/js/frame-editor.js','utf8'));
 let saved;
 function open(){w.FrameEditor.open({label:'首帧',url:'/test.png',aspect:'16:9',resolution:'0.98',dimensions:(r,a)=>a==='9:16'?[768,1344]:[1344,768],save:async v=>{saved=v;}});}
 open();const d=w.document,c=d.querySelector('canvas');
 d.querySelector('#feAspect').value='9:16';d.querySelector('#feAspect').dispatchEvent(new w.Event('change'));
 assert.match(d.querySelector('#feDimensions').textContent,/768 × 1344/);
 // Right edge of centered portrait crop. Drag inward: left edge remains anchored.
 c.onpointerdown({clientX:587.14,clientY:250,pointerId:1,preventDefault(){}});
 c.onpointermove({clientX:550,clientY:250});c.onpointerup();
 c.onkeydown({key:'ArrowRight',preventDefault(){}});
 await d.querySelector('#feSave').onclick();
 assert.equal(saved.aspect,'9:16');
 assert.ok(saved.crop.width<0.381);
 assert.ok(Math.abs(saved.crop.width*1200/(saved.crop.height*800)-768/1344)<1e-9);
 assert.ok(saved.crop.x>=0&&saved.crop.x+saved.crop.width<=1);
 assert.equal(d.querySelector('dialog'),null);
 const previous=saved;open();d.querySelector('#feCancel').click();assert.equal(saved,previous);
 console.log('Frame editor: portrait ratio, edge resize, keyboard movement, save and cancel passed');dom.window.close();
})().catch(e=>{console.error(e);process.exit(1);});
