'use strict';
const form = document.querySelector('#filters');
const button = document.querySelector('#collect');
const status = document.querySelector('#status');
const message = document.querySelector('#page-message');
const records = new Map(), rows = new Map(), pendingReviews = new Set(), pendingRealtors = new Set(), pendingDuplicates = new Set();
const labels = {new:'НОВОЕ', liked:'Понравилось', viewed:'Просмотрено', rejected:'Не подходит'};
const realtorLabels = {pending:'Не оценено',approved:'Одобрено',rejected:'Отбраковано'};
let selectedSort = null, identityRevision = 0, refreshPromise = null;
let currentUser = null, csrfToken = '', pending = false, refreshing = false, selectedFilter = 'all';

function setText(node, value) {
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
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Проверьте введённые значения.');
  return data;
}
function jsonOptions(method, data) {
  return {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)};
}
const visitUI = XataVisits.create({getItem:key=>records.get(key),getUser:()=>currentUser,
  saveItem:item=>{acceptListing(item);setText(message,'Просмотр сохранён.');message.className='';},request:async(url,options)=>responseJson(await apiFetch(url,options)),
  jsonOptions,render:renderListings,refresh:()=>refresh(true)});
const comparisonUI = XataCompare.create({getItem:key=>records.get(key),render:renderListings});
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
  const duplicateCompare = document.createElement('a'); duplicateCompare.target='_blank'; duplicateCompare.rel='noopener noreferrer';
  duplicateCompare.textContent='Сравнить на ЦИАН'; duplicateBox.insertBefore(duplicateCompare,duplicateActions);
  cells[2].append(title,address,propertyHistory,historyDetails,duplicateBox);
  const coverButton = document.createElement('button'); coverButton.type='button'; coverButton.className='cover-button';
  const coverImage = document.createElement('img'); coverImage.className='listing-cover'; coverImage.loading='lazy';
  coverImage.decoding='async'; coverImage.width=110; coverImage.height=80;
  const coverPlaceholder = document.createElement('span'); coverPlaceholder.className='cover-placeholder'; coverPlaceholder.textContent='Нет фото';
  coverButton.append(coverImage); cells[0].append(coverButton,coverPlaceholder);
  coverButton.addEventListener('click',()=>openCover(id));
  const compareLabel=document.createElement('label');compareLabel.className='compare-choice';
  const compareInput=document.createElement('input');compareInput.type='checkbox';compareInput.className='compare-select';
  compareLabel.append(compareInput,document.createTextNode('В сравнение'));cells[0].append(compareLabel);
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
  const realtorBadge = document.createElement('span'); realtorBadge.className = 'realtor-label';
  const commentPreview = document.createElement('small'); commentPreview.className = 'comment-preview';
  const commentDetails = document.createElement('details'), commentSummary = document.createElement('summary'), commentText = document.createElement('p');
  commentDetails.className = 'realtor-details'; commentSummary.textContent = 'Комментарий'; commentText.className = 'comment-text';
  commentDetails.append(commentSummary, commentText);
  const realtorMeta = document.createElement('small'); realtorMeta.className = 'realtor-meta';
  const editor = document.createElement('details'); editor.className = 'realtor-editor';
  const editorSummary = document.createElement('summary'); editorSummary.textContent = 'Оценить / изменить';
  const realtorForm = document.createElement('form');
  const realtorSelect = document.createElement('select'); realtorSelect.className = 'realtor-select';
  realtorSelect.setAttribute('aria-label', 'Оценка риелтора ' + id);
  for (const [value,label] of Object.entries(realtorLabels)) { const option = document.createElement('option'); option.value=value; option.textContent=label; realtorSelect.append(option); }
  const comment = document.createElement('textarea'); comment.maxLength = 2000; comment.rows = 4; comment.className = 'realtor-comment';
  comment.setAttribute('aria-label', 'Комментарий риелтора ' + id);
  const saveRealtor = document.createElement('button'); saveRealtor.type = 'submit'; saveRealtor.className = 'compact'; saveRealtor.textContent = 'Сохранить';
  const realtorSaving = document.createElement('small'); realtorSaving.setAttribute('aria-live','polite');
  realtorForm.append(realtorSelect,comment,saveRealtor,realtorSaving); editor.append(editorSummary,realtorForm);
  cells[11].append(realtorBadge,commentPreview,commentDetails,realtorMeta,editor);
  const entry = {row,cells,compareInput,compareLabel,returnHint,link,title,address,propertyHistory,coverButton,coverImage,coverPlaceholder,failedCover:null,failedCoverAt:0,historyDetails,historyText,duplicateCompare,duplicateBox,duplicateText,duplicateActions,
    duplicateSame,duplicateDifferent,date,clock,badge,select,meta,saving,
    realtorBadge,commentPreview,commentDetails,commentText,realtorMeta,editor,realtorSelect,comment,saveRealtor,realtorSaving,dirty:false};
  coverImage.addEventListener('error',()=>{
    entry.failedCover=coverImage.getAttribute('src'); entry.failedCoverAt=Date.now();
    coverButton.hidden=true; coverPlaceholder.hidden=false;
  });
  coverImage.addEventListener('load',()=>{
    entry.failedCover=null; entry.failedCoverAt=0;
    coverButton.hidden=false; coverPlaceholder.hidden=true;
  });
  comment.addEventListener('input', () => {entry.dirty=true;});
  realtorSelect.addEventListener('change', () => {entry.dirty=true;});
  realtorForm.addEventListener('submit', event => {event.preventDefault(); changeRealtor(id);});
  duplicateSame.addEventListener('click', () => decideDuplicate(id,'same'));
  duplicateDifferent.addEventListener('click', () => decideDuplicate(id,'different'));
  for (const field of [realtorSelect,comment]) field.addEventListener('blur', () => renderListings());
  rows.set(id, entry);
  return entry;
}
function renderRow(item, forceReview = false) {
  const id = XataReview.key(item);
  const entry = rows.get(id) || makeRow(id);
  entry.row.dataset.listingId = item.id;
  entry.row.dataset.propertyId = item.property_id || '';
  entry.select.setAttribute('aria-label','Статус объявления ' + item.id);
  entry.realtorSelect.setAttribute('aria-label','Оценка риелтора ' + item.id);
  entry.comment.setAttribute('aria-label','Комментарий риелтора ' + item.id);
  const {row,cells,link,title,address,date,clock,badge,select,meta,saving} = entry;
  setText(link, item.id);
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
  setText(entry.historyText,publications.join('\n') + (prices.length ? '\nИстория цен:\n' + prices.join('\n') : ''));
  entry.historyDetails.hidden = !publications.length && !prices.length;
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
  row.className = 'review-' + effectiveStatus;
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
  const realtor = item.realtor_status || 'pending';
  setText(entry.realtorBadge, realtorLabels[realtor]);
  entry.realtorBadge.className = 'realtor-label realtor-' + realtor;
  setText(entry.commentPreview, item.realtor_comment ? item.realtor_comment.slice(0,60) + (item.realtor_comment.length > 60 ? '…' : '') : '');
  entry.commentDetails.hidden = !item.realtor_comment;
  setText(entry.commentText, item.realtor_comment || '');
  const realtorAuthor = item.realtor_updated_by_name || item.realtor_updated_by_username || (item.realtor_updated_by ? 'Пользователь #' + item.realtor_updated_by : '');
  const realtorDate = item.realtor_updated_at ? new Date(item.realtor_updated_at) : null;
  setText(entry.realtorMeta, realtorDate ? realtorAuthor + ' · ' + realtorDate.toLocaleString('ru-RU', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '');
  entry.realtorMeta.title = realtorDate ? realtorDate.toLocaleString('ru-RU') : '';
  const canRealtor = ['admin','realtor'].includes(currentUser?.role);
  entry.editor.hidden = !canRealtor;
  entry.realtorSelect.disabled = entry.comment.disabled = entry.saveRealtor.disabled = !canRealtor || pendingRealtors.has(id);
  if (!entry.dirty && !pendingRealtors.has(id)) {
    entry.realtorSelect.value = realtor;
    entry.comment.value = item.realtor_comment || '';
  }
  setText(entry.realtorSaving, pendingRealtors.has(id) ? 'Сохраняем…' : '');
  visitUI.renderCells(cells[12],cells[13],item);
  entry.compareLabel.hidden=effectiveStatus==='rejected';
  entry.compareInput.checked=comparisonUI.has(id);
  entry.compareInput.setAttribute('aria-label','В сравнение: квартира '+item.id);
  entry.returnHint.hidden=item.realtor_status!=='rejected';
  setText(entry.returnHint,'Возврат блокирует отказ риелтора. Его оценку меняет риелтор или администратор.');
  row.hidden = effectiveStatus !== 'rejected' && selectedFilter !== 'all' && (selectedFilter === 'planned' || selectedFilter !== effectiveStatus);
  return row;
}
function renderListings() {
  const body=document.querySelector('#listings'),rejectedBody=document.querySelector('#rejected-listings');
  const ordered=XataReview.ordered(records.values(),selectedSort);
  const active=ordered.filter(item=>XataReview.effectiveStatus(item)!=='rejected');
  const rejected=ordered.filter(item=>XataReview.effectiveStatus(item)==='rejected');
  const counts=XataReview.counts(active);
  counts.planned=XataReview.agenda(ordered).length;
  const planned=selectedFilter==='planned';
  document.querySelector('.table-wrap').hidden=planned;
  document.querySelector('#visit-agenda').hidden=!planned;
  if(planned)visitUI.agenda(ordered);
  comparisonUI.sync();
  // Move existing nodes between the two bodies; never duplicate an apartment
  // or rebuild a realtor draft when its effective status changes.
  for(const item of ordered) {
    const row=renderRow(item),target=XataReview.effectiveStatus(item)==='rejected'?rejectedBody:body;
    if(row.parentNode!==target)target.append(row);
  }
  if(!pendingReviews.size&&!pendingRealtors.size&&!pendingDuplicates.size&&!document.activeElement?.matches('.review-select,.realtor-select,.realtor-comment,.visit-date-input,.visit-date-picker,.visit-date-clear,.visit-date-ok')) {
    for(const [target,items] of [[body,active],[rejectedBody,rejected]]) {
      let cursor=target.firstElementChild;
      for(const item of items) {
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
  setText(document.querySelector('#count'),active.length);
  setText(document.querySelector('#rejected-count'),rejected.length);
  const empty=document.querySelector('#empty');
  empty.hidden=(selectedFilter==='all'?counts.all:counts[selectedFilter])>0;
  setText(empty,active.length?'Нет объявлений с выбранным статусом.':'В основном списке пока нет объявлений.');
}

function acceptListing(item) {
  if (Number(item.identity_revision || 0) < identityRevision) return;
  identityRevision = Math.max(identityRevision,Number(item.identity_revision || 0));
  const key = XataReview.key(item);
  records.set(key,XataReview.merge(records.get(key),item));
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
async function changeRealtor(id) {
  if (pendingRealtors.has(id) || !records.has(id)) return;
  const entry = rows.get(id), data = {status:entry.realtorSelect.value,comment:entry.comment.value};
  pendingRealtors.add(id); renderRow(records.get(id));
  try {
    const saved = await responseJson(await apiFetch('/api/listings/' + encodeURIComponent(records.get(id).id) + '/realtor-review', jsonOptions('POST',data)));
    acceptListing(saved); entry.dirty = false;
    setText(message,'Оценка риелтора сохранена.'); message.className='';
  } catch(error) { setText(message,error.message); message.className='error'; }
  finally {pendingRealtors.delete(id); renderListings();}
}
async function decideDuplicate(id, decision) {
  if (pendingDuplicates.has(id) || !records.has(id)) return;
  if (rows.get(id)?.dirty) {
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
for (const node of document.querySelectorAll('[data-sort]')) node.addEventListener('click', () => {
  selectedSort = XataReview.cycleSort(selectedSort,node.dataset.sort);
  for (const header of document.querySelectorAll('[data-sort]')) {
    const direction = selectedSort?.key === header.dataset.sort ? selectedSort.direction : null;
    header.closest('th').setAttribute('aria-sort', direction === 'asc' ? 'ascending' : direction === 'desc' ? 'descending' : 'none');
    setText(header.querySelector('[data-sort-indicator]'),direction === 'asc' ? ' ↑' : direction === 'desc' ? ' ↓' : '');
  }
  renderListings();
});
async function refresh(force=false) {
  if (refreshPromise) {
    await refreshPromise;
    if (force) return refresh();
    return;
  }
  refreshPromise = refreshState();
  try {await refreshPromise;} finally {refreshPromise=null;}
}
async function refreshState() {
  refreshing = true;
  try {
    const data = await responseJson(await apiFetch('/api/state'));
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
    renderListings();
    if (currentUser.role !== 'admin') return;
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
  } catch(error) { setText(message, error.message); message.className = 'error'; }
  finally { refreshing = false; }
}
form.addEventListener('submit', async event => {
  event.preventDefault(); pending = true; button.disabled = true;
  try {
    await responseJson(await apiFetch('/api/browser/collect', jsonOptions('POST', {})));
  } catch(error) { status.className = 'error'; setText(status, error.message); }
  finally { pending = false; await refresh(true); }
});
let browserRefreshPending = false;
async function refreshBrowser() {
  if (browserRefreshPending || currentUser?.role !== 'admin') return;
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
importSelect.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', async () => {
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
importCancel.addEventListener('click', async () => {
  const token = importToken; importToken = null; previewPanel.hidden = true;
  importMessage('Импорт отменён. Объявления не изменены.');
  if (token) { try { await responseJson(await apiFetch('/api/import/cancel', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})})); } catch(error) { importMessage('Объявления не изменены. Не удалось удалить предпросмотр: ' + error.message, true); } }
});
importConfirm.addEventListener('click', async () => {
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
async function boot() {
  try {
    const me = await responseJson(await apiFetch('/api/auth/me'));
    csrfToken = me.csrf_token; identify(me.user);
    await refresh(true); await refreshBrowser();
    setInterval(refresh, 5000); setInterval(refreshBrowser, 15000);
  } catch(error) { setText(message, error.message); message.className = 'error'; }
}
boot();
