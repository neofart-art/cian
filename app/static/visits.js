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
  const comment=document.querySelector('#visit-comment');
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
      preparation:visit?.preparation||'',scheduledAt:visit?.scheduled_at||null,completedAt:actualTime,requestId:visit?null:crypto.randomUUID()};
    document.querySelector('#visit-title').textContent=visit?'Просмотр квартиры':'Новый просмотр';
    document.querySelector('#visit-address').textContent=item.address||item.title||'Квартира';
    state.value=mode||visit?.status||'planned';shownState=state.value;
    draftDates={planned:localDate(visit?.scheduled_at),completed:localDate(actualTime)};
    date.value=draftDates[shownState==='completed'?'completed':'planned'];
    for(const option of state.options)option.disabled=visit?.status==='completed'&&option.value!=='completed';
    outcome.value=visit?.outcome||'';comment.value=visit?.comment||'';
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
      preparation:editing.preparation,comment:comment.value,
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
    if(visit.comment) {const d=el('details');d.append(el('summary','После просмотра'),el('p',visit.comment));box.append(d);}
    const labels=(visit.rejection_reasons||[]).map(value=>reasons[value]).filter(Boolean);
    if(labels.length)box.append(el('p','Причины отказа: '+labels.join(', '),'visit-rejection-reasons'));
    box.append(el('small',(visit.updated_by_name||visit.updated_by_username||'—')+' · '+format(visit.updated_at),'visit-meta'));
    if(agenda&&canEdit())box.append(actions(item,visit));
    return box;
  }
  // One shared calendar, independent of native date-input rendering. It only
  // chooses a draft day; the existing OK handler remains the sole write path.
  const calendarPopup=(()=>{
    const popup=el('div',null,'visit-calendar');popup.id='visit-calendar-popup';
    const native=typeof popup.showPopover==='function';
    if(native)popup.setAttribute('popover','auto');else popup.hidden=true;
    popup.setAttribute('role','group');popup.setAttribute('aria-label','Календарь просмотра');
    const head=el('div',null,'visit-calendar-head'),title=el('strong');
    title.setAttribute('aria-live','polite');
    let active=null,year=2000,month=0;
    const opened=()=>native?popup.matches(':popover-open'):!popup.hidden;
    const close=(focus=false)=>{
      const trigger=active?.trigger;active=null;
      if(trigger)trigger.setAttribute('aria-expanded','false');
      if(native&&opened())popup.hidePopover();else if(!native)popup.hidden=true;
      if(focus&&trigger?.isConnected)trigger.focus({preventScroll:true});
    };
    const previous=button('‹',()=>move(-1)),next=button('›',()=>move(1)),dismiss=button('×',()=>close(true));
    previous.setAttribute('aria-label','Предыдущий месяц');next.setAttribute('aria-label','Следующий месяц');
    dismiss.setAttribute('aria-label','Закрыть календарь');
    head.append(previous,title,next,dismiss);
    const table=el('table');table.setAttribute('aria-label','Дни месяца');
    const thead=el('thead'),week=el('tr'),body=el('tbody');
    for(const text of ['Пн','Вт','Ср','Чт','Пт','Сб','Вс']) {const th=el('th',text);th.scope='col';week.append(th);}
    thead.append(week);table.append(thead,body);popup.append(head,table);document.body.append(popup);
    function place() {
      if(!active||!opened())return;
      if(!active.trigger.isConnected) {close();return;}
      const box=active.trigger.getBoundingClientRect(),size=popup.getBoundingClientRect();
      popup.style.left=Math.max(8,Math.min(box.right-size.width,innerWidth-size.width-8))+'px';
      popup.style.top=Math.max(8,box.bottom+6+size.height<=innerHeight-8?box.bottom+6:box.top-size.height-6)+'px';
    }
    function draw() {
      title.textContent=new Intl.DateTimeFormat('ru-RU',{month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(Date.UTC(year,month,1)));
      previous.disabled=year===2000&&month===0;next.disabled=year===2100&&month===11;
      body.replaceChildren();
      const start=(new Date(Date.UTC(year,month,1)).getUTCDay()+6)%7;
      const count=new Date(Date.UTC(year,month+1,0)).getUTCDate(),today=XataReview.moscowDay(Date.now());
      let row;
      for(let index=0;index<Math.ceil((start+count)/7)*7;index++) {
        if(index%7===0) {row=el('tr');body.append(row);}
        const cell=el('td'),day=index-start+1;row.append(cell);
        if(day<1||day>count)continue;
        const value=String(year)+'-'+String(month+1).padStart(2,'0')+'-'+String(day).padStart(2,'0');
        const choice=button(String(day),()=>{
          const selected=active;
          if(!selected||!selected.trigger.isConnected||!canEditDate()) {close();return;}
          close();selected.choose(value);
        });
        choice.className='visit-calendar-day';choice.dataset.calendarDate=value;
        choice.setAttribute('aria-label',XataReview.displayDay(value));
        choice.setAttribute('aria-pressed',String(value===active?.selected));
        if(value===today)choice.setAttribute('aria-current','date');
        cell.append(choice);
      }
      place();
    }
    function move(delta) {
      const date=new Date(Date.UTC(year,month+delta,1));
      if(date.getUTCFullYear()<2000||date.getUTCFullYear()>2100)return;
      year=date.getUTCFullYear();month=date.getUTCMonth();draw();
    }
    function toggle(trigger,selected,choose) {
      if(opened()&&active?.trigger===trigger){close();return;}
      close();
      const value=selected||XataReview.moscowDay(Date.now());
      year=Number(value.slice(0,4));month=Number(value.slice(5,7))-1;
      active={trigger,selected,choose};draw();
      trigger.setAttribute('aria-expanded','true');
      if(native)popup.showPopover();else popup.hidden=false;
      place();
      (popup.querySelector('[data-calendar-date="'+value+'"]')||popup.querySelector('.visit-calendar-day'))?.focus({preventScroll:true});
    }
    document.addEventListener('pointerdown',event=>{
      if(active&&!popup.contains(event.target)&&!active.trigger.contains(event.target))close();
    });
    document.addEventListener('focusin',event=>{if(active&&event.target.closest?.('dialog[open]'))close();});
    document.addEventListener('keydown',event=>{
      if(event.key==='Escape'&&active) {event.preventDefault();close(true);}
    });
    popup.addEventListener('toggle',()=>{if(!opened()&&active)close();});
    window.addEventListener('resize',place);window.addEventListener('scroll',place,true);
    return {toggle,owns:trigger=>active?.trigger===trigger&&opened(),
      closeFor:trigger=>{if(active?.trigger===trigger)close();}};
  })();
  function renderDate(cell,item) {
    const shown=XataReview.displayVisit(item),raw=shown?(shown.status==='planned'?shown.scheduled_at:shown.completed_at):null;
    const day=XataReview.moscowDay(raw),editable=canEditDate();
    let editor=dateEditors.get(cell);
    if(!editor||editor.editable!==editable) {
      if(editor?.picker)calendarPopup.closeFor(editor.picker);
      cell.replaceChildren();
      editor={editable,dirty:false,saving:false,failed:false};
      if(editable) {
        const controls=el('div',null,'visit-date-editor'),control=el('div',null,'visit-date-control');
        const input=el('input');input.type='text';input.className='visit-date-input';input.inputMode='numeric';
        input.placeholder='ДД.ММ.ГГГГ';input.maxLength=10;input.autocomplete='off';
        const picker=el('button');picker.type='button';picker.className='visit-date-picker';
        picker.setAttribute('aria-expanded','false');picker.setAttribute('aria-controls','visit-calendar-popup');
        const svg=document.createElementNS('http://www.w3.org/2000/svg','svg'),path=document.createElementNS('http://www.w3.org/2000/svg','path');
        svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');svg.setAttribute('focusable','false');
        path.setAttribute('d','M8 2v4m8-4v4M3 10h18M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z');
        path.setAttribute('fill','none');path.setAttribute('stroke','currentColor');path.setAttribute('stroke-width','2');
        path.setAttribute('stroke-linecap','round');path.setAttribute('stroke-linejoin','round');
        svg.append(path);picker.append(svg);
        const status=el('small',null,'visit-date-status');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
        const clear=button('Очистить',()=>{
          calendarPopup.closeFor(picker);input.value='';picker.value='';editor.dirty=true;status.textContent='Нажмите «ОК», чтобы сохранить очистку.';
        });clear.className='visit-date-clear secondary';
        const ok=button('ОК',()=>persist());ok.className='visit-date-ok';
        editor.input=input;editor.picker=picker;editor.status=status;editor.clear=clear;editor.ok=ok;
        const actions=el('div',null,'visit-date-actions');
        control.append(input,picker);actions.append(clear,ok);controls.append(control,actions);cell.append(controls,status);
        const changed=()=>{
          editor.dirty=true;status.textContent='Нажмите «ОК», чтобы сохранить дату.';
        };
        input.addEventListener('input',()=>{
          const digits=input.value.replace(/\D/g,'').slice(0,8);
          input.value=digits.slice(0,2)+(digits.length>2?'.'+digits.slice(2,4):'')+(digits.length>4?'.'+digits.slice(4):'');
          picker.value=XataReview.parseDay(input.value)||'';changed();
        });
        picker.addEventListener('change',()=>{
          input.value=picker.value?XataReview.displayDay(picker.value):'';changed();
        });
        picker.addEventListener('click',()=>{
          if(editor.saving||!canEditDate())return;
          calendarPopup.toggle(picker,XataReview.parseDay(input.value),value=>{
            picker.value=value;picker.dispatchEvent(new Event('change',{bubbles:true}));input.focus({preventScroll:true});
          });
        });
        const persist=async()=>{
          if(editor.saving||!editor.baseline)return;
          calendarPopup.closeFor(picker);
          const text=input.value.trim(),selected=text?XataReview.parseDay(text):null;
          if(text&&!selected) {status.textContent='Укажите корректную дату в формате ДД.ММ.ГГГГ (2000–2100).';return;}
          if(selected===editor.original&&!editor.failed) {
            editor.dirty=false;editor.forceSync=true;status.textContent='Дата не изменена.';render();return;
          }
          const expected=editor.baseline;
          if(!editor.request)editor.request={property_id:expected.propertyId,visits_version:expected.revision,
            visit_id:expected.visitId,request_id:expected.requestId};
          const data={...editor.request,date:selected};
          editor.saving=true;status.textContent='Сохраняем дату…';
          for(const field of [input,picker,clear,ok])field.disabled=true;
          let saved=false;
          try {
            const result=await request('/api/listings/'+encodeURIComponent(expected.listingId)+'/visits/date',jsonOptions('PATCH',data));
            saveItem(result);editor.dirty=false;editor.failed=false;editor.request=null;editor.forceSync=true;saved=true;
            status.textContent='Дата сохранена.';
          } catch(e) {editor.failed=true;status.textContent=e.message;}
          finally {editor.saving=false;for(const field of [input,picker,clear,ok])field.disabled=false;render();}
          if(saved)await refresh();
        };
      } else {
        editor.time=el('time');cell.append(editor.time);
      }
      editor.caption=el('small');editor.overdue=el('small','Укажите результат просмотра','visit-overdue');
      cell.append(editor.caption,editor.overdue);dateEditors.set(cell,editor);
    }
    if(editor.editable) {
      editor.input.setAttribute('aria-label','Дата просмотра квартиры '+item.id+' (ДД.ММ.ГГГГ, Москва)');
      editor.picker.setAttribute('aria-label','Выбрать дату просмотра квартиры '+item.id);
      // Draft and original target survive input/change, blur and background polls.
      if(!editor.saving&&!editor.dirty&&!editor.failed&&(editor.forceSync||(!cell.contains(document.activeElement)&&!calendarPopup.owns(editor.picker)))) {
        editor.input.value=day?XataReview.displayDay(day):'';editor.picker.value=day||'';editor.original=day;
        editor.baseline={listingId:item.id,propertyId:item.property_id,revision:item.visits_version||0,
          visitId:shown?.id||null,requestId:shown?null:crypto.randomUUID()};
        editor.request=null;editor.forceSync=false;
      }
    } else {
      editor.time.textContent=XataReview.displayDay(day);
      if(day)editor.time.dateTime=day;else editor.time.removeAttribute('datetime');
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
