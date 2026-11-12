'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let config, shots = [], current = null, refs = [], selected = [], timer, loading = false;
  const memoryKey = 'mmh3_image_project';
  async function api(path, options = {}) {
    const response = await fetch('/api' + path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data));
    return data;
  }
  const json = body => ({method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  function status(message) { $('status').textContent = message; }
  function busy(value) {
    loading = value;
    for (const id of ['generate','references','project','creation','new-project','new-creation','prompt','width','height','steps','seed']) $(id).disabled = value;
  }
  function showResult(path) {
    $('result').hidden = !path; $('empty').hidden = !!path; $('download').hidden = !path;
    if (path) { $('result').src = '/' + path; $('download').href = '/' + path; }
    else { $('result').removeAttribute('src'); $('download').removeAttribute('href'); }
  }
  function parameters() {
    return {width:Number($('width').value), height:Number($('height').value), steps:Number($('steps').value),
      seed:$('seed').value === '' ? null : Number($('seed').value), ref_ids:selected.slice()};
  }
  function restoreParams(params = {}) {
    for (const key of ['width','height','steps','seed']) {
      $(key).value = params[key] ?? ({width:1024,height:1024,steps:config.steps,seed:''}[key]);
    }
  }
  async function saveDraft() {
    if (!current) return;
    const opts = json({prompt:$('prompt').value,params:parameters()}); opts.method = 'PUT';
    const target = current;
    const saved = await api('/shots/' + target.id, opts);
    target.prompt = saved.prompt; target.params = saved.params;
  }
  function renderRefs() {
    $('mode').textContent = selected.length ? 'TI2I · 参考图生成' : 'T2I · 文生图';
    $('reference-list').replaceChildren();
    const ordered = selected.map(id => refs.find(r => r.id === id)).filter(Boolean).concat(refs.filter(r => !selected.includes(r.id)));
    for (const ref of ordered) {
      const card = document.createElement('div'); card.className = 'ref';
      const image = document.createElement('img'); image.src = '/' + ref.path; image.alt = ref.name || '参考图';
      const label = document.createElement('label');
      const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = selected.includes(ref.id);
      checkbox.disabled = loading;
      label.append(checkbox, selected.includes(ref.id) ? '参考图 ' + (selected.indexOf(ref.id) + 1) : '不参与本次生成');
      checkbox.addEventListener('change', async () => {
        if (checkbox.checked && selected.length >= config.max_references) {checkbox.checked = false; status('最多选择 4 张参考图'); return;}
        selected = checkbox.checked ? selected.concat(ref.id) : selected.filter(id => id !== ref.id);
        renderRefs(); try { await saveDraft(); } catch (error) {status(error.message);}
      });
      const move = document.createElement('button'); move.type = 'button'; move.textContent = '← 向前';
      move.disabled = loading || selected.indexOf(ref.id) <= 0;
      move.addEventListener('click', async () => {
        const i = selected.indexOf(ref.id); [selected[i-1],selected[i]] = [selected[i],selected[i-1]];
        renderRefs(); try {await saveDraft();} catch (error) {status(error.message);}
      });
      card.append(image,label,move); $('reference-list').append(card);
    }
  }
  async function history() {
    const pid = $('project').value;
    const items = await api('/projects/' + pid + '/history');
    if (pid !== $('project').value) return;
    $('history-list').replaceChildren();
    for (const item of items.filter(i => ['t2i','ti2i'].includes(i.mode))) {
      const card = document.createElement('button'); card.type = 'button'; card.className = 'history-card';
      if (item.result_path) {const img = document.createElement('img'); img.src = '/' + item.result_path; img.alt = item.payload.prompt; card.append(img);}
      const text = document.createElement('p'); text.textContent = item.payload.prompt;
      const caption = document.createElement('small'); caption.textContent = item.mode.toUpperCase() + ' · ' + item.status;
      card.append(text,caption);
      card.addEventListener('click', () => {
        if (loading) return;
        const shot = shots.find(s => s.id === item.shot_id); if (!shot) return;
        selectCreation(shot.id); $('prompt').value = item.payload.prompt; restoreParams(item.payload.params);
        selected = (item.payload.ref_ids || []).filter(id => refs.some(r => r.id === id)); renderRefs();
        showResult(item.result_path); status(item.error || '已载入历史参数，可继续创作');
      });
      $('history-list').append(card);
    }
  }
  async function poll(tid, sid) {
    clearTimeout(timer);
    try {
      const task = await api('/generations/' + tid);
      if (!current || current.id !== sid) return;
      if (['pending','processing'].includes(task.status)) {
        busy(true); renderRefs(); status(task.status === 'pending' ? '任务排队中…' : '正在生成图片…');
        timer = setTimeout(() => poll(tid,sid), 1800); return;
      }
      busy(false); renderRefs(); showResult(task.result_path);
      status(task.error || '生成完成 · AI 生成');
      current.latest_task = task; current.result_path = task.result_path;
      await history();
    } catch (error) {status('查询暂时失败，将继续查询：' + error.message); timer = setTimeout(() => poll(tid,sid), 3000);}
  }
  function selectCreation(id) {
    clearTimeout(timer); current = shots.find(s => s.id === id); $('creation').value = id;
    if (!current) return;
    localStorage.setItem('mmh3_image_shot_' + $('project').value, id);
    $('prompt').value = current.prompt || ''; restoreParams(current.params);
    refs = current.refs || []; selected = (current.params.ref_ids || refs.map(r => r.id)).filter(id => refs.some(r => r.id === id)).slice(0,4);
    busy(false); renderRefs(); showResult(current.result_path); status('准备就绪');
    if (current.latest_task && ['pending','processing'].includes(current.latest_task.status)) {
      busy(true); renderRefs(); poll(current.latest_task.id,id);
    }
  }
  async function loadCreations(selectId) {
    shots = (await api('/projects/' + $('project').value + '/shots')).filter(s => s.mode === 'image');
    if (!shots.length) {await newCreation(); return;}
    $('creation').replaceChildren(...shots.map(s => new Option(s.name,s.id)));
    const previous = selectId || localStorage.getItem('mmh3_image_shot_' + $('project').value);
    selectCreation(shots.some(s => s.id === previous) ? previous : shots[shots.length-1].id);
    await history();
  }
  async function newCreation() {
    const shot = await api('/projects/' + $('project').value + '/shots', json({name:'图像 ' + (shots.length+1),mode:'image'}));
    await loadCreations(shot.id);
  }
  async function loadProjects(selectId) {
    let projects = await api('/projects');
    if (!projects.length) projects = [await api('/projects', json({name:'图像创作'}))];
    $('project').replaceChildren(...projects.map(p => new Option(p.name,p.id)));
    const previous = selectId || localStorage.getItem(memoryKey);
    $('project').value = projects.some(p => p.id === previous) ? previous : projects[0].id;
    localStorage.setItem(memoryKey,$('project').value); await loadCreations();
  }
  async function guarded(action) {
    if (loading) return;
    busy(true);
    try {await action();} catch (error) {status(error.message);}
    finally {busy(false); renderRefs(); if (current?.latest_task && ['pending','processing'].includes(current.latest_task.status)) {busy(true); renderRefs();}}
  }
  $('new-project').addEventListener('click', () => {
    const name = window.prompt('项目名称', '图像创作');
    if (name?.trim()) guarded(async () => {const p = await api('/projects',json({name:name.trim()})); await loadProjects(p.id);});
  });
  $('new-creation').addEventListener('click', () => guarded(newCreation));
  $('project').addEventListener('change', () => guarded(async () => {localStorage.setItem(memoryKey,$('project').value); await loadCreations();}));
  $('creation').addEventListener('change', () => selectCreation($('creation').value));
  for (const id of ['prompt','width','height','steps','seed']) $(id).addEventListener('change', () => {
    if (!loading) saveDraft().catch(error => status(error.message));
  });
  $('references').addEventListener('change', () => guarded(async () => {
    const files = Array.from($('references').files);
    try {
      if (selected.length + files.length > config.max_references) throw new Error('最多选择 4 张参考图');
      for (const file of files) {
        if (file.size > config.max_upload_mb * 1024 * 1024) throw new Error('图片超过上传大小限制');
        const form = new FormData(); form.append('shot_id',current.id); form.append('file',file);
        const asset = await api('/upload',{method:'POST',body:form}); refs.push(asset); selected.push(asset.id);
        current.refs = refs; renderRefs(); await saveDraft();
      }
      status('参考图已上传');
    } finally {$('references').value = '';}
  }));
  $('image-form').addEventListener('submit', event => {
    event.preventDefault();
    guarded(async () => {
      await saveDraft();
      const task = await api('/image-generations',json({shot_id:current.id,prompt:$('prompt').value,...parameters()}));
      current.latest_task = task; $('seed').value = task.payload.params.seed;
      current.params = {...task.payload.params, ref_ids:task.payload.ref_ids || selected.slice()};
      await poll(task.id,current.id);
    });
  });
  busy(true);
  api('/images/config').then(data => {config = data; $('model').textContent = data.model; return loadProjects();})
    .catch(error => status(error.message)).finally(() => {if (!current?.latest_task || !['pending','processing'].includes(current.latest_task.status)) busy(false);});
})();
