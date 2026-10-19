'use strict';
const form = document.querySelector('#filters');
const button = document.querySelector('#collect');
const status = document.querySelector('#status');
const message = document.querySelector('#page-message');
const records = new Map(), rows = new Map(), pendingReviews = new Set(), pendingRealtors = new Set(), pendingDuplicates = new Set();
const labels = {new:'НОВОЕ', liked:'Понравилось', viewed:'Просмотрено', rejected:'Не подходит'};
const realtorLabels = {pending:'Не оценено',approved:'Одобрено',rejected:'Отбраковано'};
let selectedSort = null;
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
function cell(row) { const td = document.createElement('td'); row.append(td); return td; }
function safeCianUrl(value) {
  return /^https?:\/\/([a-z0-9-]+\.)*cian\.ru(?::(?:80|443))?\/sale\/flat\/\d+\/?(?:[?#].*)?$/i.test(value || '');
}
function makeRow(id) {
  const row = document.createElement('tr');
  row.dataset.listingId = id;
  const cells = Array.from({length:11}, () => cell(row));
  const link = document.createElement('a');
  link.target = '_blank'; link.rel = 'noopener noreferrer';
  cells[0].append(link);
  cells[1].className = 'object-cell';
  const title = document.createElement('strong'), address = document.createElement('small');
  const propertyHistory = document.createElement('small'); propertyHistory.className = 'property-history';
  const details = document.createElement('details'), summary = document.createElement('summary'), text = document.createElement('p');
  summary.textContent = 'Текст карточки / описание'; details.append(summary, text);
  const duplicateBox = document.createElement('div'); duplicateBox.className = 'duplicate-box';
  const duplicateText = document.createElement('div');
  const duplicateActions = document.createElement('div'); duplicateActions.className = 'duplicate-actions';
  const duplicateSame = document.createElement('button'); duplicateSame.type='button'; duplicateSame.textContent='Да, тот же объект';
  const duplicateDifferent = document.createElement('button'); duplicateDifferent.type='button'; duplicateDifferent.className='secondary'; duplicateDifferent.textContent='Нет, другая квартира';
  duplicateActions.append(duplicateSame,duplicateDifferent); duplicateBox.append(duplicateText,duplicateActions);
  cells[1].append(title, address, propertyHistory, details, duplicateBox);
  const date = document.createElement('time'), clock = document.createElement('small'); cells[8].append(date, clock);
  const badge = document.createElement('span'); badge.className = 'review-label';
  const select = document.createElement('select'); select.className = 'review-select';
  select.setAttribute('aria-label', 'Статус объявления ' + id);
  for (const [value,label] of Object.entries(labels)) { const option = document.createElement('option'); option.value = value; option.textContent = label; select.append(option); }
  const meta = document.createElement('small'); meta.className = 'review-meta';
  const saving = document.createElement('div'); saving.className = 'review-saving'; saving.setAttribute('aria-live', 'polite');
  select.addEventListener('change', () => changeReview(id, select.value));
  select.addEventListener('blur', () => renderListings());
  cells[9].append(badge, select, meta, saving);
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
  cells[10].append(realtorBadge,commentPreview,commentDetails,realtorMeta,editor);
  const entry = {row,cells,link,title,address,propertyHistory,details,text,duplicateBox,duplicateText,duplicateActions,
    duplicateSame,duplicateDifferent,date,clock,badge,select,meta,saving,
    realtorBadge,commentPreview,commentDetails,commentText,realtorMeta,editor,realtorSelect,comment,saveRealtor,realtorSaving,dirty:false};
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
  const entry = rows.get(item.id) || makeRow(item.id);
  const {row,cells,link,title,address,details,text,date,clock,badge,select,meta,saving} = entry;
  setText(link, item.id);
  if (safeCianUrl(item.url)) link.href = item.url; else link.removeAttribute('href');
  setText(title, item.complex_name || item.title || 'Квартира');
  setText(address, item.address);
  const description = item.card_text || item.description || '';
  details.hidden = !description; setText(text, description);
  const ids = item.listing_ids || [item.id];
  const relists = Number(item.relist_count || 0);
  setText(entry.propertyHistory, relists ? 'Перепубликовано ' + relists + ' ' + (relists === 1 ? 'раз' : 'раза') + ' · CIAN: ' + ids.join(' → ') : '');
  entry.propertyHistory.hidden = !relists;
  const candidate = item.duplicate_candidate;
  entry.duplicateBox.hidden = !candidate;
  if (candidate) {
    setText(entry.duplicateText, 'Возможно, это перепубликация CIAN ' + candidate.candidate_listing_id + ' · совпадение ' + candidate.confidence + '%');
    const canDecide = ['admin','reviewer'].includes(currentUser?.role);
    entry.duplicateActions.hidden = !canDecide;
    entry.duplicateSame.disabled = entry.duplicateDifferent.disabled = pendingDuplicates.has(item.id);
  }
  setText(cells[2], item.price == null ? '—' : new Intl.NumberFormat('ru-RU', {style:'currency', currency:'RUB', maximumFractionDigits:0}).format(item.price));
  const payment = XataReview.mortgage(item.price);
  setText(cells[3], payment == null ? '—' : (payment > 0 ? '≈ ' : '') + (Math.round(payment / 100) * 100).toLocaleString('ru-RU') + ' ₽/мес.');
  setText(cells[4], item.area == null ? '—' : item.area + ' м²');
  setText(cells[5], item.floor || '—'); setText(cells[6], item.metro || '—'); setText(cells[7], item.metro_time || '—');
  const first = item.first_seen ? new Date(item.first_seen) : null;
  setText(date, first ? first.toLocaleDateString('ru-RU') : '—');
  date.dateTime = item.first_seen || '';
  setText(clock, first ? first.toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'}) : '');
  const review = item.review_status || 'new';
  row.className = 'review-' + review;
  row.dataset.reviewStatus = review;
  setText(badge, labels[review]);
  if (forceReview || (!pendingReviews.has(item.id) && document.activeElement !== select)) select.value = review;
  select.hidden = currentUser?.role === 'realtor';
  select.disabled = pendingReviews.has(item.id) || currentUser?.role === 'realtor';
  setText(saving, pendingReviews.has(item.id) ? 'Сохраняем…' : '');
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
  entry.realtorSelect.disabled = entry.comment.disabled = entry.saveRealtor.disabled = !canRealtor || pendingRealtors.has(item.id);
  if (!entry.dirty && !pendingRealtors.has(item.id)) {
    entry.realtorSelect.value = realtor;
    entry.comment.value = item.realtor_comment || '';
  }
  setText(entry.realtorSaving, pendingRealtors.has(item.id) ? 'Сохраняем…' : '');
  row.hidden = selectedFilter !== 'all' && selectedFilter !== review;
  return row;
}
function renderListings() {
  const body = document.querySelector('#listings');
  const ordered = XataReview.ordered(records.values(), selectedSort);
  const counts = XataReview.counts(ordered);
  for (const item of ordered) { const row = renderRow(item); if (!row.parentNode) body.append(row); }
  // Keep focused status controls/details in place during refresh.
  if (!pendingReviews.size && !pendingRealtors.size && !pendingDuplicates.size && !document.activeElement?.matches('.review-select,.realtor-select,.realtor-comment')) {
    let cursor = body.firstElementChild;
    for (const item of ordered) {
      const row = rows.get(item.id).row;
      if (row !== cursor) body.insertBefore(row, cursor);
      cursor = row.nextElementSibling;
    }
  }
  for (const node of document.querySelectorAll('[data-review-filter]')) {
    node.setAttribute('aria-pressed', String(node.dataset.reviewFilter === selectedFilter));
    setText(node.querySelector('span'), counts[node.dataset.reviewFilter]);
  }
  setText(document.querySelector('#count'), ordered.length);
  const empty = document.querySelector('#empty');
  empty.hidden = (selectedFilter === 'all' ? counts.all : counts[selectedFilter]) > 0;
  setText(empty, counts.all ? 'Нет объявлений с выбранным статусом.' : 'Пока нет сохранённых объявлений.');
}
function acceptListing(item) {
  records.set(item.id, XataReview.merge(records.get(item.id), item));
}
async function changeReview(id, nextStatus) {
  if (pendingReviews.has(id)) return;
  pendingReviews.add(id); renderRow(records.get(id));
  try {
    const saved = await responseJson(await apiFetch('/api/listings/' + encodeURIComponent(id) + '/review', jsonOptions('POST', {status:nextStatus})));
    acceptListing(saved);
    setText(message, 'Статус сохранён.'); message.className = '';
  } catch(error) {
    setText(message, error.message); message.className = 'error';
  } finally {
    pendingReviews.delete(id);
    renderRow(records.get(id), true);
    renderListings();
  }
}
for (const node of document.querySelectorAll('[data-review-filter]')) node.addEventListener('click', () => { selectedFilter = node.dataset.reviewFilter; renderListings(); });
async function changeRealtor(id) {
  if (pendingRealtors.has(id)) return;
  const entry = rows.get(id), data = {status:entry.realtorSelect.value,comment:entry.comment.value};
  pendingRealtors.add(id); renderRow(records.get(id));
  try {
    const saved = await responseJson(await apiFetch('/api/listings/' + encodeURIComponent(id) + '/realtor-review', jsonOptions('POST',data)));
    acceptListing(saved); entry.dirty = false;
    setText(message,'Оценка риелтора сохранена.'); message.className='';
  } catch(error) { setText(message,error.message); message.className='error'; }
  finally {pendingRealtors.delete(id); renderListings();}
}
async function decideDuplicate(id, decision) {
  if (pendingDuplicates.has(id)) return;
  pendingDuplicates.add(id); renderRow(records.get(id));
  try {
    await responseJson(await apiFetch('/api/listings/' + encodeURIComponent(id) + '/duplicate-decision', jsonOptions('POST',{decision})));
    setText(message, decision === 'same' ? 'Перепубликации объединены в один объект.' : 'Объявления отмечены как разные квартиры.');
    message.className='';
    await refresh();
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
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const data = await responseJson(await apiFetch('/api/state'));
    identify(data.user);
    setText(document.querySelector('#last-sync'), 'Последняя успешная синхронизация с ЦИАН: ' + (data.last_success ? new Date(data.last_success).toLocaleString('ru-RU') : 'ещё не было'));
    const criteria = data.criteria || {max_price:14500000,metro_minutes:20};
    setText(document.querySelector('#criteria-price'),'Бюджет до ' + (criteria.max_price / 1000000).toLocaleString('ru-RU') + ' млн ₽');
    setText(document.querySelector('#criteria-metro'),'До метро — не более ' + criteria.metro_minutes + ' минут пешком');
    const incoming = new Set(data.listings.map(item => String(item.id)));
    for (const id of Array.from(records.keys())) {
      if (!incoming.has(String(id))) {
        records.delete(id);
        const entry = rows.get(id);
        if (entry) entry.row.remove();
        rows.delete(id);
      }
    }
    for (const item of data.listings) acceptListing(item);
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
  finally { pending = false; await refresh(); }
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
    document.querySelector('#import-counts').textContent = `${file.name}: найдено строк — ${data.found}, уникальных объявлений — ${data.unique}, повторов в файле — ${data.duplicates}. Новых — ${data.added}, для обновления — ${data.updated}.`;
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
    importMessage(`Импорт завершён: добавлено ${data.added}, обновлено ${data.updated}. Старые объявления сохранены.`);
    await refresh();
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
    await refresh(); await refreshBrowser();
    setInterval(refresh, 5000); setInterval(refreshBrowser, 15000);
  } catch(error) { setText(message, error.message); message.className = 'error'; }
}
boot();
