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
      const title = cell(row, item.title); const address = document.createElement('small'); address.textContent = item.address; title.append(address);
      cell(row, new Intl.NumberFormat('ru-RU', {style:'currency',currency:'RUB',maximumFractionDigits:0}).format(item.price));
      cell(row, new Date(item.last_seen).toLocaleString('ru-RU'));
      const linkCell = cell(row, ''); const link = document.createElement('a');
      if (/^https:\/\/(www\.)?cian\.ru\/sale\/flat\/\d+\/?$/.test(item.url)) { link.href = item.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = 'Открыть ↗'; linkCell.append(link); }
      body.append(row);
    }
    const run = data.run;
    status.className = run?.status === 'error' ? 'error' : '';
    status.textContent = run ? `${run.message}${run.status === 'done' ? ` Получено: ${run.count}.` : ''}\nНачало: ${new Date(run.started).toLocaleString('ru-RU')}` : 'Сбор ещё не запускался.';
    button.disabled = pending || run?.status === 'running';
  } catch (error) { status.className = 'error'; status.textContent = error.message; button.disabled = pending; }
}
form.addEventListener('submit', async event => {
  event.preventDefault(); pending = true; button.disabled = true;
  try {
    const response = await fetch('/api/collect', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({max_price:Number(document.querySelector('#price').value),metro_minutes:Number(document.querySelector('#metro').value)})});
    if (!response.ok) { const error = await response.json(); throw new Error(typeof error.detail === 'string' ? error.detail : 'Проверьте значения фильтров.'); }
    pending = false; await refresh();
  } catch(error) { status.className = 'error'; status.textContent = error.message; }
  finally { pending = false; button.disabled = false; }
});
refresh(); setInterval(refresh, 5000);
