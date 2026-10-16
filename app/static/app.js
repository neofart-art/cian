'use strict';
const form = document.querySelector('#filters');
const button = document.querySelector('#collect');
const status = document.querySelector('#status');
const message = document.querySelector('#page-message');
const records = new Map(), rows = new Map(), pendingReviews = new Set();
const labels = {new:'НОВОЕ', viewed:'Просмотрено', rejected:'Не подходит'};
let currentUser = null, csrfToken = '', pending = false, refreshing = false, selectedFilter = 'all';

function setText(node, value) {
  const text = String(value ?? '');
  if (node.textContent !== text) node.textContent = text;
}
function identify(user) {
  currentUser = user;
  setText(document.querySelector('#identity'), user.display_name + ' · ' + (user.role === 'admin' ? 'Администратор' : 'Просмотр'));
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
  const cells = Array.from({length:9}, () => cell(row));
  const link = document.createElement('a');
  link.target = '_blank'; link.rel = 'noopener noreferrer';
  cells[0].append(link);
  cells[1].className = 'object-cell';
  const title = document.createElement('strong'), address = document.createElement('small');
  const details = document.createElement('details'), summary = document.createElement('summary'), text = document.createElement('p');
  summary.textContent = 'Текст карточки / описание'; details.append(summary, text);
  cells[1].append(title, address, details);
  const date = document.createElement('time'), clock = document.createElement('small'); cells[7].append(date, clock);
  const badge = document.createElement('span'); badge.className = 'review-label';
  const select = document.createElement('select'); select.className = 'review-select';
  select.setAttribute('aria-label', 'Статус объявления ' + id);
  for (const [value,label] of Object.entries(labels)) { const option = document.createElement('option'); option.value = value; option.textContent = label; select.append(option); }
  const meta = document.createElement('small'); meta.className = 'review-meta';
  const saving = document.createElement('div'); saving.className = 'review-saving'; saving.setAttribute('aria-live', 'polite');
  select.addEventListener('change', () => changeReview(id, select.value));
  select.addEventListener('blur', () => renderListings());
  cells[8].append(badge, select, meta, saving);
  const entry = {row,cells,link,title,address,details,text,date,clock,badge,select,meta,saving};
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
  setText(cells[2], item.price == null ? '—' : new Intl.NumberFormat('ru-RU', {style:'currency', currency:'RUB', maximumFractionDigits:0}).format(item.price));
  setText(cells[3], item.area == null ? '—' : item.area + ' м²');
  setText(cells[4], item.floor || '—'); setText(cells[5], item.metro || '—'); setText(cells[6], item.metro_time || '—');
  const first = item.first_seen ? new Date(item.first_seen) : null;
  setText(date, first ? first.toLocaleDateString('ru-RU') : '—');
  date.dateTime = item.first_seen || '';
  setText(clock, first ? first.toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'}) : '');
  const review = item.review_status || 'new';
  row.className = 'review-' + review;
  row.dataset.reviewStatus = review;
  setText(badge, labels[review]);
  if (forceReview || (!pendingReviews.has(item.id) && document.activeElement !== select)) select.value = review;
  select.disabled = pendingReviews.has(item.id);
  setText(saving, pendingReviews.has(item.id) ? 'Сохраняем…' : '');
  let author = item.review_updated_by_name || item.review_updated_by_username;
  if (!author && item.review_updated_by) author = 'Пользователь #' + item.review_updated_by;
  const changed = item.review_updated_at ? new Date(item.review_updated_at) : null;
  setText(meta, changed ? (author || '') + ' · ' + changed.toLocaleString('ru-RU', {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'}) : '');
  meta.title = changed ? changed.toLocaleString('ru-RU') : '';
  row.hidden = selectedFilter !== 'all' && selectedFilter !== review;
  return row;
}
function renderListings() {
  const body = document.querySelector('#listings');
  const ordered = XataReview.ordered(records.values());
  const counts = {all:ordered.length,new:0,viewed:0,rejected:0};
  for (const item of ordered) { counts[item.review_status || 'new'] += 1; const row = renderRow(item); if (!row.parentNode) body.append(row); }
  // Keep focused status controls/details in place during refresh.
  if (!pendingReviews.size && !document.activeElement?.matches('.review-select')) {
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
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const data = await responseJson(await apiFetch('/api/state'));
    identify(data.user);
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
    await responseJson(await apiFetch('/api/browser/collect', jsonOptions('POST', {max_price:Number(document.querySelector('#price').value), metro_minutes:Number(document.querySelector('#metro').value)})));
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
      for (const [value,label] of [['reviewer','Просмотр'],['admin','Администратор']]) {
        const option = document.createElement('option'); option.value = value; option.textContent = label; role.append(option);
      }
      role.value = user.role; roleBox.append(roleLabel, role); edit.append(roleBox);
      const password = input('Новый пароль (необязательно)', 'password', '', 'password');
      password.minLength = 12; password.autocomplete = 'new-password';
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
document.querySelector('#user-create').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target, save = form.querySelector('button'), data = new FormData(form);
  save.disabled = true;
  try {
    await responseJson(await apiFetch('/api/users', jsonOptions('POST', Object.fromEntries(data))));
    form.reset(); setText(usersMessage, 'Пользователь создан.'); usersMessage.className = ''; await loadUsers();
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
