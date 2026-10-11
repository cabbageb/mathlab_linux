'use strict';

const $ = selector => document.querySelector(selector);
let email = '';
let cooldown = 0;
let timer = null;

async function post(path, body) {
  const response = await fetch(path, {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

function status(message, error=false) {
  const item = $('#login-status');
  item.textContent = message; item.classList.toggle('error', error);
}

function startCooldown(seconds=60) {
  clearInterval(timer); cooldown = seconds;
  const button = $('#login-resend'); button.disabled = true;
  const update = () => {
    button.textContent = cooldown > 0 ? `${cooldown}s 后可重新发送` : '重新发送';
    button.disabled = cooldown > 0;
    if (cooldown <= 0) clearInterval(timer);
    cooldown -= 1;
  };
  update(); timer = setInterval(update, 1000);
}

async function requestCode() {
  status('正在发送验证码…');
  const result = await post('/api/auth/request-otp', {email});
  $('#login-email-form').classList.add('hidden');
  $('#login-code-form').classList.remove('hidden');
  $('#login-email-copy').textContent = `验证码已发送至 ${result.email}`;
  $('#login-code').focus(); startCooldown(60); status('');
}

$('#login-email-form').addEventListener('submit', async event => {
  event.preventDefault(); email = $('#login-email').value.trim();
  try { await requestCode(); } catch (error) { status(error.message, true); }
});

$('#login-code-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    status('正在验证…');
    await post('/api/auth/verify-otp', {email, code: $('#login-code').value.trim()});
    window.location.replace('/');
  } catch (error) { status(error.message, true); }
});

$('#login-resend').addEventListener('click', async () => {
  try { await requestCode(); } catch (error) { status(error.message, true); }
});

$('#login-change-email').addEventListener('click', () => {
  clearInterval(timer); $('#login-code-form').classList.add('hidden');
  $('#login-email-form').classList.remove('hidden'); $('#login-code').value = '';
  status(''); $('#login-email').focus();
});
