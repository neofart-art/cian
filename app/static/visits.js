/* One shared family result per viewing; editor drafts survive background polls. */
'use strict';
globalThis.XataVisits = {create({getItem,getUser,saveItem,request,jsonOptions,render,refresh}) {
  const outcomes={liked:'Понравилось',rejected:'Не понравилось',thinking:'Думаем'};
  const states={planned:'Запланирован',completed:'Состоялся',cancelled:'Отменён'};
  const zone='Europe/Moscow';
  const format=value=>value ? new Date(value).toLocaleString('ru-RU',{timeZone:zone,day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}) : 'Без предварительной записи';
  const canEdit=()=>['admin','reviewer'].includes(getUser()?.role);
  function el(tag,text,cls) {const n=document.createElement(tag); if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;}
  function button(text,action) {const b=el('button',text,'secondary compact');b.type='button';b.addEventListener('click',action);return b;}
  const dialog=document.querySelector('#visit-dialog'), form=document.querySelector('#visit-form');
  const date=document.querySelector('#visit-date'), state=document.querySelector('#visit-state'), outcome=document.querySelector('#visit-outcome');
  const preparation=document.querySelector('#visit-preparation'), comment=document.querySelector('#visit-comment');
  const error=document.querySelector('#visit-error'), submit=document.querySelector('#visit-save');
  let editing=null, saving=false;
  function stateFields() {
    const done=state.value==='completed';
    outcome.closest('label').hidden=comment.closest('label').hidden=!done;
    outcome.required=done; date.required=state.value==='planned';
    submit.textContent=done?'Сохранить итог':'Сохранить';
  }
  state.addEventListener('change',stateFields);
  function localDate(value) {
    if(!value)return '';
    const parts=new Intl.DateTimeFormat('sv-SE',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(value));
    return parts.replace(' ','T');
  }
  function open(key,visit=null,finish=false) {
    const item=getItem(key); if(!item||!canEdit())return;
    editing={key,listingId:item.id,propertyId:item.property_id,id:visit?.id,version:visit?.version||0};
    document.querySelector('#visit-title').textContent=visit?'Просмотр квартиры':'Новый просмотр';
    document.querySelector('#visit-address').textContent=item.address||item.title||'Квартира';
    date.value=localDate(visit?.scheduled_at);state.value=finish?'completed':(visit?.status||'planned');
    for(const option of state.options)option.disabled=visit?.status==='completed'&&option.value!=='completed';
    outcome.value=visit?.outcome||'';preparation.value=visit?.preparation||'';comment.value=visit?.comment||'';
    error.textContent='';stateFields();dialog.showModal();
  }
  document.querySelector('#visit-close').addEventListener('click',()=>{if(!saving)dialog.close();});
  dialog.addEventListener('cancel',event=>{if(saving)event.preventDefault();});
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(saving||!editing)return;
    const current=getItem(editing.key);
    if(!current||current.property_id!==editing.propertyId) {error.textContent='Объект изменился. Закройте окно и откройте его заново.';return;}
    const data={property_id:editing.propertyId,version:editing.version,status:state.value,
      scheduled_at:date.value?date.value+':00+03:00':null,
      outcome:state.value==='completed'?outcome.value:null,preparation:preparation.value,comment:comment.value};
    saving=true;submit.disabled=true;error.textContent='';
    try {
      const suffix=editing.id?'/'+editing.id:'';
      const saved=await request('/api/listings/'+encodeURIComponent(editing.listingId)+'/visits'+suffix,jsonOptions(editing.id?'PATCH':'POST',data));
      saveItem(saved);dialog.close();render();await refresh();
    } catch(e) {error.textContent=e.message;}
    finally {saving=false;submit.disabled=false;}
  });
  function record(item,visit) {
    const box=el('div',null,'visit-record');
    box.append(el('strong',format(visit.scheduled_at||visit.completed_at)),el('div',outcomes[visit.outcome]||states[visit.status],'visit-state'));
    if(visit.status==='planned'&&Date.parse(visit.scheduled_at)<Date.now()) box.append(el('small','Укажите результат просмотра','visit-overdue'));
    if(visit.preparation) {const d=el('details');d.append(el('summary','До просмотра'),el('p',visit.preparation));box.append(d);}
    if(visit.comment) {const d=el('details');d.append(el('summary',visit.comment.slice(0,70)+(visit.comment.length>70?'…':'')),el('p',visit.comment));box.append(d);}
    box.append(el('small',(visit.updated_by_name||visit.updated_by_username)+' · '+format(visit.updated_at),'visit-meta'));
    if(canEdit()) {
      const actions=el('div',null,'visit-actions');
      if(visit.status==='planned')actions.append(button('Итог просмотра',()=>open(XataReview.key(item),visit,true)));
      actions.append(button('Изменить',()=>open(XataReview.key(item),visit)));box.append(actions);
    }
    return box;
  }
  function renderCell(cell,item) {
    const signature=JSON.stringify([item.visits_version,item.visits,getUser()?.role,(item.visits||[]).map(v=>v.status==='planned'&&Date.parse(v.scheduled_at)<Date.now())]);
    if(cell.dataset.visitsSignature===signature)return;
    cell.dataset.visitsSignature=signature;
    const historyOpen=cell.querySelector('.visit-history')?.open;
    cell.replaceChildren();
    const visits=item.visits||[], nearest=XataReview.nextVisit(item);
    const latest=[...visits].filter(v=>v.status!=='planned').sort((a,b)=>Date.parse(b.completed_at||b.updated_at)-Date.parse(a.completed_at||a.updated_at)||b.id-a.id)[0];
    const shown=nearest||latest;
    cell.append(shown?record(item,shown):el('small','Не запланирован'));
    if(canEdit())cell.append(button(visits.length?'Назначить повторный':'Назначить / записать итог',()=>open(XataReview.key(item))));
    const history=visits.filter(v=>v.id!==shown?.id);
    if(history.length) {const details=el('details',null,'visit-history');details.open=!!historyOpen;details.append(el('summary','История просмотров ('+history.length+')'));for(const v of history)details.append(record(item,v));cell.append(details);}
  }
  function agenda(items) {
    const root=document.querySelector('#visit-agenda');
    const signature=JSON.stringify([getUser()?.role,items.map(item=>[item.id,item.property_id,item.address,item.title,item.url,item.visits_version,item.visits,(item.visits||[]).map(v=>v.status==='planned'&&Date.parse(v.scheduled_at)<Date.now())])]);
    if(root.dataset.visitsSignature===signature)return;
    root.dataset.visitsSignature=signature;root.replaceChildren();
    const entries=items.flatMap(item=>(item.visits||[]).filter(v=>v.status==='planned').map(v=>({item,v})))
      .sort((a,b)=>Date.parse(a.v.scheduled_at)-Date.parse(b.v.scheduled_at)||a.v.id-b.v.id);
    if(!entries.length)root.append(el('p','Нет запланированных просмотров.'));
    for(const {item,v} of entries) {
      const card=el('article',null,'panel visit-card');card.append(el('h3',item.address||item.title));
      if(/^https?:\/\/([a-z0-9-]+\.)*cian\.ru(?::(?:80|443))?\/sale\/flat\/\d+\/?(?:[?#].*)?$/i.test(item.url||'')) {
        const link=el('a','Открыть ЦИАН');link.href=item.url;link.target='_blank';link.rel='noopener noreferrer';card.append(link);
      }
      card.append(record(item,v));root.append(card);
    }
  }
  return {renderCell,agenda};
}};
