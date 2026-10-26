/* Shared family viewings. The dialog is independent of background table refresh. */
'use strict';
globalThis.XataVisits = {create({getItem,getUser,saveItem,request,jsonOptions,render,refresh}) {
  const outcomes={liked:'Понравилось',rejected:'Не понравилось',thinking:'Думаем'};
  const states={planned:'Запланирован',completed:'Состоялся',cancelled:'Отменён'};
  const reasons={noise:'Шум',layout:'Планировка',condition:'Состояние квартиры',building:'Дом / подъезд',location:'Расположение',documents:'Документы',other:'Другое'};
  const zone='Europe/Moscow';
  const format=value=>value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('ru-RU',{timeZone:zone,day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
  const canEdit=()=>['admin','reviewer'].includes(getUser()?.role);
  const canEditDate=()=>['admin','realtor'].includes(getUser()?.role);
  const dateEditors=new WeakMap();
  function el(tag,text,cls) {const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;}
  function button(text,action) {const b=el('button',text,'secondary compact');b.type='button';b.addEventListener('click',action);return b;}
  const dialog=document.querySelector('#visit-dialog'),form=document.querySelector('#visit-form');
  const date=document.querySelector('#visit-date'),state=document.querySelector('#visit-state'),outcome=document.querySelector('#visit-outcome');
  const preparation=document.querySelector('#visit-preparation'),comment=document.querySelector('#visit-comment');
  const error=document.querySelector('#visit-error'),submit=document.querySelector('#visit-save');
  const recordChoice=document.querySelector('#visit-record-choice');
  const reasonFields=document.querySelector('#visit-reasons'),outcomeFields=document.querySelector('#visit-outcome-group');
  for(const [value,label] of Object.entries(reasons)) {
    const choice=el('label'),input=el('input');input.type='checkbox';input.value=value;input.name='rejection_reasons';
    choice.append(input,document.createTextNode(label));reasonFields.append(choice);
  }
  let editing=null,saving=false,shownState='planned',draftDates={};
  function localDate(value) {
    if(!value)return '';
    return new Intl.DateTimeFormat('sv-SE',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(value)).replace(' ','T');
  }
  function stateFields() {
    const done=state.value==='completed';
    outcomeFields.hidden=comment.closest('label').hidden=!done;
    reasonFields.hidden=!done||outcome.value!=='rejected';
    document.querySelector('#visit-legacy').hidden=!done||outcome.value!=='thinking';
    for(const choice of outcomeFields.querySelectorAll('[data-visit-outcome]'))choice.setAttribute('aria-pressed',String(choice.dataset.visitOutcome===outcome.value));
    date.required=state.value!=='cancelled';
    document.querySelector('#visit-date-label').textContent=done?'Фактическая дата и время (Москва)':'Плановая дата и время (Москва)';
    submit.textContent=done?'Сохранить результат':(state.value==='cancelled'?'Отменить встречу':'Сохранить');
  }
  state.addEventListener('change',()=>{
    draftDates[shownState==='completed'?'completed':'planned']=date.value;
    shownState=state.value;date.value=draftDates[shownState==='completed'?'completed':'planned']||'';
    stateFields();
  });
  for(const choice of outcomeFields.querySelectorAll('[data-visit-outcome]'))choice.addEventListener('click',()=>{
    outcome.value=choice.dataset.visitOutcome;stateFields();
  });
  function open(key,visit=null,mode=null) {
    const item=getItem(key);if(!item||!canEdit()||saving)return;
    const actualTime=visit?.completed_at||new Date().toISOString();
    editing={key,listingId:item.id,propertyId:item.property_id,id:visit?.id,version:visit?.version||0,
      scheduledAt:visit?.scheduled_at||null,completedAt:actualTime,requestId:visit?null:crypto.randomUUID()};
    document.querySelector('#visit-title').textContent=visit?'Просмотр квартиры':'Новый просмотр';
    document.querySelector('#visit-address').textContent=item.address||item.title||'Квартира';
    state.value=mode||visit?.status||'planned';shownState=state.value;
    draftDates={planned:localDate(visit?.scheduled_at),completed:localDate(actualTime)};
    date.value=draftDates[shownState==='completed'?'completed':'planned'];
    for(const option of state.options)option.disabled=visit?.status==='completed'&&option.value!=='completed';
    outcome.value=visit?.outcome||'';preparation.value=visit?.preparation||'';comment.value=visit?.comment||'';
    for(const input of reasonFields.querySelectorAll('input'))input.checked=(visit?.rejection_reasons||[]).includes(input.value);
    recordChoice.replaceChildren();
    for(const entry of item.visits||[]) {
      const option=el('option',format(entry.status==='completed'?entry.completed_at:entry.scheduled_at)+' · '+(outcomes[entry.outcome]||states[entry.status]));
      option.value=String(entry.id);recordChoice.append(option);
    }
    const fresh=el('option','Новый просмотр');fresh.value='new';recordChoice.append(fresh);
    recordChoice.value=visit?String(visit.id):'new';
    error.textContent='';stateFields();if(!dialog.open)dialog.showModal();
  }
  recordChoice.addEventListener('change',()=>{
    if(!editing||saving)return;
    const item=getItem(editing.key),entry=(item?.visits||[]).find(v=>String(v.id)===recordChoice.value)||null;
    open(editing.key,entry,'completed');
  });
  document.querySelector('#visit-close').addEventListener('click',()=>{if(!saving)dialog.close();});
  dialog.addEventListener('cancel',event=>{if(saving)event.preventDefault();});
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(saving||!editing)return;
    const current=getItem(editing.key);
    if(!current||current.property_id!==editing.propertyId) {error.textContent='Объект изменился. Закройте окно и откройте его заново.';return;}
    const done=state.value==='completed';
    if(done&&!outcome.value) {error.textContent='Выберите результат просмотра.';return;}
    const entered=date.value?date.value+':00+03:00':null;
    const actual=done?(date.value===localDate(editing.completedAt)?editing.completedAt:entered):null;
    const scheduled=done?editing.scheduledAt:(date.value===localDate(editing.scheduledAt)?editing.scheduledAt:entered);
    const data={property_id:editing.propertyId,version:editing.version,status:state.value,
      scheduled_at:scheduled,completed_at:actual,outcome:done?outcome.value:null,
      preparation:preparation.value,comment:comment.value,
      rejection_reasons:done&&outcome.value==='rejected'?[...reasonFields.querySelectorAll('input:checked')].map(n=>n.value):[]};
    if(!editing.id)data.request_id=editing.requestId;
    saving=true;error.textContent='';
    for(const field of form.querySelectorAll('input,textarea,select,button'))field.disabled=true;
    try {
      const suffix=editing.id?'/'+editing.id:'';
      const saved=await request('/api/listings/'+encodeURIComponent(editing.listingId)+'/visits'+suffix,jsonOptions(editing.id?'PATCH':'POST',data));
      saveItem(saved);dialog.close();render();await refresh();
    } catch(e) {error.textContent=e.message;}
    finally {saving=false;for(const field of form.querySelectorAll('input,textarea,select,button'))field.disabled=false;}
  });
  function actions(item,visit) {
    const box=el('div',null,'visit-actions'),key=XataReview.key(item);
    if(visit.status==='planned')box.append(
      button('Записать результат',()=>open(key,visit,'completed')),
      button('Перенести',()=>open(key,visit)),
      button('Отменить',()=>open(key,visit,'cancelled')));
    else box.append(button('Изменить',()=>open(key,visit)));
    return box;
  }
  function record(item,visit,agenda=false) {
    const box=el('div',null,'visit-record');box.dataset.visitId=visit.id;
    box.append(el('strong',format(visit.status==='completed'?visit.completed_at:visit.scheduled_at)),
      el('div',outcomes[visit.outcome]||states[visit.status],'visit-state'));
    if(XataReview.overdue(visit))box.append(el('small','Укажите результат просмотра','visit-overdue'));
    if(visit.preparation) {
      if(agenda)box.append(el('p','Что проверить / спросить: '+visit.preparation,'visit-preparation'));
      else {const d=el('details');d.append(el('summary','Что проверить / спросить'),el('p',visit.preparation));box.append(d);}
    }
    if(visit.comment) {const d=el('details');d.append(el('summary','После просмотра'),el('p',visit.comment));box.append(d);}
    const labels=(visit.rejection_reasons||[]).map(value=>reasons[value]).filter(Boolean);
    if(labels.length)box.append(el('p','Причины отказа: '+labels.join(', '),'visit-rejection-reasons'));
    box.append(el('small',(visit.updated_by_name||visit.updated_by_username||'—')+' · '+format(visit.updated_at),'visit-meta'));
    if(agenda&&canEdit())box.append(actions(item,visit));
    return box;
  }
  function renderDate(cell,item) {
    const shown=XataReview.displayVisit(item),raw=shown?(shown.status==='planned'?shown.scheduled_at:shown.completed_at):null;
    const editable=canEditDate();
    let editor=dateEditors.get(cell);
    if(!editor||editor.editable!==editable) {
      cell.replaceChildren();
      editor={editable,dirty:false,saving:false,failed:false};
      if(editable) {
        const input=el('input');input.type='datetime-local';input.className='visit-date-input';
        input.min='2000-01-01T00:00';input.max='2100-12-31T23:59';input.step='60';
        const status=el('small',null,'visit-date-status');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
        editor.input=input;editor.status=status;cell.append(input,status);
        input.addEventListener('input',()=>{editor.dirty=true;});
        const persist=async()=>{
          if(editor.saving||!editor.dirty||!editor.baseline)return;
          if(!input.checkValidity()) {status.textContent='Укажите корректные дату и время (Москва).';return;}
          if(input.value===editor.original&&!editor.failed) {editor.dirty=false;return;}
          const expected=editor.baseline;
          if(!editor.request)editor.request={property_id:expected.propertyId,visits_version:expected.revision,
            visit_id:expected.visitId,request_id:expected.requestId};
          const data={...editor.request,date:input.value?input.value+':00+03:00':null};
          editor.saving=true;input.disabled=true;status.textContent='Сохраняем дату…';
          let saved=false;
          try {
            const result=await request('/api/listings/'+encodeURIComponent(expected.listingId)+'/visits/date',jsonOptions('PATCH',data));
            saveItem(result);editor.dirty=false;editor.failed=false;editor.request=null;saved=true;
            status.textContent='Дата сохранена.';
          } catch(e) {editor.failed=true;status.textContent=e.message;}
          finally {editor.saving=false;input.disabled=false;render();}
          if(saved)await refresh();
        };
        input.addEventListener('change',persist);
        input.addEventListener('blur',persist);
      } else {
        editor.time=el('time');cell.append(editor.time);
      }
      editor.caption=el('small');editor.overdue=el('small','Укажите результат просмотра','visit-overdue');
      cell.append(editor.caption,editor.overdue);dateEditors.set(cell,editor);
    }
    if(editor.editable) {
      editor.input.setAttribute('aria-label','Дата просмотра квартиры '+item.id+' (Москва)');
      // Keep the exact target and revision captured before the user typed;
      // a background refresh must neither erase a draft nor retarget it.
      if(!editor.saving&&!editor.dirty&&!editor.failed&&document.activeElement!==editor.input) {
        editor.input.value=localDate(raw);editor.original=editor.input.value;
        editor.baseline={listingId:item.id,propertyId:item.property_id,revision:item.visits_version||0,
          visitId:shown?.id||null,requestId:shown?null:crypto.randomUUID()};
        editor.request=null;
      }
    } else {
      editor.time.textContent=format(raw);
      if(raw)editor.time.dateTime=raw;else editor.time.removeAttribute('datetime');
    }
    editor.caption.textContent=shown?states[shown.status]:'—';
    editor.overdue.hidden=!XataReview.overdue(shown);
  }
  function renderCells(dateCell,resultCell,item) {
    renderDate(dateCell,item);
    const signature=JSON.stringify([item.visits_version,item.visits,getUser()?.role,
      (item.visits||[]).map(v=>XataReview.overdue(v))]);
    if(resultCell.dataset.visitsSignature===signature)return;
    resultCell.dataset.visitsSignature=signature;
    const historyOpen=resultCell.querySelector('.visit-history')?.open;
    resultCell.replaceChildren();
    const latest=XataReview.latestCompleted(item);
    if(latest)resultCell.append(record(item,latest));else resultCell.append(el('span','—'));
    if(canEdit())resultCell.append(button('Добавить результат',()=>{
      const current=getItem(XataReview.key(item));
      if(current)open(XataReview.key(current),XataReview.displayVisit(current),'completed');
    }));
    const history=item.visits||[];
    if(history.length) {
      const details=el('details',null,'visit-history');details.open=!!historyOpen;
      details.append(el('summary','История просмотров ('+history.length+')'));
      for(const v of history)details.append(record(item,v));
      resultCell.append(details);
    }
  }
  function agenda(items) {
    const root=document.querySelector('#visit-agenda');
    const signature=JSON.stringify([getUser()?.role,items.map(item=>[item.id,item.property_id,item.address,item.title,item.url,
      item.review_status,item.realtor_status,item.visits_version,item.visits,(item.visits||[]).map(v=>XataReview.overdue(v))])]);
    if(root.dataset.visitsSignature===signature)return;
    root.dataset.visitsSignature=signature;root.replaceChildren();
    const entries=XataReview.agenda(items);
    if(!entries.length)root.append(el('p','Нет запланированных просмотров.'));
    for(const {item,visit} of entries) {
      const card=el('article',null,'panel visit-card');card.dataset.visitId=visit.id;card.dataset.listingId=item.id;
      card.append(el('h3',item.address||item.title||'—'));
      if(XataReview.effectiveStatus(item)==='rejected')card.append(el('p','Не подходит — встреча ещё не отменена','visit-rejected'));
      if(/^https?:\/\/([a-z0-9-]+\.)*cian\.ru(?::(?:80|443))?\/sale\/flat\/\d+\/?(?:[?#].*)?$/i.test(item.url||'')) {
        const link=el('a','Открыть ЦИАН');link.href=item.url;link.target='_blank';link.rel='noopener noreferrer';card.append(link);
      }
      card.append(record(item,visit,true));root.append(card);
    }
  }
  return {renderCells,agenda};
}};
