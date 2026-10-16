'use strict';
const form = document.querySelector('#login-form');
const button = document.querySelector('#login-submit');
const message = document.querySelector('#login-message');
let csrf = '';
async function prepare() {
  const response = await fetch('/api/auth/csrf', {credentials:'same-origin', cache:'no-store'});
  if (!response.ok) throw new Error('Не удалось подготовить вход. Обновите страницу.');
  csrf = (await response.json()).csrf_token;
  button.disabled = false;
}
form.addEventListener('submit', async event => {
  event.preventDefault();
  button.disabled = true;
  message.textContent = 'Входим…';
  const password = document.querySelector('#password');
  try {
    const response = await fetch('/api/auth/login', {
      method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrf},
      body:JSON.stringify({username:document.querySelector('#username').value, password:password.value})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Проверьте логин и пароль.');
    window.location.replace('/');
  } catch(error) {
    message.textContent = error.message;
    message.className = 'error';
    try { await prepare(); } catch (retry) { message.textContent = retry.message; }
  } finally { password.value = ''; }
});
prepare().then(() => { message.textContent = ''; }).catch(error => { message.textContent = error.message; });
