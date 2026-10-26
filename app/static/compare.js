/* Read-only comparison of 2–4 current properties. No ratings or write API. */
'use strict';
globalThis.XataCompare={create({getItem,render}) {
  const selected=new Set(),dialog=document.querySelector('#compare-dialog'),table=document.querySelector('#compare-table');
  const openButton=document.querySelector('#compare-open'),notice=document.querySelector('#compare-message');
  let signature='';
  const statusLabels={new:'НОВОЕ',liked:'Понравилось',viewed:'Просмотрено',rejected:'Не подходит'};
  const realtorLabels={pending:'Не оценено',approved:'Одобрено',rejected:'Отбраковано'};
  const outcomes={liked:'Понравилось',rejected:'Не понравилось',thinking:'Думаем'};
  function el(tag,text){const n=document.createElement(tag);if(text!=null)n.textContent=text;return n;}
  function text(value){return value==null||value===''?'—':String(value);}
  function money(value,suffix=' ₽'){return value==null?'—':Number(value).toLocaleString('ru-RU',{maximumFractionDigits:0})+suffix;}
  function localPhoto(value){return /^\/api\/listings\/[0-9]+\/cover\/[a-f0-9]{24}\/(?:thumb|normal)$/.test(value||'');}
  function date(value){return value?new Date(value).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',dateStyle:'short',timeStyle:'short'}):'—';}
  function announce(value){notice.textContent=value;document.querySelector('#compare-dialog-message').textContent=value;}
  function toggle(key,checked) {
    if(!checked)selected.delete(key);
    else if(selected.size<4&&getItem(key)&&XataReview.effectiveStatus(getItem(key))!=='rejected')selected.add(key);
    else if(selected.size>=4)announce('Можно сравнить не более четырёх квартир.');
    render();
  }
  function sync() {
    let rejected=0,missing=0;
    for(const key of selected) {
      const item=getItem(key);
      if(!item) {selected.delete(key);missing++;}
      else if(XataReview.effectiveStatus(item)==='rejected'){selected.delete(key);rejected++;}
    }
    if(rejected)announce('Квартира перемещена в «Не подходят» и убрана из сравнения.');
    else if(missing)announce('Состав объектов изменился. Недоступная квартира убрана из сравнения.');
    const count=selected.size;
    document.querySelector('#compare-count').textContent=count;
    openButton.disabled=count<2||count>4;
    document.querySelector('#compare-clear').disabled=count===0;
    if(dialog.open&&count<2)dialog.close();
    if(dialog.open)showTable();
  }
  function showTable() {
    const items=[...selected].map(getItem).filter(Boolean);
    const next=JSON.stringify(items);
    if(signature===next)return;
    signature=next;
    const scroll=document.querySelector('#compare-scroll'),left=scroll.scrollLeft;
    table.replaceChildren();
    const head=el('thead'),row=el('tr');row.append(el('th','Характеристика'));
    for(const item of items) {
      const th=el('th');th.scope='col';th.dataset.propertyId=item.property_id;
      const source=localPhoto(item.cover_thumb_src)?item.cover_thumb_src:null;
      const placeholder=el('span','—');placeholder.className='compare-placeholder';th.append(placeholder);
      if(source) {
        const img=el('img');img.className='compare-cover';img.alt='Заглавное фото квартиры '+item.id;
        img.loading='lazy';img.decoding='async';img.src=source;placeholder.hidden=true;
        img.addEventListener('error',()=>{img.hidden=true;placeholder.hidden=false;});th.append(img);
      }
      const link=el('a','ЦИАН '+item.id);
      if(/^https?:\/\/([a-z0-9-]+\.)*cian\.ru(?::(?:80|443))?\/sale\/flat\/\d+\/?(?:[?#].*)?$/i.test(item.url||''))link.href=item.url;
      link.target='_blank';link.rel='noopener noreferrer';th.append(link);row.append(th);
    }
    head.append(row);table.append(head);
    const body=el('tbody');
    const fields=[
      ['Адрес',item=>text(item.address)],['ЖК',item=>text(item.complex_name)],
      ['Цена',item=>money(item.price)],['Цена за м²',item=>money(XataReview.pricePerMeter(item),' ₽/м²')],
      ['Площадь',item=>item.area==null?'—':Number(item.area).toLocaleString('ru-RU')+' м²'],
      ['Этаж',item=>text(item.floor)],['Метро',item=>text(item.metro)],['До метро',item=>text(item.metro_time)],
      ['Ипотека / мес.',item=>{const value=XataReview.mortgage(item.price);return value==null?'—':'≈ '+money(Math.round(value/100)*100,' ₽/мес.');}],
      ['Семейный статус',item=>statusLabels[item.review_status]||'—'],
      ['Оценка риелтора',item=>realtorLabels[item.realtor_status]||'—'],
      ['Комментарий риелтора',item=>text(item.realtor_comment)],
      ['Последний состоявшийся просмотр',item=>date(XataReview.latestCompleted(item)?.completed_at)],
      ['Результат просмотра',item=>outcomes[XataReview.latestCompleted(item)?.outcome]||'—'],
      ['Комментарий просмотра',item=>text(XataReview.latestCompleted(item)?.comment)],
    ];
    for(const [label,read] of fields) {
      const tr=el('tr'),th=el('th',label);th.scope='row';tr.append(th);
      for(const item of items)tr.append(el('td',read(item)));
      body.append(tr);
    }
    table.append(body);scroll.scrollLeft=left;
  }
  openButton.addEventListener('click',()=>{sync();if(selected.size>=2){signature='';showTable();dialog.showModal();}});
  document.querySelector('#compare-close').addEventListener('click',()=>dialog.close());
  document.querySelector('#compare-clear').addEventListener('click',()=>{selected.clear();render();});
  return {toggle,sync,has:key=>selected.has(key)};
}};
