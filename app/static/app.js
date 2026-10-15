const form = document.querySelector('#filters');
const button = document.querySelector('#collect');
const status = document.querySelector('#status');
let pending = false;
function cell(row, text) { const td = document.createElement('td'); td.textContent = text; row.append(td); return td; }
async function refresh() {
  try {
    const response = await fetch('/api/state');
    if (!response.ok) throw new Error(`Не удалось загрузить данные (HTTP ${response.status}).`);
    const data = await response.json();
    document.querySelector('#count').textContent = data.listings.length;
    document.querySelector('#empty').hidden = data.listings.length > 0;
    const body = document.querySelector('#listings'); body.replaceChildren();
    for (const item of data.listings) {
      const row = document.createElement('tr');
      const title = cell(row, item.complex_name || item.title); const address = document.createElement('small'); address.textContent = item.address; title.append(address);
      if (item.description || item.card_text) { const details = document.createElement('details'); const summary = document.createElement('summary'); summary.textContent = 'Текст карточки / описание'; const text = document.createElement('p'); text.textContent = item.card_text || item.description; details.append(summary, text); title.append(details); }
      cell(row, item.price == null ? '—' : new Intl.NumberFormat('ru-RU', {style:'currency',currency:'RUB',maximumFractionDigits:0}).format(item.price));
      cell(row, item.area == null ? '—' : `${item.area} м²`);
      cell(row, item.floor || '—');
      cell(row, item.metro || '—');
      cell(row, item.metro_time || '—');
      const linkCell = cell(row, ''); const link = document.createElement('a');
      if (/^https?:\/\/([a-z0-9-]+\.)*cian\.ru(?::(?:80|443))?\/sale\/flat\/\d+\/?(?:[?#].*)?$/i.test(item.url)) { link.href = item.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = 'Открыть ↗'; linkCell.append(link); }
      for (const value of [item.first_seen, item.last_seen, item.last_imported]) cell(row, value ? new Date(value).toLocaleString('ru-RU') : '—');
      cell(row, item.source || 'ЦИАН online');
      body.append(row);
    }
    const browser = data.browser || {};
    const labels = {authorized:'Авторизован', login_required:'Требуется вход', confirmation_required:'Требуется подтверждение/CAPTCHA', error:'Ошибка browser collector'};
    document.querySelector('#browser-state').textContent = labels[browser.status] || browser.message || 'Состояние неизвестно';
    document.querySelector('#browser-success').textContent = browser.last_success ? new Date(browser.last_success).toLocaleString('ru-RU') : 'Ещё не было';
    document.querySelector('#browser-new').textContent = browser.new_count ?? 0;
    document.querySelector('#browser-error').textContent = browser.last_error || '—';
    const run = data.run;
    status.className = run?.status === 'error' ? 'error' : run?.status === 'blocked' ? 'blocked' : '';
    status.textContent = run ? `${run.message}${run.status === 'done' ? ` Получено: ${run.count}.` : ''}\nСбор (${run.source === 'browser' ? 'браузер' : 'диагностика'}) · начало: ${new Date(run.started).toLocaleString('ru-RU')}` : 'Сбор ещё не запускался.';
    button.disabled = pending || run?.status === 'running';
  } catch (error) { status.className = 'error'; status.textContent = error.message; button.disabled = pending; }
}
form.addEventListener('submit', async event => {
  event.preventDefault(); pending = true; button.disabled = true;
  try {
    const response = await fetch('/api/browser/collect', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({max_price:Number(document.querySelector('#price').value),metro_minutes:Number(document.querySelector('#metro').value)})});
    if (!response.ok) { const error = await response.json(); throw new Error(typeof error.detail === 'string' ? error.detail : 'Проверьте значения фильтров.'); }
    pending = false; await refresh();
  } catch(error) { status.className = 'error'; status.textContent = error.message; }
  finally { pending = false; button.disabled = false; }
});
let browserRefreshPending = false;
async function refreshBrowser() {
  if (browserRefreshPending) return;
  browserRefreshPending = true;
  try {
    const response = await fetch('/api/browser/status');
    if (!response.ok) throw new Error('Не удалось проверить browser service');
    const data = await response.json();
    document.querySelector('#browser-connection').textContent = data.available
      ? (data.manual_access ? 'Открыт режим ручного входа. После входа отключите noVNC по README.' : data.message)
      : data.connection_error;
  } catch (error) {
    document.querySelector('#browser-connection').textContent = error.message;
  } finally { browserRefreshPending = false; }
}
refresh(); refreshBrowser(); setInterval(refresh, 5000); setInterval(refreshBrowser, 15000);

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
async function responseJson(response) {
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Ошибка HTTP ${response.status}`);
  return data;
}
importSelect.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', async () => {
  const file = fileInput.files[0];
  if (!file) return;
  const oldToken = importToken;
  importToken = null; previewPanel.hidden = true;
  if (oldToken) fetch('/api/import/cancel', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:oldToken})}).catch(() => {});
  if (!file.name.toLowerCase().endsWith('.csv') || file.size > 5 * 1024 * 1024 || !file.size) {
    importMessage('Выберите непустой файл CSV размером до 5 МБ.', true); fileInput.value = ''; return;
  }
  importSelect.disabled = true;
  importMessage('Проверяем структуру CSV…');
  try {
    const data = await responseJson(await fetch('/api/import/preview', {method:'POST',headers:{'Content-Type':'text/csv'},body:file}));
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
  if (token) { try { await responseJson(await fetch('/api/import/cancel', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})})); } catch(error) { importMessage('Объявления не изменены. Не удалось удалить предпросмотр: ' + error.message, true); } }
});
importConfirm.addEventListener('click', async () => {
  if (!importToken) return;
  importConfirm.disabled = true; importSelect.disabled = true; importCancel.disabled = true;
  try {
    const data = await responseJson(await fetch('/api/import/confirm', {method:'POST', headers:{'Content-Type':'application/json'},body:JSON.stringify({token:importToken})}));
    importToken = null; previewPanel.hidden = true;
    importMessage(`Импорт завершён: добавлено ${data.added}, обновлено ${data.updated}. Старые объявления сохранены.`);
    await refresh();
  } catch (error) { importMessage(error.message, true); }
  finally { importConfirm.disabled = false; importSelect.disabled = false; importCancel.disabled = false; }
});
