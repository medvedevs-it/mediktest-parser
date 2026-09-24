(() => {
  'use strict';
  const el = id => document.getElementById(id);
  let job = null, timer = null, generation = 0;
  const active = new Set(['reading', 'queued', 'comparing', 'exporting']);
  const labels = {reading:'Читаем Excel и проверяем строки…',queued:'Сравнение поставлено в очередь…',comparing:'Сравниваем вопросы со снимком банка…',exporting:'Формируем отчёт Excel…',completed:'Сравнение завершено.',failed:'Ошибка сравнения.',interrupted:'Сравнение прервано перезапуском сервиса.'};
  function tab(comparison) {
    el('collectorPanel').hidden = comparison; el('comparisonPanel').hidden = !comparison;
    el('collectorTab').setAttribute('aria-selected', String(!comparison));
    el('comparisonTab').setAttribute('aria-selected', String(comparison));
  }
  el('collectorTab').onclick = () => tab(false);
  el('comparisonTab').onclick = () => tab(true);
  function message(text, error=false) {
    el('compareState').textContent = text;
    el('compareState').classList.toggle('error', error);
  }
  async function api(url, options) {
    const r = await fetch(url, options), body = await r.json();
    if (!r.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Не удалось выполнить запрос.');
    return body;
  }
  function preview() {
    const sheet = job?.sheets?.find(s => s.name === el('compareSheet').value);
    const wrap = el('comparePreview'); wrap.replaceChildren(); wrap.hidden = !sheet;
    el('comparePreviewDetails').hidden=!sheet;
    if (!sheet) return;
    const table=document.createElement('table'), head=document.createElement('thead'), body=document.createElement('tbody');
    table.style.width=Math.max(900,sheet.headers.length*210)+'px';table.style.tableLayout='fixed';
    const tr=document.createElement('tr');
    sheet.headers.forEach(h=>{const th=document.createElement('th');th.textContent=h;tr.append(th)});head.append(tr);
    sheet.preview.forEach(row=>{const tr=document.createElement('tr');row.forEach(v=>{const td=document.createElement('td');td.textContent=v??'';tr.append(td)});body.append(tr)});
    table.append(head,body);wrap.append(table);
    if (job.status==='ready') message(`Файл готов · ${sheet.rows.toLocaleString('ru-RU')} вопросов`);
    el('compareStart').disabled = job.status!=='ready' || !sheet.rows;
  }
  function render(meta) {
    job=meta;
    const busy=active.has(meta.status);
    el('compareFile').disabled=busy;el('compareSpecialty').disabled=busy;
    el('compareSheet').disabled=meta.status!=='ready';
    el('compareStart').disabled=meta.status!=='ready';
    if (meta.specialty) el('compareSpecialty').value=meta.specialty;
    message(meta.error || labels[meta.status] || '', ['failed','interrupted'].includes(meta.status));
    if (meta.sheets && el('compareSheet').dataset.job !== meta.id) {
      el('compareSheet').replaceChildren(...meta.sheets.map(s=>new Option(s.name,s.name)));
      el('compareSheet').dataset.job=meta.id;
      if(meta.sheet) el('compareSheet').value=meta.sheet;
    }
    el('compareSheetField').hidden=!meta.sheets || meta.sheets.length<2;
    if(meta.sheets) preview();
    const complete=meta.status==='completed';el('compareTotals').hidden=!complete;el('compareDownload').hidden=!complete;
    if(complete) {
      const s={...meta.summary};s.missing_client_rows=s.exported_missing_client??s.missing_client_rows;el('compareTotals').replaceChildren();
      [['Совпало вопросов',s.matched_unique],['Нет в вашей базе, строк',s.missing_client_rows],['Нет в банке парсера, строк',s.missing_parser_rows]].forEach(([label,value])=>{
        const card=document.createElement('div'),strong=document.createElement('strong');card.className='comparison-total';card.textContent=label;strong.textContent=typeof value==='number'?value.toLocaleString('ru-RU'):value;card.append(strong);el('compareTotals').append(card);
      });
      message(`Сравнение завершено · ${meta.specialty}`);
      el('compareDownload').href=`/api/comparisons/${meta.id}/report.xlsx`;
    }
  }
  function poll(id, g) {
    clearTimeout(timer);
    timer=setTimeout(async()=>{
      try { const meta=await api(`/api/comparisons/${id}`);if(g!==generation)return;render(meta);if(active.has(meta.status))poll(id,g) }
      catch(e){if(g!==generation)return;message(e.message,true);el('compareFile').disabled=false;el('compareSpecialty').disabled=false}
    },900);
  }
  el('compareSheet').onchange=preview;
  el('compareFile').onchange=async()=>{
    const file=el('compareFile').files[0];if(!file)return;
    if(!file.name.toLowerCase().endsWith('.xlsx')||file.size>20*1024*1024){message('Выберите файл .xlsx размером до 20 МБ.',true);return}
    const g=++generation;clearTimeout(timer);job=null;sessionStorage.removeItem('comparisonJob');
    ['comparePreview','comparePreviewDetails','compareTotals','compareDownload','compareSheetField'].forEach(id=>el(id).hidden=true);
    el('compareStart').disabled=true;el('compareFile').disabled=true;el('compareSpecialty').disabled=true;
    const progress=el('compareUploadProgress');progress.hidden=false;progress.value=0;message('Загружаем файл…');
    try {
      const meta=await new Promise((resolve,reject)=>{
        const xhr=new XMLHttpRequest();xhr.open('POST',`/api/comparisons/uploads?filename=${encodeURIComponent(file.name)}`);xhr.setRequestHeader('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');xhr.timeout=120000;
        xhr.upload.onprogress=e=>{if(e.lengthComputable)progress.value=e.loaded/e.total*100};
        xhr.onerror=()=>reject(new Error('Сеть недоступна. Повторите загрузку.'));xhr.ontimeout=()=>reject(new Error('Истекло время загрузки. Повторите попытку.'));
        xhr.onload=()=>{try{const body=JSON.parse(xhr.responseText);if(xhr.status>=200&&xhr.status<300)resolve(body);else reject(new Error(typeof body.detail==='string'?body.detail:'Ошибка загрузки.'))}catch(e){reject(new Error('Сервис вернул некорректный ответ.'))}};xhr.send(file);
      });
      if(g!==generation)return;sessionStorage.setItem('comparisonJob',meta.id);render(meta);poll(meta.id,g);
    }catch(e){message(e.message,true);el('compareFile').disabled=false;el('compareSpecialty').disabled=false}
    finally{progress.hidden=true;el('compareFile').value=''}
  };
  el('compareStart').onclick=async()=>{
    if(!job)return;el('compareStart').disabled=true;el('compareFile').disabled=true;el('compareSpecialty').disabled=true;el('compareSheet').disabled=true;
    try {const meta=await api(`/api/comparisons/${job.id}/start`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sheet:el('compareSheet').value,specialty:el('compareSpecialty').value})});render(meta);poll(meta.id,generation)}
    catch(e){message(e.message,true);el('compareStart').disabled=false;el('compareFile').disabled=false;el('compareSpecialty').disabled=false;el('compareSheet').disabled=false}
  };
  const saved=sessionStorage.getItem('comparisonJob');
  if(saved) api(`/api/comparisons/${saved}`).then(meta=>{if(job||generation!==0)return;render(meta);if(active.has(meta.status))poll(saved,generation)}).catch(()=>{if(generation===0)sessionStorage.removeItem('comparisonJob')});
})();
