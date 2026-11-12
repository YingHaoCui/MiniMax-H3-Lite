const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(__dirname,'_rendered/images.html'),'utf8');
const script = fs.readFileSync(path.join(root,'assets/js/images.js'),'utf8');
const settle = () => new Promise(resolve => setTimeout(resolve,30));

async function boot(withImage = false, resume = false) {
  const dom = new JSDOM(html,{url:'http://localhost/images',runScripts:'outside-only'});
  const win = dom.window, calls = [];
  const refs = withImage ? [{id:'r1',kind:'image',path:'uploads/ref.png',name:'参考图'}] : [];
  const shot = {id:'s',mode:'image',name:'创作',prompt:'透明背景的花',params:{seed:0,ref_ids:refs.map(r => r.id)},refs,
    latest_task:resume ? {id:'old',status:'processing'} : null};
  win.fetch = async (url, options = {}) => {
    calls.push({url,options});
    let data;
    if (url === '/api/images/config') data = {model:'env-configured-model',steps:32,max_references:4,max_upload_mb:200};
    else if (url === '/api/projects') data = [{id:'p',name:'项目'}];
    else if (url.endsWith('/shots')) data = [shot,{id:'video',mode:'text',name:'视频'}];
    else if (url.endsWith('/history')) data = [];
    else if (url === '/api/shots/s') {data = JSON.parse(options.body); Object.assign(shot,data);}
    else if (url === '/api/image-generations') data = {id:'task',status:'pending',payload:{params:{seed:0}}};
    else if (url.startsWith('/api/generations/')) data = {id:'task',status:'completed',result_path:'assets/image.png'};
    else throw Error('Unexpected request: ' + url);
    return {ok:true,json:async () => data};
  };
  win.eval(script); await settle();
  return {dom,win,calls};
}

(async () => {
  for (const withImage of [false,true]) {
    const {dom,win,calls} = await boot(withImage);
    const $ = id => win.document.getElementById(id);
    assert.equal($('model').textContent,'env-configured-model');
    assert.equal($('creation').options.length,1);
    assert.equal($('steps').value,'32');
    assert.match($('mode').textContent,withImage ? /TI2I/ : /T2I/);
    $('image-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await settle();
    const submitted = calls.filter(c => c.url === '/api/image-generations');
    assert.equal(submitted.length,1);
    const body = JSON.parse(submitted[0].options.body);
    assert.deepEqual(body.ref_ids,withImage ? ['r1'] : []);
    assert.equal(body.seed,0);
    assert.equal(body.model,undefined);
    assert.equal($('result').hidden,false);
    assert.equal($('download').getAttribute('href'),'/assets/image.png');
    assert.equal($('generate').disabled,false);
    dom.window.close();
  }
  const {dom,calls} = await boot(true,true);
  assert(calls.some(c => c.url === '/api/generations/old'));
  assert(!calls.some(c => c.url === '/api/image-generations'));
  dom.window.close();
  console.log('Image UI passed: unified T2I/TI2I, env model, submission, archive preview, refresh recovery.');
})().catch(error => {console.error(error); process.exitCode = 1;});
