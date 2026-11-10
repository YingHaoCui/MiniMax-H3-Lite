/* Non-destructive crop editor. Geometry is stored in EXIF-oriented image coordinates. */
(function(){
'use strict';
function centered(iw,ih,ratio){var w=Math.min(iw,ih*ratio),h=w/ratio;return {x:(iw-w)/2,y:(ih-h)/2,w:w,h:h};}
function normalized(r,im){return {x:r.x/im.width,y:r.y/im.height,width:r.w/im.width,height:r.h/im.height};}
function fromNormalized(c,im){return {x:c.x*(im.naturalWidth||im.width),y:c.y*(im.naturalHeight||im.height),w:c.width*(im.naturalWidth||im.width),h:c.height*(im.naturalHeight||im.height)};}
function fit(r,iw,ih){r.w=Math.min(iw,r.w);r.h=Math.min(ih,r.h);r.x=Math.max(0,Math.min(iw-r.w,r.x));r.y=Math.max(0,Math.min(ih-r.h,r.y));return r;}
window.FrameEditor={centered:centered,normalized:normalized,open:function(options){
  var dialog=document.createElement('dialog');dialog.className='frame-editor';
  dialog.innerHTML='<form method="dialog"><header><strong>编辑'+options.label+' · 裁剪取景</strong><button value="cancel" aria-label="关闭">×</button></header></form><div class="fe-controls"><label>画面比例 <select id="feAspect"></select></label><label>输出分辨率 <select id="feResolution"></select></label><output id="feDimensions"></output></div><p>拖动框内移动取景；拖动四角或边中点缩放裁剪框，始终保持所选比例。滚轮可缩放取景范围。</p><canvas tabindex="0" aria-label="图像裁剪画布，方向键移动，加减键缩放"></canvas><p>首尾帧共用比例与尺寸。更改后另一帧将重置为居中裁剪，可点击该帧重新调整。</p><footer><button type="button" id="feReset">居中重置</button><button type="button" id="feCancel">取消</button><button type="button" id="feSave" disabled>保存取景</button></footer><div role="status" id="feStatus">正在加载原图…</div>';
  document.body.appendChild(dialog);
  var previous=document.activeElement,aspect=dialog.querySelector('#feAspect'),res=dialog.querySelector('#feResolution'),canvas=dialog.querySelector('canvas'),ctx=canvas.getContext('2d'),im=new Image(),rect,drag,scale=1,padX=0,padY=0,busy=false;
  ['16:9','21:9','4:3','1:1','3:4','9:16'].forEach(function(v){aspect.add(new Option(v,v));});
  ['0.98','0.9','0.8','0.7','0.6','0.5','0.4'].forEach(function(v){res.add(new Option(v+'M',v));});
  aspect.disabled=res.disabled=true;
  aspect.value=options.aspect||'16:9';res.value=options.resolution||'0.98';if(!res.value)res.value='0.98';
  function dims(){return options.dimensions(res.value,aspect.value);}
  function ratio(){var d=dims();return d[0]/d[1];}
  function handles(){return [[rect.x,rect.y,'nw'],[rect.x+rect.w/2,rect.y,'n'],[rect.x+rect.w,rect.y,'ne'],[rect.x+rect.w,rect.y+rect.h/2,'e'],[rect.x+rect.w,rect.y+rect.h,'se'],[rect.x+rect.w/2,rect.y+rect.h,'s'],[rect.x,rect.y+rect.h,'sw'],[rect.x,rect.y+rect.h/2,'w']];}
  function draw(){
    if(!rect)return;
    canvas.width=900;canvas.height=500;scale=Math.min(880/im.width,480/im.height);padX=(900-im.width*scale)/2;padY=(500-im.height*scale)/2;
    ctx.fillStyle='#101317';ctx.fillRect(0,0,900,500);ctx.drawImage(im,padX,padY,im.width*scale,im.height*scale);
    var x=padX+rect.x*scale,y=padY+rect.y*scale,w=rect.w*scale,h=rect.h*scale;
    ctx.fillStyle='rgba(0,0,0,.6)';ctx.beginPath();ctx.rect(0,0,900,500);ctx.rect(x,y,w,h);ctx.fill('evenodd');ctx.strokeStyle='#fff';ctx.lineWidth=2;ctx.strokeRect(x,y,w,h);
    ctx.lineWidth=.5;for(var i=1;i<3;i++){ctx.beginPath();ctx.moveTo(x+w*i/3,y);ctx.lineTo(x+w*i/3,y+h);ctx.moveTo(x,y+h*i/3);ctx.lineTo(x+w,y+h*i/3);ctx.stroke();}
    ctx.fillStyle='#fff';handles().forEach(function(p){ctx.fillRect(padX+p[0]*scale-5,padY+p[1]*scale-5,10,10);});
    var d=dims();dialog.querySelector('#feDimensions').textContent=d[0]+' × '+d[1]+' px';
  }
  function reset(){if(!im.width)return;rect=centered(im.width,im.height,ratio());draw();}
  function zoom(f){var w=Math.max(Math.min(im.width,im.height*ratio())*.02,Math.min(rect.w*f,im.width,im.height*ratio())),h=w/ratio();rect=fit({x:rect.x+(rect.w-w)/2,y:rect.y+(rect.h-h)/2,w:w,h:h},im.width,im.height);draw();}
  function point(e){var b=canvas.getBoundingClientRect();return {x:((e.clientX-b.left)*900/b.width-padX)/scale,y:((e.clientY-b.top)*500/b.height-padY)/scale};}
  canvas.onpointerdown=function(e){if(!rect||busy)return;var p=point(e),hit=handles().find(function(h){return Math.hypot(p.x-h[0],p.y-h[1])*scale<18;});if(!hit&&(p.x<rect.x||p.y<rect.y||p.x>rect.x+rect.w||p.y>rect.y+rect.h))return;drag={p:p,r:Object.assign({},rect),handle:hit?hit[2]:'move'};canvas.setPointerCapture(e.pointerId);e.preventDefault();};
  canvas.onpointermove=function(e){if(!drag)return;var p=point(e),r=drag.r,dx=p.x-drag.p.x,dy=p.y-drag.p.y,h=drag.handle;
    if(h==='move')rect=fit({x:r.x+dx,y:r.y+dy,w:r.w,h:r.h},im.width,im.height);
    else{
      var ax=h.includes('w')?r.x+r.w:h.includes('e')?r.x:r.x+r.w/2,ay=h.includes('n')?r.y+r.h:h.includes('s')?r.y:r.y+r.h/2;
      var w=h==='n'||h==='s'?(r.h+(h==='n'?-dy:dy))*ratio():r.w+(h.includes('w')?-dx:dx);
      if(h.length===2)w=r.w+((h.includes('w')?-dx:dx)+(h.includes('n')?-dy:dy)/ratio())/(1+1/(ratio()*ratio()));
      var maxW=(h==='n'||h==='s')?2*Math.min(ax,im.width-ax):h.includes('w')?ax:im.width-ax;
      var maxH=(h==='e'||h==='w')?2*Math.min(ay,im.height-ay):h.includes('n')?ay:im.height-ay;
      w=Math.max(1,Math.min(w,maxW,maxH*ratio()));var height=w/ratio();
      rect={x:h.includes('w')?ax-w:h.includes('e')?ax:ax-w/2,y:h.includes('n')?ay-height:h.includes('s')?ay:ay-height/2,w:w,h:height};
    }draw();};
  canvas.onpointerup=canvas.onpointercancel=function(){drag=null;};
  canvas.addEventListener('wheel',function(e){if(rect&&!busy){e.preventDefault();zoom(e.deltaY>0?1.08:.92);}},{passive:false});
  canvas.onkeydown=function(e){if(!rect||busy)return;if(e.key==='+'||e.key==='=')zoom(.95);else if(e.key==='-')zoom(1.05);else if(/^Arrow/.test(e.key)){rect.x+=e.key==='ArrowLeft'?-im.width/200:e.key==='ArrowRight'?im.width/200:0;rect.y+=e.key==='ArrowUp'?-im.height/200:e.key==='ArrowDown'?im.height/200:0;fit(rect,im.width,im.height);draw();}else return;e.preventDefault();};
  aspect.onchange=res.onchange=reset;dialog.querySelector('#feReset').onclick=reset;
  dialog.querySelector('#feCancel').onclick=function(){if(!busy)dialog.close();};
  dialog.oncancel=function(e){if(busy)e.preventDefault();};
  dialog.onclose=function(){dialog.remove();if(previous)previous.focus();};
  dialog.querySelector('form').onsubmit=function(e){if(busy)e.preventDefault();};
  dialog.querySelector('#feSave').onclick=async function(){if(busy||!rect)return;busy=true;dialog.querySelectorAll('button,select').forEach(function(b){b.disabled=true;});try{await options.save({aspect:aspect.value,resolution:res.value,crop:normalized(rect,im)});dialog.close();}catch(e){dialog.querySelector('#feStatus').textContent='保存失败：'+e.message;}finally{busy=false;dialog.querySelectorAll('button,select').forEach(function(b){b.disabled=false;});}};
  im.onload=function(){aspect.disabled=res.disabled=false;rect=options.crop?fromNormalized(options.crop,im):centered(im.width,im.height,ratio());draw();dialog.querySelector('#feSave').disabled=false;dialog.querySelector('#feStatus').textContent='原图 '+im.width+' × '+im.height+' · 保存不会覆盖原图';};
  im.onerror=function(){dialog.querySelector('#feStatus').textContent='原图加载失败，请关闭后重试';};
  dialog.showModal();im.src=options.url;
},preview:function(img,crop,dims){img.onload=function(){var r=crop?fromNormalized(crop,img):centered(img.naturalWidth,img.naturalHeight,dims[0]/dims[1]);var c=document.createElement('canvas');c.width=320;c.height=Math.round(320*dims[1]/dims[0]);c.getContext('2d').drawImage(img,r.x,r.y,r.w,r.h,0,0,c.width,c.height);c.style.cssText='width:100%;height:100%;object-fit:contain';img.replaceWith(c);};}};
var style=document.createElement('style');style.textContent='.frame-editor{width:min(940px,94vw);max-height:94vh;overflow:auto;background:#20242b;color:#eee;border:1px solid #59616d;border-radius:12px;padding:20px}.frame-editor::backdrop{background:#000a}.frame-editor header,.frame-editor footer,.fe-controls{display:flex;align-items:center;gap:16px;flex-wrap:wrap}.frame-editor header{justify-content:space-between}.frame-editor footer{justify-content:flex-end}.frame-editor p,.frame-editor [role=status]{font-size:12px;color:#bcc4ce;margin:12px 0}.frame-editor canvas{display:block;width:100%;height:auto;touch-action:none;cursor:move}.frame-editor button,.frame-editor select{padding:8px;border:1px solid #707782;border-radius:5px;background:#303843;color:white}.frame-editor button:disabled{opacity:.5}.fe-controls{margin-top:16px}.fs-thumb[role=button]{cursor:crosshair}';document.head.appendChild(style);
})();
