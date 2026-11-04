'use strict';
const ourPage=document.body.dataset.page==='ours', pageMode=ourPage?'ours':'all';
const form = document.querySelector('#filters');
const button = document.querySelector('#collect');
const status = document.querySelector('#status');
const message = document.querySelector('#page-message');
const records = new Map(), rows = new Map(), pendingReviews = new Set(), pendingRealtors = new Set(), pendingDuplicates = new Set(), pendingClassifications = new Set(), pendingSelections = new Set();
const labels = {new:'НОВОЕ', liked:'Понравилось', viewed:'Просмотрено', rejected:'Не подходит'};
const realtorLabels = {pending:'Не оценено',approved:'Одобрено',rejected:'Отбраковано'};
let searchText='', selectedSort = null, identityRevision = 0, refreshPromise = null;
let listLoaded=false, listTag=null, feedEpoch='', feedRevision=-1, refreshAgain=false, refreshController=null, pollTimer=null;
let globalTotals={all:null,ours:null};
const detailRequests=new Map(),detailFailures=new Map();
let currentUser = null, csrfToken = '', pending = false, refreshing = false, selectedFilter = 'all';

function setText(node, value) {
  if(!node)return;
  const text = String(value ?? '');
  if (node.textContent !== text) node.textContent = text;
}
function identify(user) {
  currentUser = user;
  setText(document.querySelector('#identity'), user.display_name + ' · ' + ({admin:'Администратор',reviewer:'Просмотр',realtor:'Риелтор'}[user.role]));
  for (const node of document.querySelectorAll('[data-admin]')) node.hidden = user.role !== 'admin';
}
async function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (['POST','PATCH','PUT','DELETE'].includes((options.method || 'GET').toUpperCase())) headers.set('X-CSRF-Token', csrfToken);
  const response = await fetch(url, {...options, headers, credentials:'same-origin'});
  if (response.status === 401) {
    window.location.assign('/login');
    throw new Error('Сессия завершена. Войдите заново.');
  }
  return response;
}
async function responseJson(response) {
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail :
    (response.status===422?'Не удалось сохранить: проверьте поля и обновите страницу, если открыта старая версия формы.':'Не удалось выполнить запрос. Попробуйте ещё раз.'));
  return data;
}
function jsonOptions(method, data) {
  return {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)};
}
const visitUI = ourPage ? XataVisits.create({getItem:key=>records.get(key),getUser:()=>currentUser,
  saveItem:item=>{acceptListing(item);setText(message,'Просмотр сохранён.');message.className='';},request:async(url,options)=>responseJson(await apiFetch(url,options)),
  jsonOptions,render:renderListings,refresh:()=>refresh(true),ensureDetails}) : null;
const commentOptions={getItem:key=>records.get(key),getUser:()=>currentUser,
  request:async(url,options)=>responseJson(await apiFetch(url,options)),jsonOptions,
  accept:acceptListing,refresh:()=>refresh(true),render:renderListings,ensureDetails};
