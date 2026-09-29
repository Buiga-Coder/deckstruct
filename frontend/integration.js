/* Real API integration. Existing design assets remain in index.html. */
(() => {
  const visibilityStyle = document.createElement('style');
  visibilityStyle.textContent = '#page-analysis [hidden] { display: none !important; }';
  document.head.appendChild(visibilityStyle);
  const esc = escapeHtml;
  const labels = {uploaded:'Загружен',queued:'В очереди',extracting:'Извлечение структуры',rendering:'Создание превью',analyzing:'Анализ моделью',exporting:'Подготовка результата',completed:'Анализ завершён',partial:'Частичный результат',failed:'Ошибка анализа',interrupted:'Прерван — можно продолжить',awaiting_configuration:'Структура извлечена; ожидается настройка API',submission_failed:'Ожидает отправки в парсер'};
  const active = new Set(['queued','extracting','rendering','analyzing','exporting']);
  let poll, urls = [], analysisId, generation = 0;
  const revoke = () => { urls.forEach(u => URL.revokeObjectURL(u)); urls=[]; };
  let deckPoll;
  const stop = () => { clearTimeout(poll); clearTimeout(deckPoll); generation++; revoke(); };
  const originalGo = Router.go.bind(Router);
  Router.go = id => { stop(); if(id==='result'&&Auth.isAuthed()){const deckId=Store.get('real_deck_id');return deckId?Decks.open(deckId):originalGo('decks');}const result=originalGo(id);if(id==='workspace'&&Auth.isAuthed())Workspace.init();return result; };
  const originalLogout = Auth.logout.bind(Auth);
  Auth.logout = silent => { stop(); Workspace.state.template=null; Workspace.state.contentFiles=[]; Store.del('ds_current_id'); originalLogout(silent); };
  const status = t => labels[t.status] || 'Статус неизвестен';
  async function fileResponse(url) {
    const r = await fetch(url,{headers:{Authorization:'Bearer '+Auth.token}});
    if(!r.ok)throw new Error((await r.json().catch(()=>({}))).message || 'Файл пока недоступен');
    return r.blob();
  }
  function download(blob,name) { const u=URL.createObjectURL(blob), a=document.createElement('a');a.href=u;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000); }
  function select(t) {
    Workspace.state.template=t;Store.set('ds_current_id',t.id);
  }
  Workspace.handleTemplate = async file => {
    try { const t=await API.uploadTemplate(Auth.token,file);select(t);Toast.success('Шаблон сохранён. Открываем анализ.');Router.go('analysis'); }
    catch(e){Toast.error(e.message);}
  };
  Workspace._updateProgress = () => {};
  Workspace.init = async () => {
    const host=$('#page-workspace');
    if(!$('#generationForm'))host.innerHTML=`<div style="max-width:850px;margin:0 auto;padding:40px 24px 80px">
      <h1 style="font-size:42px">Создать презентацию</h1><p style="margin:18px 0">Выберите разобранный шаблон, добавьте материалы и получите три редактируемых PPTX.</p>
      <label class="btn-outline-custom sm">Загрузить новый PPTX<input id="realTemplateUpload" type="file" accept=".pptx" hidden></label>
      <form id="generationForm" style="display:grid;gap:18px;margin-top:28px">
        <label>Шаблон<select id="genTemplate" name="template_id" required style="width:100%;padding:12px"></select></label>
        <button type="button" class="btn-outline-custom sm" id="genAnalysis">Открыть анализ шаблона</button>
        <label>Название<input name="title" maxlength="120" required placeholder="Например: План развития продукта" style="width:100%;padding:12px"></label>
        <label>Материалы ZIP (до 50 МБ)<input name="content" type="file" accept=".zip" required style="display:block;margin-top:8px"></label>
        <p>В ZIP: тексты TXT/MD/DOCX/PDF, таблицы CSV/XLSX и изображения PNG/JPG. До 200 файлов, до 100 МБ после распаковки. Материалы обрабатываются моделью через Polza.</p>
        <label>Задача и аудитория<textarea name="brief" required maxlength="20000" rows="5" placeholder="Что рассказать, кому и на каком языке? Используйте только факты из материалов." style="width:100%;padding:12px"></textarea></label>
        <label>Слайдов в каждом варианте<input name="slide_count" type="number" min="3" max="20" value="5" required style="width:100%;padding:12px"></label>
        <label><input name="allow_partial" type="checkbox" value="true"> Разрешаю использовать только успешно разобранные слайды, если анализ частичный</label>
        <p id="generationNotice" role="status"></p><button class="btn-custom" type="submit" id="generateBtn">Создать три варианта</button>
      </form></div>`;
    const form=$('#generationForm');
    $('#realTemplateUpload').onchange=e=>{if(e.target.files[0])Workspace.handleTemplate(e.target.files[0]);};
    $('#genAnalysis').onclick=()=>{Store.set('ds_current_id',$('#genTemplate').value);Router.go('analysis');};
    form.onsubmit=async e=>{
      e.preventDefault();const button=$('#generateBtn');button.disabled=true;
      $('#generationNotice').textContent='Загружаем материалы…';
      try {
        const data=await HttpBackend._fetch('/decks',{method:'POST',body:new FormData(form)});
        $('#generationNotice').textContent='Задание сохранено';Decks.open(data.id);
      } catch(err){$('#generationNotice').textContent=err.message;}
      finally{button.disabled=false;}
    };
    if(!Auth.isAuthed())return;
    const chosen=$('#genTemplate').value||Store.get('ds_current_id');
    try{const list=await API.listTemplates(Auth.token);$('#genTemplate').innerHTML='<option value="">Выберите шаблон</option>'+list.map(t=>`<option value="${esc(t.id)}">${esc(t.name)} — ${esc(status(t))}</option>`).join('');if(list.some(t=>t.id===chosen))$('#genTemplate').value=chosen;}
    catch(e){$('#generationNotice').textContent=e.message;}
  };
  Decks.render = () => {
    const names={done:'Готово',queued:'В очереди',running:'Генерация',failed:'Ошибка',interrupted:'Прервано',submitting:'Отправка',generator_unavailable:'Старый черновик'};
    const list=Decks._all.filter(d=>(Decks._filter!=='done'||d.status==='done')&&(Decks._filter!=='draft'||d.status!=='done')&&d.title.toLowerCase().includes(Decks._query||''));
    $('#decksGrid').innerHTML=list.length?list.map(d=>`<div class="deck-card"><div class="meta"><div class="t">${esc(d.title)}</div><p>${d.slides} слайдов · ${esc(names[d.status]||d.status)}</p><div class="actions"><button data-open="${esc(d.id)}">Открыть</button><button data-remove="${esc(d.id)}">Удалить</button></div></div></div>`).join(''):'Пока нет презентаций. Начните в разделе «Создать».';
    $$('[data-open]').forEach(b=>b.onclick=()=>Decks.open(b.dataset.open));
    $$('[data-remove]').forEach(b=>b.onclick=()=>Decks.remove(b.dataset.remove));
  };
  Decks.open = async id => {
    stop();Store.set('real_deck_id',id);originalGo('result');const token=generation;
    $('#page-result').innerHTML='<div style="max-width:850px;margin:0 auto;padding:40px 24px"><h1 id="realDeckTitle" style="font-size:38px">Презентация</h1><p id="realDeckStatus" role="status" style="margin:24px 0">Загрузка…</p><div id="realDeckActions" style="display:flex;gap:12px;flex-wrap:wrap"></div></div>';
    async function update(){
      try {
        const d=await HttpBackend._fetch('/decks/'+id);if(token!==generation)return;
        $('#realDeckTitle').textContent=d.title;
        $('#realDeckStatus').textContent=(d.message||d.status)+(d.status==='running'?` · ${d.percent}%`:'');
        const actions=$('#realDeckActions');actions.innerHTML='';
        if(d.status==='done')for(let v=1;v<=d.variants;v++){
          const b=document.createElement('button');b.className='btn-custom sm';b.textContent=`Скачать вариант ${v}`;b.onclick=async()=>{try{download(await fileResponse(`/api/decks/${id}/download?variant=${v}`),`${d.title}-${v}.pptx`);}catch(e){Toast.error(e.message);}};actions.append(b);
        }
        if(['failed','interrupted'].includes(d.status)){
          const b=document.createElement('button');b.className='btn-outline-custom sm';b.textContent='Продолжить';b.onclick=async()=>{try{await HttpBackend._fetch(`/decks/${id}/resume`,{method:'POST'});await update();}catch(e){Toast.error(e.message);}};actions.append(b);
        }
        if(['queued','running','submitting'].includes(d.status))deckPoll=setTimeout(update,4000);
      }catch(e){if(token===generation)$('#realDeckStatus').textContent=e.message;}
    }
    await update();
  };
  Gallery.use = () => { Toast.info('В галерее показаны примеры дизайна. Для анализа загрузите свой PPTX.'); Router.go('workspace'); };
  Templates.use = id => { const t=Templates._all.find(t=>t.id===id);if(t){select(t);Router.go('workspace');} };
  const originalTemplatesRender = Templates.render.bind(Templates);
  Templates.render = () => {
    originalTemplatesRender();
    $$('#templatesGrid .deck-card').forEach((card,i)=>{const t=Templates._all[i];if(t)card.querySelector('.d').textContent=`${t.slides || '—'} слайдов · ${status(t)} · ${formatDate(t.createdAt)}`;});
  };
  DesignSystem.init = async id => {
    stop();
    analysisId=id;
    if(!id){ const list=await API.listTemplates(Auth.token).catch(()=>[]);analysisId=list[0]?.id; }
    $('#page-analysis').innerHTML=`<div style="max-width:1160px;margin:0 auto;padding:40px 24px 80px">
      <button class="btn-outline-custom sm" id="analysisBack">← Мои шаблоны</button>
      <h2 id="analysisName" style="margin-top:24px">Анализ шаблона</h2>
      <p id="analysisStatus" role="status" style="margin:16px 0">Загрузка…</p>
      <div style="display:flex;gap:12px;flex-wrap:wrap;margin:20px 0">
        <button class="btn-custom sm" id="analysisRefresh">Обновить</button>
        <button class="btn-outline-custom sm" id="analysisResume" hidden>Продолжить анализ</button>
        <button class="btn-outline-custom sm" id="analysisZip" hidden>Скачать пакет для генератора</button>
        <button class="btn-outline-custom sm" id="analysisJson" hidden>Экспорт JSON</button>
      </div>
      <div id="analysisFacts"></div><div id="analysisSlides" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px;margin-top:28px"></div>
      <details id="analysisOverrides" style="margin-top:28px" hidden><summary>Правки дизайн-системы</summary>
        <p style="margin:12px 0">Ваши настройки сохраняются отдельно от исходного анализа. Генератор ещё не применяет их.</p>
        <textarea id="analysisEditor" aria-label="Правки дизайн-системы JSON" rows="10" style="width:100%;font:14px monospace;background:var(--surface);color:var(--ink);padding:16px;border:1px solid var(--line)"></textarea>
        <p id="analysisSaveStatus" role="status"></p>
      </details>
    </div>`;
    $('#analysisBack').onclick=()=>Router.go('templates');
    $('#analysisRefresh').onclick=()=>refresh();
    $('#analysisResume').onclick=async()=>{try{await HttpBackend._fetch(`/templates/${analysisId}/analyze`,{method:'POST'});await refresh();}catch(e){Toast.error(e.message);}};
    if(!analysisId){$('#analysisStatus').textContent='Сначала загрузите PPTX в рабочей области.';return;}
    const editorId=analysisId, token=generation;
    const overrides=await DesignSystemAPI.http.get(editorId).catch(()=>({}));
    if(token!==generation)return;
    $('#analysisEditor').value=JSON.stringify(overrides,null,2);
    $('#analysisOverrides').hidden=false;
    let saveTimer;
    $('#analysisEditor').oninput=e=>{
      const value=e.target.value;clearTimeout(saveTimer);
      $('#analysisSaveStatus').textContent='Есть несохранённые изменения…';
      saveTimer=setTimeout(async()=>{
        try{const parsed=JSON.parse(value);if(!parsed || Array.isArray(parsed) || typeof parsed!=='object')throw new Error('Нужен JSON-объект');await DesignSystemAPI.http.update(editorId,parsed);if(token===generation)$('#analysisSaveStatus').textContent='Сохранено';}
        catch(e){if(token===generation)$('#analysisSaveStatus').textContent='Не сохранено: '+e.message;}
      },800);
    };
    await refresh();
  };
  async function refresh(){
    clearTimeout(poll);
    const id=analysisId, token=generation;
    try{
      const data=await HttpBackend._fetch(`/templates/${id}/analysis`);
      if(token!==generation)return;
      $('#analysisName').textContent=data.template.name;
      let message=labels[data.status] || data.status;
      const counts=data.progress?.counts;
      if(counts)message+=` · готовы ${(counts.completed||0)+(counts.skipped||0)} из ${data.progress.total} · ошибок ${(counts.error||0)+(counts.api_error||0)} · ожидают ${counts.pending||0}${data.progress.currentSlide?' · сейчас '+data.progress.currentSlide:''}`;
      if(data.status==='partial')message+=' — не все слайды прошли проверку; доступен частичный пакет.';
      if(data.status==='failed')message+=' — '+(data.error || 'проверьте исходный файл');
      if(data.status==='awaiting_configuration')message+='; настройте ключ модели, затем нажмите «Продолжить анализ».';
      $('#analysisStatus').textContent=message;
      $('#analysisResume').hidden=!['partial','failed','interrupted','awaiting_configuration','submission_failed','uploaded'].includes(data.status);
      $('#analysisZip').hidden=!data.version;
      $('#analysisZip').onclick=async()=>{try{download(await fileResponse(`/api/templates/${id}/package`),'template-analysis.zip');}catch(e){Toast.error(e.message);}};
      $('#analysisJson').hidden=!data.summary;
      $('#analysisJson').onclick=()=>download(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}),'template-analysis.json');
      if(data.summary){
        const s=data.summary;
        $('#analysisFacts').innerHTML=`<div style="padding:24px;border:1px solid var(--line);border-radius:20px;background:var(--surface)">
          <h3>Структура и оформление</h3><p style="margin:12px 0">${s.slides} слайдов · ${s.masters} мастеров · ${s.layouts.length} макетов</p>
          <h4>Цвета из PPTX</h4><div style="display:flex;flex-wrap:wrap;gap:10px;margin:14px 0">${s.palette.map(c=>`<span style="display:inline-flex;align-items:center;gap:6px"><i style="display:inline-block;width:22px;height:22px;border:1px solid #888;border-radius:5px;background:${/^#[0-9A-F]{6}$/.test(c.hex)?c.hex:'#888'}"></i>${esc(c.hex)}</span>`).join('') || 'Явные RGB-цвета не найдены'}</div>
          <h4>Шрифты</h4><p style="margin:12px 0">${s.fonts.map(f=>esc(f.family)).join(', ') || 'Явные шрифты не найдены'}</p>
          <h4>Макеты</h4><p style="margin:12px 0">${s.layouts.map(l=>esc(l.name)).join(' · ')}</p>
          <p style="color:var(--ink-soft);font-size:14px">${s.notes.map(esc).join('<br>')}</p></div>`;
        // Do not repeatedly download previews on each polling tick.
        const signature=id+':'+(data.version||'')+':'+data.status;
        if($('#analysisSlides').dataset.signature!==signature && !['queued','extracting','rendering'].includes(data.status)){
          $('#analysisSlides').dataset.signature=signature;revoke();
          const analyzed=new Map((data.result?.slides||[]).map(x=>[x.slide_id,x]));
          const slideStates=new Map((data.slideStatuses||[]).map(x=>[x.slide_id,x]));
          const slides=Array.from({length:s.slides},(_,i)=>({slide_id:'s'+(i+1),components:[],...analyzed.get('s'+(i+1))}));
          $('#analysisSlides').innerHTML=slides.map(slide=>`<article class="card" style="padding:18px;background:var(--surface);border:1px solid var(--line);border-radius:18px"><h4>${esc(slide.slide_id)}</h4><p>${esc(({completed:"Готово",skipped:"Готово (сохранено)",error:"Ошибка разметки моделью",api_error:"Ошибка API — можно повторить",pending:"Ожидает анализа",processing:"Анализируется"})[slideStates.get(slide.slide_id)?.status] || "Ожидает анализа")}</p><img data-slide="${esc(slide.slide_id)}" alt="Превью ${esc(slide.slide_id)}" style="width:100%;margin:12px 0;border-radius:8px" loading="lazy"><p>${slide.needs_review?'Требуется проверка результата':''}</p><ul>${(slide.components||[]).map(c=>`<li>${esc(c.name||c.kind||c.id)} · ${(c.slots||[]).length} полей для замены</li>`).join('')}</ul></article>`).join('');
          for(const img of $$('#analysisSlides img')){
            try{const blob=await fileResponse(`/api/templates/${id}/previews/${img.dataset.slide}.png`);if(token!==generation)return;const url=URL.createObjectURL(blob);urls.push(url);img.src=url;}
            catch {img.alt='Превью пока недоступно';}
          }
        }
      }
      if(active.has(data.status))poll=setTimeout(refresh,4000);
    }catch(e){if(token!==generation)return;$('#analysisStatus').textContent=e.message;}
  }
  document.addEventListener('DOMContentLoaded',()=>{
    Workspace._updateProgress();
    const link=document.createElement('a');link.href='/admin.html';link.textContent='Админ-панель';link.style.cssText='display:inline-block;margin:20px';$('#page-profile').append(link);
  });
})();