const realtorUI=XataComments.create({...commentOptions,kind:'realtor'});
const familyUI=XataComments.create({...commentOptions,kind:'family'});
const comparisonUI = ourPage ? XataCompare.create({getItem:key=>{const item=records.get(key);return item?.in_our_list?item:null;},render:renderListings,ensureDetails}) : null;
function cell(row) { const td = document.createElement('td'); row.append(td); return td; }
function safeCianUrl(value) {
  return /^https?:\/\/([a-z0-9-]+\.)*cian\.ru(?::(?:80|443))?\/sale\/flat\/\d+\/?(?:[?#].*)?$/i.test(value || '');
}
function localCover(value) {
  return /^\/api\/listings\/[0-9]+\/cover\/[a-f0-9]{24}\/(?:normal|thumb)$/.test(value || '') ? value : null;
}
const coverDialog = document.querySelector('#cover-dialog'), largeCover = document.querySelector('#cover-large');
const largePlaceholder = document.querySelector('#cover-large-placeholder');
function openCover(id) {
  const item=records.get(id), source=localCover(item?.cover_image_src);
  if (!item || !source) return;
  setText(document.querySelector('#cover-title'),item.complex_name || item.title || 'Квартира');
  setText(document.querySelector('#cover-address'),item.address || '');
  setText(document.querySelector('#cover-price'),item.price == null ? 'Цена не указана' : Number(item.price).toLocaleString('ru-RU') + ' ₽');
  const link=document.querySelector('#cover-cian');
  link.textContent='ЦИАН ' + item.id;
  if (safeCianUrl(item.url)) link.href=item.url; else link.removeAttribute('href');
  largeCover.hidden=false; largePlaceholder.hidden=true; largeCover.alt='Заглавное фото квартиры ' + item.id;
  largeCover.src=source;
  coverDialog.showModal();
}
largeCover.addEventListener('error',()=>{largeCover.hidden=true; largePlaceholder.hidden=false;});
document.querySelector('#cover-close').addEventListener('click',()=>coverDialog.close());
coverDialog.addEventListener('close',()=>largeCover.removeAttribute('src'));
function makeRow(id) {
  const row = document.createElement('tr');
  row.dataset.listingId = id;
  const cells = Array.from({length:14}, () => cell(row));
  const cellLabels=['Фото','Номер ЦИАН / ссылка','Объект / адрес','Цена','Ипотека / мес.','Площадь','Этаж','Метро','До метро','Добавлено','Семейная оценка','Риелтор','Дата просмотра','Результат просмотра'];
  cells.forEach((td,i)=>{td.dataset.label=cellLabels[i];td.className='listing-col-'+i;});
  cells[11].classList.add('realtor-cell');cells[0].classList.add('photo-cell');
  const link = document.createElement('a');
  link.target = '_blank'; link.rel = 'noopener noreferrer';
  cells[1].append(link);
  cells[2].className = 'object-cell';
  const title = document.createElement('strong'), address = document.createElement('small');
  const propertyHistory = document.createElement('small'); propertyHistory.className = 'property-history';
  const duplicateBox = document.createElement('div'); duplicateBox.className = 'duplicate-box';
  const duplicateText = document.createElement('div');
  const duplicateActions = document.createElement('div'); duplicateActions.className = 'duplicate-actions';
  const duplicateSame = document.createElement('button'); duplicateSame.type='button'; duplicateSame.textContent='Да, тот же объект';
  const duplicateDifferent = document.createElement('button'); duplicateDifferent.type='button'; duplicateDifferent.className='secondary'; duplicateDifferent.textContent='Нет, другая квартира';
  duplicateActions.append(duplicateSame,duplicateDifferent); duplicateBox.append(duplicateText,duplicateActions);
  const historyDetails = document.createElement('details'), historySummary = document.createElement('summary'), historyText = document.createElement('p');
  historyDetails.className = 'publication-details'; historySummary.textContent='Публикации и цены';
  historyDetails.append(historySummary,historyText);
  historyDetails.addEventListener('toggle',()=>{if(historyDetails.open)loadRowDetails(id,historyText);});
  const duplicateCompare = document.createElement('a'); duplicateCompare.target='_blank'; duplicateCompare.rel='noopener noreferrer';
  duplicateCompare.textContent='Сравнить на ЦИАН'; duplicateBox.insertBefore(duplicateCompare,duplicateActions);
  const groupLabel=document.createElement('span');groupLabel.className='viewed-group-label';
  const stageButton=document.createElement('select');stageButton.className='viewed-stage-select';
  for(const [value,label] of [['liked','Оставляем в запасе'],['documents','Проверка документов']]){
    const option=document.createElement('option');option.value=value;option.textContent=label;stageButton.append(option);
  }
  stageButton.addEventListener('change',()=>changeClassification(id,stageButton.value==='documents'));
  const stageCell=document.createElement('td');stageCell.className='selection-stage-cell';stageCell.dataset.label='Этап отбора';stageCell.append(groupLabel,stageButton);
  const membership=document.createElement('span');membership.className='membership-label';membership.textContent='В нашем списке';
  const selectionButton=document.createElement('button');selectionButton.type='button';selectionButton.className='secondary selection-toggle';
  selectionButton.addEventListener('click',()=>changeSelection(id));
  const characteristics=document.createElement('details');characteristics.className='characteristics';
  const characteristicsSummary=document.createElement('summary');characteristicsSummary.textContent='Характеристики';
  const characteristicsText=document.createElement('p');characteristics.append(characteristicsSummary,characteristicsText);
  if(ourPage){
    characteristics.append(propertyHistory,historyDetails);
    cells[2].append(title,address,link,membership,selectionButton,characteristics,duplicateBox);
    row.replaceChildren(cells[11],cells[0],cells[2],cells[3],cells[12],cells[13],stageCell);
    cells[12].className='viewing-date-cell';cells[13].className='viewing-result-cell';
  } else {
    characteristics.append(propertyHistory,historyDetails);
    cells[2].append(title,address,membership,selectionButton,characteristics,duplicateBox);
    row.replaceChildren(cells[11],...cells.slice(0,8),cells[9],cells[10]);
  }
  const coverButton = document.createElement('button'); coverButton.type='button'; coverButton.className='cover-button';
  const coverImage = document.createElement('img'); coverImage.className='listing-cover'; coverImage.loading='lazy';
  coverImage.decoding='async'; coverImage.width=110; coverImage.height=80;
  const coverPlaceholder = document.createElement('span'); coverPlaceholder.className='cover-placeholder'; coverPlaceholder.textContent='Нет фото';
  coverButton.append(coverImage); cells[0].append(coverButton,coverPlaceholder);
  coverButton.addEventListener('click',()=>openCover(id));
  const compareLabel=document.createElement('label');compareLabel.className='compare-choice';
  const compareInput=document.createElement('input');compareInput.type='checkbox';compareInput.className='compare-select';
  compareLabel.append(compareInput,document.createTextNode('В сравнение'));if(ourPage)cells[0].append(compareLabel);
  compareInput.addEventListener('change',()=>comparisonUI.toggle(id,compareInput.checked));
  const date = document.createElement('time'), clock = document.createElement('small'); cells[9].append(date, clock);
  const badge = document.createElement('span'); badge.className = 'review-label';
  const select = document.createElement('select'); select.className = 'review-select';
  select.setAttribute('aria-label', 'Статус объявления ' + id);
  for (const [value,label] of Object.entries(labels)) { const option = document.createElement('option'); option.value = value; option.textContent = label; select.append(option); }
  const meta = document.createElement('small'); meta.className = 'review-meta';
  const saving = document.createElement('div'); saving.className = 'review-saving'; saving.setAttribute('aria-live', 'polite');
  select.addEventListener('change', () => changeReview(id, select.value));
  select.addEventListener('blur', () => renderListings());
  const returnHint=document.createElement('small');returnHint.className='return-hint';
  cells[10].append(badge, select, meta, saving,returnHint);
  const realtorComment=realtorUI.element(id),familyComment=familyUI.element(id);
  cells[11].append(realtorComment);
  const familyCell=document.createElement('td');familyCell.className='family-comment-cell';familyCell.dataset.label='Наш комментарий';familyCell.append(familyComment);
  const familyDetails=document.createElement('details');familyDetails.className='family-details';
  familyDetails.addEventListener('toggle',()=>{if(familyDetails.open)ensureDetails(id,true).catch(error=>setText(message,error.message));});
  const familySummary=document.createElement('summary');familySummary.textContent='Наш комментарий';
  if(ourPage)row.insertBefore(familyCell,stageCell);
  else{familyDetails.append(familySummary,familyComment);characteristics.append(familyDetails);}
  const entry = {row,cells,membership,groupLabel,stageButton,selectionButton,characteristicsText,familyDetails,compareInput,compareLabel,returnHint,link,title,address,propertyHistory,coverButton,coverImage,coverPlaceholder,failedCover:null,failedCoverAt:0,historyDetails,historyText,duplicateCompare,duplicateBox,duplicateText,duplicateActions,
    duplicateSame,duplicateDifferent,date,clock,badge,select,meta,saving};
  coverImage.addEventListener('error',()=>{
    entry.failedCover=coverImage.getAttribute('src'); entry.failedCoverAt=Date.now();
    coverButton.hidden=true; coverPlaceholder.hidden=false;
  });
  coverImage.addEventListener('load',()=>{
    entry.failedCover=null; entry.failedCoverAt=0;
    coverButton.hidden=false; coverPlaceholder.hidden=true;
  });
  duplicateSame.addEventListener('click', () => decideDuplicate(id,'same'));
  duplicateDifferent.addEventListener('click', () => decideDuplicate(id,'different'));
  rows.set(id, entry);
  return entry;
}
function renderRow(item, forceReview = false) {
  const id = XataReview.key(item);
  const entry = rows.get(id) || makeRow(id);
  entry.row.dataset.listingId = item.id;
  entry.row.dataset.propertyId = item.property_id || '';
  entry.select.setAttribute('aria-label','Статус объявления ' + item.id);
  const {row,cells,link,title,address,date,clock,badge,select,meta,saving} = entry;
  setText(link, (ourPage?'ЦИАН ':'')+item.id);
  if (safeCianUrl(item.url)) link.href = item.url; else link.removeAttribute('href');
  setText(title, item.complex_name || item.title || 'Квартира');
  setText(address, item.address);
  const thumb = localCover(item.cover_thumb_src);
  // A missing local file can be restored at the same content URL. Retry
  // failures at a bounded cadence instead of keeping a permanent placeholder.
  const retryCover = thumb && entry.failedCover === thumb && Date.now()-entry.failedCoverAt >= 30000;
  const usableCover = thumb && (entry.failedCover !== thumb || retryCover);
  entry.coverButton.hidden=!usableCover; entry.coverPlaceholder.hidden=!!usableCover;
  entry.coverButton.setAttribute('aria-label','Открыть фото объявления ' + item.id);
  entry.coverImage.alt='Заглавное фото квартиры ' + item.id;
  if (thumb && (entry.coverImage.getAttribute('src') !== thumb || retryCover)) {
    entry.failedCover=null; entry.coverImage.src=thumb;
  } else if (!thumb) entry.coverImage.removeAttribute('src');
  const ids = item.listing_ids || [item.id];
  const relists = Number(item.relist_count || 0);
  setText(entry.propertyHistory, relists ? 'Перепубликовано ' + relists + ' ' + (relists === 1 ? 'раз' : 'раза') + ' · CIAN: ' + ids.join(' → ') : '');
  entry.propertyHistory.hidden = !relists;
  const publications = (item.publication_history || []).map(entry => 'CIAN ' + entry.id + ' · ' +
    (entry.status === 'current' ? 'Текущая публикация' : 'Историческая публикация') + ' · ' + entry.first_seen + ' — ' + entry.last_seen);
  const prices = (item.price_history || []).map(entry => new Date(entry.observed_at).toLocaleString('ru-RU') + ' · CIAN ' +
    entry.listing_id + ' · ' + Number(entry.price).toLocaleString('ru-RU') + ' ₽');
  if(item.details_loaded)setText(entry.historyText,publications.join('\n') + (prices.length ? '\nИстория цен:\n' + prices.join('\n') : ''));
  else if(!entry.historyText.textContent)setText(entry.historyText,'Подробности загрузятся при открытии.');
  entry.historyDetails.hidden = !item.listing_ids?.length && !item.price_history_count && !publications.length && !prices.length;
  const candidate = item.duplicate_candidate;
  entry.duplicateBox.hidden = !candidate;
  if (candidate) {
    setText(entry.duplicateText, 'Возможно, это перепубликация CIAN ' + candidate.candidate_listing_id + ' · совпадение ' + candidate.confidence + '%');
    if (safeCianUrl(candidate.candidate_url)) entry.duplicateCompare.href=candidate.candidate_url; else entry.duplicateCompare.removeAttribute('href');
    const canDecide = ['admin','reviewer'].includes(currentUser?.role);
    entry.duplicateActions.hidden = !canDecide;
    entry.duplicateSame.disabled = entry.duplicateDifferent.disabled = pendingDuplicates.has(id);
  }
  setText(cells[3], item.price == null ? '—' : new Intl.NumberFormat('ru-RU', {style:'currency', currency:'RUB', maximumFractionDigits:0}).format(item.price));
  const payment = XataReview.mortgage(item.price);
  setText(cells[4], payment == null ? '—' : (payment > 0 ? '≈ ' : '') + (Math.round(payment / 100) * 100).toLocaleString('ru-RU') + ' ₽/мес.');
  setText(cells[5], item.area == null ? '—' : item.area + ' м²');
  setText(cells[6], item.floor || '—'); setText(cells[7], item.metro || '—'); setText(cells[8], item.metro_time || '—');
  const first = item.first_seen ? new Date(item.first_seen) : null;
  setText(date, first ? first.toLocaleDateString('ru-RU') : '—');
  date.dateTime = item.first_seen || '';
  setText(clock, first ? first.toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'}) : '');
  const review = item.review_status || 'new';
  const effectiveStatus = XataReview.effectiveStatus(item);
  const group=ourPage?XataReview.section(item,'ours'):null,legacy=XataReview.latestCompleted(item)?.outcome==='thinking';
  row.className = 'review-' + effectiveStatus + (group?' viewed-'+group:'')+(!ourPage&&item.in_our_list?' in-our-list':'');
  entry.membership.hidden=!item.in_our_list;
  row.dataset.viewedGroup=group||'';
  const groupLabels={planned:'К просмотру',liked:'Оставляем в запасе',documents:'Проверка документов',rejected:'Не понравилось'};
  entry.groupLabel.hidden=!ourPage;
  setText(entry.groupLabel,legacy?'Думаем — уточните результат':(groupLabels[group]||''));
  entry.stageButton.hidden=!['liked','documents'].includes(group)||!['admin','reviewer'].includes(currentUser?.role);
  entry.stageButton.disabled=pendingClassifications.has(id);
  entry.stageButton.setAttribute('aria-label','Этап отбора квартиры '+item.id);
  if(!pendingClassifications.has(id))entry.stageButton.value=group==='documents'?'documents':'liked';
  entry.selectionButton.hidden=!['admin','reviewer'].includes(currentUser?.role);
  entry.selectionButton.disabled=pendingSelections.has(id);
  setText(entry.selectionButton,item.in_our_list?'Убрать из нашего списка':'В наш список');
  entry.selectionButton.setAttribute('aria-label',(item.in_our_list?'Убрать из нашего списка: квартира ':'В наш список: квартира ')+item.id);
  setText(entry.characteristicsText,'Площадь: '+(item.area==null?'—':item.area+' м²')+'\nЭтаж: '+(item.floor||'—')+'\nМетро: '+(item.metro||'—')+'\nДо метро: '+(item.metro_time||'—'));
  row.dataset.reviewStatus = review;
  setText(badge, labels[review]);
  if (forceReview || (!pendingReviews.has(id) && document.activeElement !== select)) select.value = review;
  select.hidden = currentUser?.role === 'realtor';
  select.disabled = pendingReviews.has(id) || currentUser?.role === 'realtor';
  setText(saving, pendingReviews.has(id) ? 'Сохраняем…' : '');
  let author = item.review_updated_by_name || item.review_updated_by_username;
  if (!author && item.review_updated_by) author = 'Пользователь #' + item.review_updated_by;
  const changed = item.review_updated_at ? new Date(item.review_updated_at) : null;
  setText(meta, changed ? (author || '') + ' · ' + changed.toLocaleString('ru-RU', {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'}) : '');
  meta.title = changed ? changed.toLocaleString('ru-RU') : '';
  realtorUI.update(id);familyUI.update(id);
  entry.familyDetails.hidden=!item.family_comment&&!item.comment_history_count?.family&&!(item.comment_history||[]).some(h=>h.kind==='family');
  visitUI?.renderCells(cells[12],cells[13],item);
  entry.compareLabel.hidden=!XataReview.comparisonEligible(item);
  entry.compareInput.checked=!!comparisonUI?.has(id);
  entry.compareInput.setAttribute('aria-label','В сравнение: квартира '+item.id);
  entry.returnHint.hidden=!!group||item.realtor_status!=='rejected';
  setText(entry.returnHint,'Возврат блокирует отказ риелтора. Его оценку меняет риелтор или администратор.');
  row.hidden = !ourPage && XataReview.section(item,pageMode)==='main' && selectedFilter !== 'all' && (selectedFilter === 'planned' || selectedFilter !== effectiveStatus);
  row.hidden=row.hidden||!matchesSearch(item);
  return row;
}
function matchesSearch(item) {
  return !searchText||[item.id,...(item.listing_ids||[]),item.title,item.complex_name,item.address,item.metro]
    .filter(Boolean).join(' ').toLocaleLowerCase('ru').includes(searchText);
}
document.querySelector('#listing-search')?.addEventListener('input',event=>{
  searchText=event.target.value.trim().toLocaleLowerCase('ru');renderListings();
});
function renderListings() {
  if(!listLoaded)return;
  const all=XataReview.ordered(records.values(),selectedSort);
  const pageItems=all.filter(item=>XataReview.section(item,pageMode)!==null);
  const ordered=pageItems.filter(matchesSearch);
  const destinations=ourPage
    ? new Map(['planned','liked','documents','rejected'].map(group=>[group,document.querySelector('#viewed-'+group+'-listings')]))
    : new Map([['main',document.querySelector('#listings')],['refused',document.querySelector('#rejected-listings')]]);
  const active=ordered.filter(item=>XataReview.section(item,pageMode)==='main');
  const counts=ourPage?{all:ordered.length,planned:XataReview.agenda(ordered).length}:XataReview.counts(active);
  if(!ourPage)counts.all=ordered.length; // Includes apartments in the collapsed rejection section.
  const planned=ourPage&&selectedFilter==='planned';
  if(ourPage){
    document.querySelector('#viewed-section').hidden=planned;
    document.querySelector('#visit-agenda').hidden=!planned;
    if(planned)visitUI.agenda(ordered);
  }
  comparisonUI?.sync();
  for(const item of all) {
    const target=destinations.get(XataReview.section(item,pageMode)),key=XataReview.key(item);
    if(!target){rows.get(key)?.row.remove();continue;}
    const row=renderRow(item);
    if(row.parentNode!==target&&!(row.isConnected&&(realtorUI.editing(key)||familyUI.editing(key))))target.append(row);
  }
  if(![...rows.keys()].some(key=>realtorUI.editing(key)||familyUI.editing(key))&&!pendingReviews.size&&!pendingRealtors.size&&!pendingDuplicates.size&&!pendingClassifications.size&&!pendingSelections.size&&!document.activeElement?.matches('.review-select,.realtor-select,.realtor-comment,.family-comment,.visit-date-input,.visit-date-picker,.visit-date-clear,.visit-date-ok,.viewed-stage-select')) {
    for(const [section,target] of destinations) {
      let cursor=target.firstElementChild;
      for(const item of ordered.filter(item=>XataReview.section(item,pageMode)===section)) {
        const row=rows.get(XataReview.key(item)).row;
        if(row!==cursor)target.insertBefore(row,cursor);
        cursor=row.nextElementSibling;
      }
    }
  }
  for(const node of document.querySelectorAll('[data-review-filter]')) {
    node.setAttribute('aria-pressed',String(node.dataset.reviewFilter===selectedFilter));
    setText(node.querySelector('span'),counts[node.dataset.reviewFilter]);
  }
  setText(document.querySelector('#count'),pageItems.length);
  const found=ordered.filter(item=>ourPage||XataReview.section(item,pageMode)!=='main'||selectedFilter==='all'||XataReview.effectiveStatus(item)===selectedFilter).length;
  setText(document.querySelector('#found-count'),'Найдено: '+found+' из '+pageItems.length);
  setText(document.querySelector('#rejected-count'),ordered.length-active.length);
  for(const group of ['planned','liked','documents','rejected'])
    setText(document.querySelector('#viewed-'+group+'-count'),ordered.filter(item=>XataReview.section(item,pageMode)===group).length);
  const selected=all.filter(item=>item.in_our_list).length;
  setText(document.querySelector('#nav-ours-count'),globalTotals.ours??selected);
  setText(document.querySelector('#nav-all-count'),globalTotals.all??all.length);
  const empty=document.querySelector('#empty');
  empty.hidden=ourPage?ordered.length>0:(counts[selectedFilter]||0)>0;
  setText(empty,ourPage?'В нашем списке пока нет квартир. Выберите варианты на странице «Все объявления».':
    active.length?'Нет объявлений с выбранным статусом.':'В основном списке пока нет объявлений.');
}

function acceptListing(item) {
  if (Number(item.identity_revision || 0) < identityRevision) return;
  if(item.feed_epoch===feedEpoch&&Number(item.feed_revision??feedRevision)<feedRevision)return;
  if(item.feed_epoch){feedEpoch=item.feed_epoch;feedRevision=Math.max(feedRevision,Number(item.feed_revision||0));}
  identityRevision = Math.max(identityRevision,Number(item.identity_revision || 0));
  const key = XataReview.key(item);
  const previous=records.get(key);
  if(previous&&previous.in_our_list!==item.in_our_list&&globalTotals.ours!=null)globalTotals.ours+=item.in_our_list?1:-1;
  records.set(key,XataReview.merge(previous,item));
}
async function changeClassification(id, documentsReady) {
  if(pendingClassifications.has(id)||!records.has(id))return;
  const item=records.get(id);
  pendingClassifications.add(id);renderRow(item);
  try {
    const saved=await responseJson(await apiFetch('/api/listings/'+encodeURIComponent(item.id)+'/viewing-classification',
      jsonOptions('PATCH',{property_id:item.property_id,visits_version:item.visits_version,documents_ready:documentsReady})));
    acceptListing(saved);setText(message,'Группа просмотренной квартиры сохранена.');message.className='';
  } catch(error) {
    setText(message,error.message);message.className='error';
    await refresh(true);
  } finally {pendingClassifications.delete(id);renderListings();}
}
async function changeSelection(id) {
  if(pendingSelections.has(id)||!records.has(id))return;
  if(realtorUI.editing(id)||familyUI.editing(id)){
    setText(message,'Сначала сохраните комментарий риелтора.');message.className='error';return;
  }
  const item=records.get(id);pendingSelections.add(id);renderRow(item);
  try{
    const saved=await responseJson(await apiFetch('/api/listings/'+encodeURIComponent(item.id)+'/selection',
      jsonOptions('PATCH',{property_id:item.property_id,selection_version:item.selection_version,selected:!item.in_our_list})));
    acceptListing(saved);setText(message,saved.in_our_list?'Квартира добавлена в наш список и остаётся во всех объявлениях.':'Квартира убрана из нашей подборки. Данные и история сохранены.');message.className='';
  }catch(error){setText(message,error.message);message.className='error';await refresh(true);}
  finally{pendingSelections.delete(id);renderListings();}
}
async function changeReview(id, nextStatus) {
  if (pendingReviews.has(id) || !records.has(id)) return;
  pendingReviews.add(id); renderRow(records.get(id));
  try {
    const saved = await responseJson(await apiFetch('/api/listings/' + encodeURIComponent(records.get(id).id) + '/review', jsonOptions('POST', {status:nextStatus})));
    acceptListing(saved);
    setText(message, 'Статус сохранён.'); message.className = '';
  } catch(error) {
    setText(message, error.message); message.className = 'error';
  } finally {
    pendingReviews.delete(id);
    if (records.has(id)) renderRow(records.get(id),true);
    renderListings();
  }
}
for (const node of document.querySelectorAll('[data-review-filter]')) node.addEventListener('click', () => { selectedFilter = node.dataset.reviewFilter; renderListings(); });
async function decideDuplicate(id, decision) {
  if (pendingDuplicates.has(id) || !records.has(id)) return;
  if (realtorUI.editing(id)||familyUI.editing(id)) {
    setText(message,'Сначала сохраните комментарий риелтора перед решением по дублю.'); message.className='error';
    return;
  }
  pendingDuplicates.add(id); renderRow(records.get(id));
  try {
    const candidate = records.get(id).duplicate_candidate;
    const saved = await responseJson(await apiFetch('/api/listings/' + encodeURIComponent(records.get(id).id) + '/duplicate-decision',
      jsonOptions('POST',{decision,candidate_id:candidate.id,source_property_id:candidate.source_property_id,candidate_property_id:candidate.candidate_property_id})));
    identityRevision = Math.max(identityRevision,Number(saved.identity_revision || 0));
    setText(message, decision === 'same' ? 'Перепубликации объединены в один объект.' : 'Объявления отмечены как разные квартиры.');
    message.className='';
    await refresh(true);
  } catch(error) {
    setText(message,error.message); message.className='error';
  } finally {
    pendingDuplicates.delete(id);
    renderListings();
  }
}
function updateSort(sort) {
  selectedSort=sort;
  for(const header of document.querySelectorAll('[data-sort]')) {
    const direction=selectedSort?.key===header.dataset.sort?selectedSort.direction:null;
    header.closest('th').setAttribute('aria-sort',direction==='asc'?'ascending':direction==='desc'?'descending':'none');
    setText(header.querySelector('[data-sort-indicator]'),direction==='asc'?' ↑':direction==='desc'?' ↓':'');
  }
  const mobile=document.querySelector('#mobile-sort');
  if(mobile)mobile.value=sort?sort.key+':'+sort.direction:'';
  renderListings();
}
for(const node of document.querySelectorAll('[data-sort]'))node.addEventListener('click',()=>updateSort(XataReview.cycleSort(selectedSort,node.dataset.sort)));
document.querySelector('#mobile-sort')?.addEventListener('change',event=>{
  const [key,direction]=event.target.value.split(':');updateSort(key?{key,direction}:null);
});
function setLoadError(error) {
  if(error?.name==='AbortError')return;
  const node=document.querySelector('#load-state');node.hidden=false;node.className='error';
  setText(document.querySelector('#load-message'),(listLoaded?'Не удалось обновить список. Показаны предыдущие данные. ':'Не удалось загрузить объявления. ')+error.message);
  document.querySelector('#load-retry').hidden=false;
}
function loadingComplete(){
  listLoaded=true;document.querySelector('#load-state').hidden=true;
  document.querySelector('.results').setAttribute('aria-busy','false');
}
async function ensureDetails(key,force=false){
  const item=records.get(key);if(!item)throw new Error('Квартира больше недоступна.');
  if(item.details_loaded)return item;
  if(detailRequests.has(key))return detailRequests.get(key);
  if(!force&&detailFailures.has(key)&&Date.now()-detailFailures.get(key)<10000)throw new Error('Подробности недоступны. Повторите открытие через несколько секунд.');
  const task=(async()=>{
    try{
      const data=await responseJson(await apiFetch('/api/listings/'+encodeURIComponent(item.id)+'/details'));
      if(data.feed_epoch===feedEpoch&&data.feed_revision<feedRevision)throw new Error('Подробности обновились. Повторите открытие.');
      acceptListing(data);detailFailures.delete(key);renderListings();
      return records.get(key);
    }catch(error){detailFailures.set(key,Date.now());throw error;}
    finally{detailRequests.delete(key);}
  })();
  detailRequests.set(key,task);return task;
}
async function loadRowDetails(key,node){
  try{await ensureDetails(key);renderListings();}
  catch(error){setText(node,error.message+' Закройте и откройте раздел для повтора.');}
}
async function refresh(force=false){
  refreshAgain=refreshAgain||force;
  if(refreshPromise)return refreshPromise;
  if(document.hidden&&!force)return;
  refreshPromise=(async()=>{
    do{const requested=refreshAgain;refreshAgain=false;await refreshState(requested);}
    while(refreshAgain&&!document.hidden);
  })();
  try{await refreshPromise;}finally{refreshPromise=null;}
}
async function refreshState(force=false) {
  refreshing = true;
  try {
    refreshController=new AbortController();
    const response=await apiFetch('/api/state?view='+pageMode,{signal:refreshController.signal,
      headers:!force&&listTag?{'If-None-Match':listTag}:{}});
    if(response.status===304){
      for(const [key,entry] of rows)if(entry.failedCover&&Date.now()-entry.failedCoverAt>=30000)renderRow(records.get(key));
      loadingComplete();return;
    }
    const data = await responseJson(response);
    if(data.feed_epoch===feedEpoch&&Number(data.feed_revision||0)<feedRevision){refreshAgain=true;return;}
    feedEpoch=data.feed_epoch;feedRevision=Number(data.feed_revision||0);
    listTag=response.headers.get('etag');globalTotals=data.totals||globalTotals;
    identify(data.user);
    setText(document.querySelector('#last-sync'), 'Последняя успешная синхронизация с ЦИАН: ' + (data.last_success ? new Date(data.last_success).toLocaleString('ru-RU') : 'ещё не было'));
    const criteria = data.criteria || {max_price:14500000,metro_minutes:20};
    setText(document.querySelector('#criteria-price'),'Бюджет до ' + (criteria.max_price / 1000000).toLocaleString('ru-RU') + ' млн ₽');
    setText(document.querySelector('#criteria-metro'),'До метро — не более ' + criteria.metro_minutes + ' минут пешком');
    if (Number(data.identity_revision || 0) < identityRevision) return;
    const next = XataReview.reconcile(records,data.listings,identityRevision,data.identity_revision);
    identityRevision = Number(data.identity_revision || 0);
    records.clear();
    for (const [key,item] of next) records.set(key,item);
    for (const [key,entry] of rows) {
      if (!records.has(key)) {entry.row.remove(); rows.delete(key);}
    }
    loadingComplete();renderListings();
    for(const [key,entry] of rows){
      if(!records.get(key)?.details_loaded&&(entry.historyDetails.open||entry.familyDetails.open||realtorUI.needsDetails(key)||familyUI.needsDetails(key)||entry.row.querySelector('.visit-history[open]'))&&!detailRequests.has(key)&&!detailFailures.has(key))loadRowDetails(key,entry.historyText);
    }
    if (currentUser.role !== 'admin' || ourPage) return;
    const browser = data.browser || {};
    const states = {authorized:'Авторизован', login_required:'Требуется вход', confirmation_required:'Требуется подтверждение/CAPTCHA', error:'Ошибка browser collector'};
    setText(document.querySelector('#browser-state'), states[browser.status] || browser.message || 'Состояние неизвестно');
    setText(document.querySelector('#browser-success'), browser.last_success ? new Date(browser.last_success).toLocaleString('ru-RU') : 'Ещё не было');
    setText(document.querySelector('#browser-new'), browser.new_count ?? 0);
    setText(document.querySelector('#browser-error'), browser.last_error || '—');
    const run = data.run;
    status.className = run?.status === 'error' ? 'error' : run?.status === 'blocked' ? 'blocked' : '';
    setText(status, run ? run.message + (run.status === 'done' ? ' Получено: ' + run.count + '.' : '') + '\nНачало: ' + new Date(run.started).toLocaleString('ru-RU') : 'Сбор ещё не запускался.');
    button.disabled = pending || run?.status === 'running';
  } catch(error) { setLoadError(error); }
  finally { refreshing = false;refreshController=null; }
}
form?.addEventListener('submit', async event => {
  event.preventDefault(); pending = true; button.disabled = true;
  try {
    await responseJson(await apiFetch('/api/browser/collect', jsonOptions('POST', {})));
  } catch(error) { status.className = 'error'; setText(status, error.message); }
  finally { pending = false; await refresh(true); }
});
let browserRefreshPending = false;
async function refreshBrowser() {
  if (!form || browserRefreshPending || currentUser?.role !== 'admin') return;
  browserRefreshPending = true;
  try {
    const data = await responseJson(await apiFetch('/api/browser/status'));
    setText(document.querySelector('#browser-connection'), data.available ? (data.manual_access ? 'Включён ручной вход. После входа отключите noVNC по README.' : data.message) : data.connection_error);
  } catch(error) { setText(document.querySelector('#browser-connection'), error.message); }
  finally { browserRefreshPending = false; }
}
document.querySelector('#logout').addEventListener('click', async () => {
  try { await responseJson(await apiFetch('/api/auth/logout', {method:'POST'})); window.location.replace('/login'); }
  catch(error) { setText(message, error.message); }
});

const fileInput = document.querySelector('#csv-file');
const importSelect = document.querySelector('#import-select');
const importConfirm = document.querySelector('#import-confirm');
const importCancel = document.querySelector('#import-cancel');
const importStatus = document.querySelector('#import-status');
const previewPanel = document.querySelector('#import-preview');
let importToken = null;
function importMessage(text, error = false) {
  importStatus.textContent = text;
  importStatus.className = error ? 'error' : '';
}
importSelect?.addEventListener('click', () => fileInput.click());
fileInput?.addEventListener('change', async () => {
  const file = fileInput.files[0];
  if (!file) return;
  const oldToken = importToken;
  importToken = null; previewPanel.hidden = true;
  if (oldToken) apiFetch('/api/import/cancel', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:oldToken})}).catch(() => {});
  if (!file.name.toLowerCase().endsWith('.csv') || file.size > 5 * 1024 * 1024 || !file.size) {
    importMessage('Выберите непустой файл CSV размером до 5 МБ.', true); fileInput.value = ''; return;
  }
  importSelect.disabled = true;
  importMessage('Проверяем структуру CSV…');
  try {
    const data = await responseJson(await apiFetch('/api/import/preview', {method:'POST',headers:{'Content-Type':'text/csv'},body:file}));
    importToken = data.token;
    document.querySelector('#import-counts').textContent = `${file.name}: найдено строк — ${data.found}, уникальных объявлений — ${data.unique}, повторов в файле — ${data.duplicates}. Новых объектов — ${data.added}, перепубликаций — ${data.relisted || 0}, для обновления — ${data.updated}.`;
    const list = document.querySelector('#import-sample'); list.replaceChildren();
    for (const item of data.sample) {
      const li = document.createElement('li'); li.textContent = `${item.id} · ${item.address || item.title || 'Квартира'} · ${item.price == null ? 'Цена не указана' : item.price.toLocaleString('ru-RU') + ' ₽'}`; list.append(li);
    }
    previewPanel.hidden = false;
    importMessage('Первые 5 записей показаны ниже. Данные ещё не сохранены. Подтвердите импорт в течение 30 минут.');
  } catch (error) { importMessage(error.message, true); }
  finally { importSelect.disabled = false; fileInput.value = ''; }
});
importCancel?.addEventListener('click', async () => {
  const token = importToken; importToken = null; previewPanel.hidden = true;
  importMessage('Импорт отменён. Объявления не изменены.');
  if (token) { try { await responseJson(await apiFetch('/api/import/cancel', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})})); } catch(error) { importMessage('Объявления не изменены. Не удалось удалить предпросмотр: ' + error.message, true); } }
});
importConfirm?.addEventListener('click', async () => {
  if (!importToken) return;
  importConfirm.disabled = true; importSelect.disabled = true; importCancel.disabled = true;
  try {
    const data = await responseJson(await apiFetch('/api/import/confirm', {method:'POST', headers:{'Content-Type':'application/json'},body:JSON.stringify({token:importToken})}));
    importToken = null; previewPanel.hidden = true;
    importMessage(`Импорт завершён: новых объектов ${data.added}, перепубликаций ${data.relisted || 0}, обновлено ${data.updated}. Старые объявления сохранены.`);
    await refresh(true);
  } catch (error) { importMessage(error.message, true); }
  finally { importConfirm.disabled = false; importSelect.disabled = false; importCancel.disabled = false; }
});


const usersMessage = document.querySelector('#users-message');
async function loadUsers() {
  if (currentUser?.role !== 'admin') return;
  try {
    const data = await responseJson(await apiFetch('/api/users'));
    const list = document.querySelector('#users-list'); list.replaceChildren();
    const activeAdmins = data.users.filter(user => user.enabled && user.role === 'admin').length;
    for (const user of data.users) {
      const wrapper = document.createElement('section'); wrapper.className = 'user-editor';
      const edit = document.createElement('form'); edit.className = 'user-form';
      function input(labelText, name, value, type = 'text') {
        const box = document.createElement('div'), label = document.createElement('label'), field = document.createElement('input');
        field.id = 'user-' + user.id + '-' + name; field.name = name; field.type = type;
        field.value = value; field.maxLength = name === 'username' ? 128 : name === 'display_name' ? 80 : 1024;
        field.required = name !== 'password';
        label.htmlFor = field.id; label.textContent = labelText;
        box.append(label, field); edit.append(box); return field;
      }
      const login = input('Логин', 'username', user.username);
      const display = input('Имя', 'display_name', user.display_name);
      const roleBox = document.createElement('div'), roleLabel = document.createElement('label'), role = document.createElement('select');
      role.id = 'user-' + user.id + '-role'; roleLabel.htmlFor = role.id; roleLabel.textContent = 'Роль';
      for (const [value,label] of [['reviewer','Просмотр'],['realtor','Риелтор'],['admin','Администратор']]) {
        const option = document.createElement('option'); option.value = value; option.textContent = label; role.append(option);
      }
      role.value = user.role; roleBox.append(roleLabel, role); edit.append(roleBox);
      const password = input('Новый пароль (необязательно)', 'password', '', 'password');
      password.autocomplete = 'new-password';
      const passwordRule = () => {password.minLength = role.value === 'admin' ? 12 : 8; password.placeholder = 'Минимум ' + password.minLength + ' символов';};
      role.addEventListener('change',passwordRule); passwordRule();
      const save = document.createElement('button'); save.type = 'submit'; save.textContent = 'Сохранить'; edit.append(save);
      const enabledLabel = document.createElement('label'); enabledLabel.className = 'enabled-label';
      const enabled = document.createElement('input'); enabled.type = 'checkbox'; enabled.checked = !!user.enabled;
      enabledLabel.append(enabled, document.createTextNode('Доступ включён'));
      const protectedAdmin = activeAdmins === 1 && user.enabled && user.role === 'admin';
      if (protectedAdmin) { enabled.disabled = true; role.disabled = true; enabledLabel.title = 'Последний активный администратор'; }
      edit.addEventListener('submit', async event => {
        event.preventDefault(); save.disabled = true;
        const changes = {username:login.value, display_name:display.value, role:role.value, enabled:enabled.checked};
        if (password.value) changes.password = password.value;
        try {
          const saved = await responseJson(await apiFetch('/api/users/' + user.id, jsonOptions('PATCH', changes)));
          setText(usersMessage, 'Пользователь сохранён.'); usersMessage.className = '';
          if (saved.id === currentUser.id && changes.password) { window.location.replace('/login'); return; }
          await loadUsers();
        } catch(error) { setText(usersMessage, error.message); usersMessage.className = 'error'; }
        finally { password.value = ''; save.disabled = false; }
      });
      wrapper.append(edit, enabledLabel); list.append(wrapper);
    }
  } catch(error) { setText(usersMessage, error.message); usersMessage.className = 'error'; }
}
document.querySelector('#users-panel').addEventListener('toggle', event => { if (event.target.open) loadUsers(); });
const createRole = document.querySelector('#new-role'), createPassword = document.querySelector('#new-password');
function createPasswordRule() {
  createPassword.minLength = createRole.value === 'admin' ? 12 : 8;
  setText(document.querySelector('label[for="new-password"]'), 'Пароль · минимум ' + createPassword.minLength + ' символов');
}
createRole.addEventListener('change',createPasswordRule); createPasswordRule();
document.querySelector('#user-create').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target, save = form.querySelector('button'), data = new FormData(form);
  save.disabled = true;
  try {
    await responseJson(await apiFetch('/api/users', jsonOptions('POST', Object.fromEntries(data))));
    form.reset(); createPasswordRule(); setText(usersMessage, 'Пользователь создан.'); usersMessage.className = ''; await loadUsers();
  } catch(error) { setText(usersMessage, error.message); usersMessage.className = 'error'; }
  finally { form.elements.password.value = ''; save.disabled = false; }
});
let booted=false,browserPolledAt=0;
async function poll(){
  clearTimeout(pollTimer);
  if(document.hidden)return;
  try{await refresh();if(!document.hidden&&Date.now()-browserPolledAt>=15000){browserPolledAt=Date.now();await refreshBrowser();}}finally{if(!document.hidden)pollTimer=setTimeout(poll,5000);}
}
document.addEventListener('visibilitychange',()=>{
  if(document.hidden){clearTimeout(pollTimer);refreshController?.abort();}
  else if(booted)poll();
});
document.querySelector('#load-retry').addEventListener('click',async()=>{
  if(!booted)return boot();
  setText(document.querySelector('#load-message'),'Загрузка…');document.querySelector('#load-retry').hidden=true;
  await refresh(true);
});
async function boot() {
  try {
    const me = await responseJson(await apiFetch('/api/auth/me'));
    csrfToken=me.csrf_token;identify(me.user);booted=true;
    if(!document.hidden)await refresh(true);
    clearTimeout(pollTimer);if(!document.hidden)pollTimer=setTimeout(poll,5000);
  }catch(error){setLoadError(error);}
}
boot();
