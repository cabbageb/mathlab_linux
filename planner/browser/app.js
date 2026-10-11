'use strict';

const state = {
  bootstrap: null,
  conversation: null,
  selected: {kind: 'version', version: null},
  run: null,
  runEvents: [],
  runEventAfter: 0,
  runTimer: null,
  generationTimer: null,
  generationEventAfter: 0,
  generationEvents: [],
  request: {conversationId: null, revision: 0, autosaveTimer: null, saving: false, sourceIds: new Set()},
  section: {key: null, previewTimer: null, saveTimer: null, syncing: false, splitRatio: .5, savePromise: null, saveQueued: false, lastSavedContent: null},
  managingProjectId: null,
  confirmCallback: null,
  admin: {users: [], groups: [], domains: [], ledger: []},
  otpEmail: '',
};

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const uid = prefix => `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
const EXECUTION_MODEL_TIER_ALIASES = Object.freeze({balanced:'gpt_medium',capability:'gpt_medium',capability_priority:'gpt_medium',gpt_low:'gpt_medium',gpt_high:'gpt_medium'});
const normalizedExecutionModelTier = value => EXECUTION_MODEL_TIER_ALIASES[String(value||'').trim()]||String(value||'').trim()||'gpt_medium';

function cookieValue(name) {
  const prefix = `${encodeURIComponent(name)}=`;
  const part = document.cookie.split('; ').find(item => item.startsWith(prefix));
  return part ? decodeURIComponent(part.slice(prefix.length)) : '';
}

async function api(path, options = {}, {allowUnauthorized = false} = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const method = String(options.method || 'GET').toUpperCase();
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = cookieValue('mathlab_csrf');
    if (csrf) headers.set('X-CSRF-Token', csrf);
  }
  const response = await fetch(path, {...options, headers, credentials: 'same-origin'});
  if (response.status === 401 && !allowUnauthorized) showLogin();
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  const type = response.headers.get('content-type') || '';
  return type.includes('application/json') ? response.json() : response.text();
}

async function downloadAuthenticated(path, filename) {
  const response = await fetch(path, {credentials: 'same-origin'});
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `${response.status}`);
  const blob = await response.blob();
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = href; anchor.download = filename; document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}

async function renderMarkdown(container, source) {
  if (window.MathLabRender?.renderMarkdown) return MathLabRender.renderMarkdown(container, source || '');
  container.textContent = source || '';
}
async function renderMathText(container, source) {
  if (window.MathLabRender?.renderMathText) return MathLabRender.renderMathText(container, source || '');
  container.textContent = source || '';
}
function number(value) { return Number(value || 0).toLocaleString(); }
function money(value) { return `$${Number(value || 0).toFixed(4)}`; }
function formatBytes(bytes) { if (bytes < 1024) return `${bytes} B`; if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`; return `${(bytes / 1024 ** 2).toFixed(1)} MiB`; }

function toast(message, error = false) {
  const item = document.createElement('div');
  item.className = `toast${error ? ' error' : ''}`;
  item.textContent = message;
  $('#toast-region').append(item);
  setTimeout(() => item.remove(), 5000);
}
function openDialog(selector) { const dialog = $(selector); if (dialog && !dialog.open) dialog.showModal(); }
function closeDialog(selector) { const dialog = $(selector); if (dialog?.open) dialog.close(); }
function showLogin() { const d = $('#login-dialog'); if (d && !d.open) d.showModal(); }
function showView(name) { for (const id of ['landing-view','conversation-view','run-view']) $(`#${id}`).classList.add('hidden'); $(`#${name}-view`).classList.remove('hidden'); }
function setSaveState(selector, text, kind = '') { const el = $(selector); if (!el) return; el.textContent = text; el.className = `save-state${kind ? ` ${kind}` : ''}`; }

function confirmAction(title, message, onAccept, acceptLabel = '确认') {
  $('#confirm-title').textContent = title;
  $('#confirm-message').textContent = message;
  $('#confirm-accept').textContent = acceptLabel;
  state.confirmCallback = onAccept;
  openDialog('#confirm-dialog');
}

function clearIdentityScopedDom() {
  clearTimeout(state.request.autosaveTimer);
  clearTimeout(state.section.previewTimer);
  clearTimeout(state.section.saveTimer);
  stopGenerationPolling(); stopRunPolling();
  state.conversation = null; state.run = null;
  state.request = {conversationId: null, revision: 0, autosaveTimer: null, saving: false, sourceIds: new Set()};
  for (const id of ['request-problem','request-background','request-methods','request-partial','request-ideas','contract-chat-input','section-editor']) {
    const el = $(`#${id}`); if (el) el.value = '';
  }
  $('#contract-chat-list').innerHTML = '';
  $('#contract-sections').innerHTML = '';
}

async function refreshBootstrap({keepConversation = true} = {}) {
  try {
    const data = await api('/api/bootstrap', {}, {allowUnauthorized: true});
    state.bootstrap = data;
    closeDialog('#login-dialog');
    renderUser(data.current_user, data.usage);
    renderNavigation();
    renderFileLibrary();
    fillProjectSelects();
    if (!keepConversation || !state.conversation) {
      showView('landing');
      renderMathText($('#breadcrumb'), 'MathLab');
      renderMathText($('#page-title'), 'Research Contracts');
    }
    return true;
  } catch (error) {
    if (error.status === 401) { showLogin(); return false; }
    throw error;
  }
}

function renderUser(user, usage) {
  $('#user-name').textContent = user.display_name;
  $('#user-role').textContent = `${user.role} · ${state.bootstrap?.application.dev_mode ? 'development' : 'production'}`;
  $('#user-avatar').textContent = (user.display_name || 'U').slice(0, 1).toUpperCase();
  $('#user-dialog-name').textContent = user.display_name;
  $('#pref-name').value = user.display_name;
  $('#pref-model').value = normalizedExecutionModelTier(user.default_model_strategy);
  $('#pref-budget').value = user.default_budget_preset || 'balanced_3h';
  document.documentElement.style.setProperty('--chat-width', `${Math.round((user.contract_chat_panel_width || .34) * 100)}%`);
  const tiles = [
    ['Credit', money(usage.credit_balance)], ['Available', money(usage.available_credit)], ['Reserved', money(usage.reserved_credit)],
    ['This month', money(usage.monthly_cost)], ['Model calls', number(usage.calls)], ['Input tokens', number(usage.input_tokens)],
    ['Output tokens', number(usage.output_tokens)], ['Cached tokens', number(usage.cached_input_tokens)],
    ['Active runs', `${usage.active_runs}/${usage.max_concurrent_research_runs}`],
  ];
  $('#usage-grid').innerHTML = tiles.map(([a,b]) => `<div class="usage-tile"><strong>${escapeHtml(b)}</strong><span>${escapeHtml(a)}</span></div>`).join('');
  const switcher = $('#dev-user-switch');
  switcher.innerHTML = state.bootstrap.application.dev_mode ? `<strong>本地测试用户</strong>${state.bootstrap.dev_users.map(item => `<button class="dev-user-button" type="button" data-user-id="${item.user_id}"><span>${escapeHtml(item.display_name)}</span><small>${escapeHtml(item.email || item.user_id)}</small></button>`).join('')}` : '';
  $('#open-admin').classList.toggle('hidden', user.role !== 'admin');
}

function conversationMarker(item) {
  if (item.status === 'completed') return '✓';
  if (item.status === 'contract_generating') return '◐';
  if (item.status === 'request_draft') return '✎';
  if (['execution_locked','running'].includes(item.status)) return '◉';
  return '◌';
}
function createChatRow(item) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `entity-row chat-row${state.conversation?.conversation_id === item.conversation_id ? ' active' : ''}`;
  button.dataset.conversationId = item.conversation_id;
  const marker = document.createElement('span'); marker.textContent = conversationMarker(item);
  const title = document.createElement('span'); title.className = 'entity-title math-text'; title.textContent = item.display_name;
  const meta = document.createElement('small'); meta.textContent = item.status === 'request_draft' ? 'Draft' : `v${item.latest_contract_version}`;
  button.append(marker, title, meta); renderMathText(title, item.display_name);
  return button;
}
function renderNavigation() {
  const conversations = state.bootstrap.conversations || [];
  const byProject = new Map((state.bootstrap.projects || []).map(p => [p.project_id, []]));
  const unfiled = [];
  for (const c of conversations) (c.project_id && byProject.has(c.project_id) ? byProject.get(c.project_id) : unfiled).push(c);
  const projectList = $('#project-list'); projectList.replaceChildren();
  for (const project of state.bootstrap.projects || []) {
    const block = document.createElement('div'); block.className = 'project-block';
    const head = document.createElement('button'); head.type = 'button'; head.className = 'entity-row project-heading'; head.dataset.projectId = project.project_id;
    const arrow = document.createElement('span'); arrow.textContent = '▾';
    const title = document.createElement('span'); title.className = 'entity-title math-text'; title.textContent = project.name;
    const menu = document.createElement('span'); menu.className = 'project-actions'; menu.dataset.projectMenu = project.project_id; menu.textContent = '•••';
    head.append(arrow,title,menu); block.append(head); renderMathText(title, project.name);
    const chats = document.createElement('div'); chats.className = 'project-chats';
    const rows = byProject.get(project.project_id) || [];
    if (rows.length) rows.forEach(c => chats.append(createChatRow(c))); else chats.innerHTML = '<div class="empty-nav">暂无 Contract</div>';
    block.append(chats); projectList.append(block);
  }
  if (!state.bootstrap.projects?.length) projectList.innerHTML = '<div class="empty-nav">尚无 Project</div>';
  const unfiledList = $('#unfiled-list'); unfiledList.replaceChildren();
  if (unfiled.length) unfiled.forEach(c => unfiledList.append(createChatRow(c))); else unfiledList.innerHTML = '<div class="empty-nav">暂无未归档对话</div>';
}
function fillProjectSelects() {
  const options = `<option value="">Unfiled</option>${(state.bootstrap.projects || []).map(p => `<option value="${p.project_id}">${escapeHtml(p.name)}</option>`).join('')}`;
  $('#request-project').innerHTML = options; $('#conversation-manage-project').innerHTML = options;
}
function projectName(id) { return state.bootstrap.projects.find(p => p.project_id === id)?.name || 'Unfiled'; }

function resetRequestForm() {
  state.request = {conversationId: null, revision: 0, autosaveTimer: null, saving: false, sourceIds: new Set()};
  for (const id of ['request-problem','request-background','request-methods','request-partial','request-ideas']) $(`#${id}`).value = '';
  $('#request-project').value = ''; $('#request-profile').value = 'high';
  $('#request-dialog-title').textContent = '你真正想研究什么？';
  setSaveState('#request-save-state', '尚未保存到服务端');
  renderRequestFiles();
}
function requestPayload() {
  return {
    research_problem: $('#request-problem').value.trim(),
    background_and_prior_progress: $('#request-background').value.trim(),
    method_preferences_and_restrictions: $('#request-methods').value.trim(),
    valuable_partial_outcomes: $('#request-partial').value.trim(),
    possible_ideas_and_rough_approaches: $('#request-ideas').value.trim(),
    contractor_profile: $('#request-profile').value,
    project_id: $('#request-project').value || null,
    source_file_ids: [...state.request.sourceIds],
    expected_revision: state.request.revision,
  };
}
function populateRequestDraft(detail) {
  const d = detail.request_draft;
  state.request = {conversationId: detail.conversation_id, revision: d.draft_revision, autosaveTimer: null, saving: false, sourceIds: new Set(d.source_file_ids || [])};
  $('#request-problem').value = d.research_problem || '';
  $('#request-background').value = d.background_and_prior_progress || '';
  $('#request-methods').value = d.method_preferences_and_restrictions || '';
  $('#request-partial').value = d.valuable_partial_outcomes || '';
  $('#request-ideas').value = d.possible_ideas_and_rough_approaches || '';
  $('#request-profile').value = d.contractor_profile || 'high';
  $('#request-project').value = detail.project_id || '';
  $('#request-dialog-title').textContent = detail.display_name || 'Research Request Draft';
  setSaveState('#request-save-state', `已暂存 · revision ${state.request.revision}`);
  renderRequestFiles();
}
function renderRequestFiles() {
  const box = $('#request-files'); if (!box || !state.bootstrap) return;
  box.innerHTML = (state.bootstrap.files || []).map(file => `<label class="file-check"><input type="checkbox" value="${file.file_id}" ${state.request.sourceIds.has(file.file_id) ? 'checked' : ''}><span>${escapeHtml(file.name)}<small>${formatBytes(file.size_bytes)}</small></span></label>`).join('') || '<p class="muted">尚未上传 Source Material。</p>';
  $$('#request-files input').forEach(input => input.addEventListener('change', () => { input.checked ? state.request.sourceIds.add(input.value) : state.request.sourceIds.delete(input.value); scheduleRequestAutosave(); }));
}
function scheduleRequestAutosave() {
  if (!state.request.conversationId) return;
  clearTimeout(state.request.autosaveTimer);
  setSaveState('#request-save-state','等待自动保存…','saving');
  state.request.autosaveTimer = setTimeout(() => saveRequestDraft({silent:true}), 800);
}
async function saveRequestDraft({silent=false} = {}) {
  const payload = requestPayload();
  if (!payload.research_problem) { if (!silent) toast('“想研究的问题”不能为空', true); return null; }
  if (state.request.saving) return null;
  state.request.saving = true; setSaveState('#request-save-state','保存中…','saving');
  try {
    const path = state.request.conversationId ? `/api/contract-drafts/${state.request.conversationId}` : '/api/contract-drafts';
    const method = state.request.conversationId ? 'PATCH' : 'POST';
    const saved = await api(path,{method,body:JSON.stringify(payload)});
    state.request.conversationId = saved.conversation_id; state.request.revision = saved.draft_revision;
    setSaveState('#request-save-state',`已暂存 · revision ${saved.draft_revision}`);
    await refreshBootstrap({keepConversation:true});
    if (!silent) toast('Research Request Draft 已暂存');
    return saved;
  } catch (error) {
    setSaveState('#request-save-state',error.message,'error');
    if (!silent) toast(error.message,true);
    return null;
  } finally { state.request.saving = false; }
}
async function generateFromRequest(event) {
  event.preventDefault();
  const saved = await saveRequestDraft({silent:true}); if (!saved) return;
  try {
    const queued = await api(`/api/conversations/${saved.conversation_id}/generate`,{method:'POST',body:JSON.stringify({idempotency_key:uid('contract-generate')})});
    closeDialog('#new-contract-dialog');
    $('#generation-notice').classList.remove('hidden');
    await refreshBootstrap({keepConversation:true});
    await openConversation(saved.conversation_id,{showLoading:false});
    toast('Research Contract 已进入后台生成队列');
  } catch (error) { toast(error.message,true); }
}
async function openNewContract() { resetRequestForm(); openDialog('#new-contract-dialog'); }
async function openRequestDraftConversation(detail) { populateRequestDraft(detail); openDialog('#new-contract-dialog'); }

async function openConversation(id,{showLoading=true}={}) {
  stopRunPolling();
  try {
    state.conversation = await api(`/api/conversations/${id}`);
    if (state.conversation.status === 'request_draft' && state.conversation.request_draft) {
      renderNavigation(); await openRequestDraftConversation(state.conversation); return;
    }
    chooseDefaultContractView(); renderNavigation(); await renderConversation();
    if (state.conversation.run) await openRun(state.conversation.run.run_id,false);
  } catch (error) { toast(error.message,true); }
}
function chooseDefaultContractView() {
  const c = state.conversation;
  if (state.selected.kind === 'draft' && c.working_draft) return;
  if (state.selected.kind === 'version' && c.versions.some(v => v.version === state.selected.version)) return;
  state.selected = c.working_draft ? {kind:'draft',version:null} : {kind:'version',version:c.latest_contract_version || null};
}
function selectedContractData() {
  const c = state.conversation;
  if (!c) return null;
  if (state.selected.kind === 'draft') return c.working_draft ? {...c.working_draft,kind:'draft'} : null;
  const version = c.versions.find(v => v.version === Number(state.selected.version));
  return version ? {...version,kind:'version'} : null;
}
function isConversationLocked() { return ['execution_locked','running','completed'].includes(state.conversation?.status); }
async function renderConversation() {
  const c = state.conversation; if (!c) return;
  showView('conversation');
  renderMathText($('#breadcrumb'), c.project_id ? projectName(c.project_id) : 'Unfiled');
  renderMathText($('#page-title'), c.display_name);
  $('#conversation-status').textContent = c.status;
  const select = $('#version-select');
  const opts = c.versions.map(v => `<option value="version:${v.version}">Contract v${v.version}</option>`);
  if (c.working_draft) opts.push(`<option value="draft">Working Draft · based on v${c.working_draft.base_version}</option>`);
  select.innerHTML = opts.join('') || '<option>尚无 Contract</option>';
  select.disabled = !opts.length;
  select.value = state.selected.kind === 'draft' ? 'draft' : `version:${state.selected.version}`;
  $('#draft-badge').classList.toggle('hidden', !c.working_draft);
  $('#save-contract-version').classList.toggle('hidden', !(c.working_draft && state.selected.kind === 'draft') || isConversationLocked());
  $('#discard-working-draft').classList.toggle('hidden', !c.working_draft || isConversationLocked());
  $('#clone-contract').disabled = !c.versions.length;
  $('#full-regenerate').disabled = !c.versions.length || isConversationLocked();
  $('#contract-chat-input').disabled = !c.versions.length || isConversationLocked();
  $('#contract-chat-form button[type="submit"]').disabled = !c.versions.length || isConversationLocked();
  $('#contract-sources').disabled = isConversationLocked();
  await renderGenerationPanel();
  await renderContract();
  await renderContractChat();
  startGenerationPollingIfNeeded();
}
async function renderGenerationPanel() {
  const g = state.conversation.generation;
  const active = g && ['queued','running','repairing'].includes(g.status);
  $('#generation-panel').classList.toggle('hidden', !active && !g?.error);
  if (!g) return;
  $('#generation-title').textContent = g.status === 'failed' ? 'Research Contract 生成失败' : 'Research Contract 正在后台生成';
  $('#generation-stage').textContent = g.error || ({queued:'等待 Runner 领取任务。',running:'Contractor / Auditors / Integrator 正在工作。',repairing:'同一 Agent thread 正在修复结构问题。',completed:'新 Contract Version 已生成。'}[g.status] || g.status);
  $('#generation-events').innerHTML = state.generationEvents.slice(-2).map(e => `<span>${escapeHtml(e.message)}</span>`).join('');
  if (active) $('#generation-notice').classList.remove('hidden');
}
async function renderContract() {
  const data = selectedContractData();
  $('#contract-empty').classList.toggle('hidden', Boolean(data));
  $('#contract-sections').replaceChildren();
  $('#execution-policy').classList.toggle('hidden', !data);
  $('#approval-zone').classList.toggle('hidden', !data || isConversationLocked());
  if (!data) return;
  const title = document.createElement('div'); title.className='contract-title';
  const h1=document.createElement('h1'); h1.textContent=data.title || 'Research Contract'; title.append(h1); $('#contract-sections').append(title); renderMathText(h1,data.title || 'Research Contract');
  const editable = new Set(state.conversation.editable_section_keys || []);
  for (const spec of state.bootstrap.application.contract_sections) {
    if (['mathematical_contract','research_policy'].includes(spec.key)) {
      const group=document.createElement('div'); group.className='contract-group-heading';
      const heading=document.createElement(spec.level===2?'h2':'h3'); heading.textContent=spec.heading; group.append(heading); $('#contract-sections').append(group); renderMathText(heading,spec.heading); continue;
    }
    const section=document.createElement('section'); section.className=`contract-section${editable.has(spec.key)&&!isConversationLocked()?' editable':''}`; section.dataset.sectionKey=spec.key; section.dataset.editable=editable.has(spec.key)&&!isConversationLocked()?'true':'false';
    const heading=document.createElement(spec.level===2?'h2':'h3'); heading.textContent=spec.heading;
    const body=document.createElement('div'); body.className='markdown';
    section.append(heading,body); $('#contract-sections').append(section); renderMathText(heading,spec.heading); renderMarkdown(body,data.sections?.[spec.key] || '');
  }
  renderExecutionPolicy(data.execution_policy || {});
  $('#approval-copy').textContent = data.kind === 'draft' ? '批准时将先冻结当前 Working Draft 为一个新的不可变版本。' : `将执行当前正在查看的 Contract v${data.version}。`;
}
function renderExecutionPolicy(policy) {
  const catalog=state.bootstrap.execution_policy_catalog || {}; const models=catalog.model_tiers||{}; const durations=catalog.duration_tiers||{};
  $('#execution-model-tier').innerHTML=Object.entries(models).map(([key,v])=>`<option value="${key}">${escapeHtml(v.label||key)}</option>`).join('');
  $('#execution-duration-tier').innerHTML=Object.entries(durations).map(([key,v])=>`<option value="${key}">${escapeHtml(v.label||key)}</option>`).join('');
  const defaultModel=normalizedExecutionModelTier(state.bootstrap.current_user.default_model_strategy); const defaultDuration=state.bootstrap.current_user.default_budget_preset;
  const requestedModel=normalizedExecutionModelTier(policy.model_tier||defaultModel);
  $('#execution-model-tier').value=models[requestedModel]?requestedModel:(Object.keys(models)[0]||'');
  $('#execution-duration-tier').value=policy.duration_tier||defaultDuration||Object.keys(durations)[0]||'';
  updateExecutionEstimate(policy);
  const editable=!isConversationLocked(); $('#execution-model-tier').disabled=!editable; $('#execution-duration-tier').disabled=!editable;
}
function currentCatalogPolicy() {
  const model=$('#execution-model-tier').value; const duration=$('#execution-duration-tier').value; const catalog=state.bootstrap.execution_policy_catalog||{};
  return {model_tier:model,duration_tier:duration,estimated_cost:Number(catalog.price_table?.[model]?.[duration]||0),pricing_version:catalog.pricing_version||'unknown',duration_seconds:Number(catalog.duration_tiers?.[duration]?.duration_seconds||0)};
}
function updateExecutionEstimate(policy=null) {
  const p=policy?.model_tier?policy:currentCatalogPolicy();
  const catalog=state.bootstrap.execution_policy_catalog||{}; const estimate=p.estimated_cost ?? Number(catalog.price_table?.[p.model_tier]?.[p.duration_tier]||0);
  $('#execution-estimated-cost').textContent=money(estimate); $('#execution-pricing-version').textContent=`${p.pricing_version||catalog.pricing_version||'unknown'} · 预估，非最终计费`;
}
async function ensureWorkingDraft(baseVersion=null) {
  const existing=state.conversation.working_draft;
  if(existing){
    if(baseVersion && Number(existing.base_version)!==Number(baseVersion)){
      throw new Error(`当前已有基于 Contract v${existing.base_version} 的 Working Draft；请先保存或放弃该草稿，再编辑 Contract v${baseVersion}。`);
    }
    return existing;
  }
  const draft=await api(`/api/conversations/${state.conversation.conversation_id}/draft`,{method:'POST',body:JSON.stringify({base_version:baseVersion||state.selected.version||null})});
  state.conversation.working_draft=draft; state.selected={kind:'draft',version:null}; return draft;
}
async function patchExecutionPolicy() {
  if (isConversationLocked()) return;
  try {
    const draft=await ensureWorkingDraft(state.selected.kind==='version'?state.selected.version:null);
    const p=currentCatalogPolicy();
    const updated=await api(`/api/conversations/${state.conversation.conversation_id}/draft/execution`,{method:'PATCH',body:JSON.stringify({model_tier:p.model_tier,duration_tier:p.duration_tier,expected_revision:draft.draft_revision})});
    state.conversation.working_draft=updated; state.selected={kind:'draft',version:null}; await renderConversation();
  } catch(error){toast(error.message,true); await reloadConversation();}
}
async function renderContractChat() {
  const list=$('#contract-chat-list'); list.replaceChildren();
  const messages=state.conversation.contract_chat_messages||[];
  if(!messages.length){list.innerHTML='<div class="empty-state"><p>这里是一段新的 Contract 对话。你可以询问设计理由，或明确要求修改 A–F。</p></div>';return;}
  for(const m of messages){const card=document.createElement('article');card.className=`chat-message ${m.role}`;const role=document.createElement('span');role.className='message-role';role.textContent=m.role;const body=document.createElement('div');body.className='markdown';card.append(role,body);list.append(card);await renderMarkdown(body,m.content);} list.scrollTop=list.scrollHeight;
}
function startGenerationPollingIfNeeded(){stopGenerationPolling();const g=state.conversation?.generation;if(g&&['queued','running','repairing'].includes(g.status)){state.generationEventAfter=0;state.generationEvents=[];state.generationTimer=setInterval(pollGeneration,1500);pollGeneration();}}
function stopGenerationPolling(){if(state.generationTimer)clearInterval(state.generationTimer);state.generationTimer=null;}
async function pollGeneration(){const c=state.conversation;if(!c?.generation)return;try{const status=await api(`/api/conversations/${c.conversation_id}/generation`);const attempt=status.attempt;if(!attempt)return;const events=await api(`/api/conversations/${c.conversation_id}/generation-events?attempt_id=${encodeURIComponent(attempt.generation_attempt_id)}&after=${state.generationEventAfter}`);for(const e of events){state.generationEvents.push(e);state.generationEventAfter=Math.max(state.generationEventAfter,e.sequence);}c.generation=attempt;await renderGenerationPanel();if(['completed','failed'].includes(attempt.status)){stopGenerationPolling();if(attempt.status==='completed'){$('#generation-notice').classList.add('hidden');await refreshBootstrap({keepConversation:true});await reloadConversation();}else toast(attempt.error||'Contract generation failed',true);}}catch(error){console.warn(error);}}
async function reloadConversation(){if(!state.conversation)return;const id=state.conversation.conversation_id;state.conversation=await api(`/api/conversations/${id}`);chooseDefaultContractView();renderNavigation();await renderConversation();}

async function openSectionEditor(key) {
  if(isConversationLocked())return;
  try{
    const requestedBase=state.selected.kind==='version'?state.selected.version:null;
    await ensureWorkingDraft(requestedBase);
    await reloadConversation();
    state.selected={kind:'draft',version:null};
    const draft=state.conversation.working_draft; state.section.key=key; $('#section-editor').value=draft.sections[key]||'';
    const spec=state.bootstrap.application.contract_sections.find(s=>s.key===key); state.section.lastSavedContent=$('#section-editor').value; state.section.saveQueued=false; renderMathText($('#section-heading'),spec?.heading||key); setSaveState('#section-save-state',`已暂存 · revision ${draft.draft_revision}`); $('#section-validation').classList.add('hidden'); openDialog('#section-dialog'); scheduleSectionPreview(true);
  }catch(error){toast(error.message,true);}
}
function scheduleSectionPreview(immediate=false){clearTimeout(state.section.previewTimer);state.section.previewTimer=setTimeout(()=>{renderMarkdown($('#section-preview'),$('#section-editor').value);restoreEditorScrollRatio();},immediate?0:200);}
function scheduleSectionAutosave(){clearTimeout(state.section.saveTimer);setSaveState('#section-save-state','等待自动保存…','saving');state.section.saveTimer=setTimeout(()=>saveCurrentSection(),800);}
async function saveCurrentSection({silent=true}={}){
  if(!state.section.key||!state.conversation?.working_draft)return null;
  clearTimeout(state.section.saveTimer);
  state.section.saveQueued=true;
  if(state.section.savePromise)return state.section.savePromise;
  state.section.savePromise=(async()=>{
    let last=null;
    while(state.section.saveQueued&&state.section.key&&state.conversation?.working_draft){
      state.section.saveQueued=false;
      const key=state.section.key;
      const content=$('#section-editor').value;
      if(state.section.lastSavedContent===content){
        setSaveState('#section-save-state',`已暂存 · revision ${state.conversation.working_draft.draft_revision}`);
        continue;
      }
      const draft=state.conversation.working_draft;
      setSaveState('#section-save-state','保存中…','saving');
      try{
        const updated=await api(`/api/conversations/${state.conversation.conversation_id}/draft`,{method:'PATCH',body:JSON.stringify({section_key:key,content,expected_revision:draft.draft_revision})});
        state.conversation.working_draft=updated;
        state.selected={kind:'draft',version:null};
        state.section.lastSavedContent=content;
        last=updated;
        setSaveState('#section-save-state',`已暂存 · revision ${updated.draft_revision}`);
        if(!silent)toast('Working Draft 已暂存');
      }catch(error){
        setSaveState('#section-save-state',error.message,'error');
        $('#section-validation').textContent=error.message;
        $('#section-validation').classList.remove('hidden');
        if(!silent)toast(error.message,true);
        if(error.status===409)await reloadConversation();
        return null;
      }
    }
    return last||state.conversation?.working_draft||null;
  })().finally(()=>{state.section.savePromise=null;});
  return state.section.savePromise;
}
function editorScrollRatio(el){const max=el.scrollHeight-el.clientHeight;return max>0?el.scrollTop/max:0;}function setEditorRatio(el,ratio){const max=el.scrollHeight-el.clientHeight;el.scrollTop=max*ratio;}function restoreEditorScrollRatio(){const ratio=editorScrollRatio($('#section-editor'));requestAnimationFrame(()=>setEditorRatio($('#section-preview'),ratio));}
function setupScrollSync(){const left=$('#section-editor'),right=$('#section-preview');let owner=null;let frame=null;const sync=(from,to,name)=>{if(state.section.syncing&&owner!==name)return;owner=name;state.section.syncing=true;if(frame)cancelAnimationFrame(frame);frame=requestAnimationFrame(()=>{setEditorRatio(to,editorScrollRatio(from));state.section.syncing=false;owner=null;});};left.addEventListener('scroll',()=>sync(left,right,'left'));right.addEventListener('scroll',()=>sync(right,left,'right'));}
function setupDragDivider(divider,onRatio){let active=false;let container=null;const move=e=>{if(!active)return;const r=container.getBoundingClientRect();const ratio=Math.min(.75,Math.max(.25,(e.clientX-r.left)/r.width));onRatio(ratio);};divider.addEventListener('pointerdown',e=>{active=true;container=divider.parentElement;divider.classList.add('dragging');divider.setPointerCapture(e.pointerId)});divider.addEventListener('pointermove',move);divider.addEventListener('pointerup',e=>{active=false;divider.classList.remove('dragging');divider.releasePointerCapture(e.pointerId)});}

async function commitDraft(){try{await flushSectionIfOpen();const result=await api(`/api/conversations/${state.conversation.conversation_id}/draft/commit`,{method:'POST'});toast(`Contract v${result.version} 已保存`);state.selected={kind:'version',version:result.version};await refreshBootstrap({keepConversation:true});await reloadConversation();}catch(error){toast(error.message,true);}}
async function discardDraft(){confirmAction('放弃 Working Draft','这会永久丢弃尚未冻结的 Draft；历史 Contract Version 不受影响。',async()=>{try{await api(`/api/conversations/${state.conversation.conversation_id}/draft`,{method:'DELETE'});state.selected={kind:'version',version:state.conversation.latest_contract_version};await reloadConversation();toast('Working Draft 已放弃');}catch(error){toast(error.message,true);}},'放弃草稿');}
async function flushSectionIfOpen(){if($('#section-dialog').open&&state.section.key)await saveCurrentSection({silent:true});}
async function approveContract(ack=false){try{await flushSectionIfOpen();const target=selectedContractData();if(!target)return;const payload=target.kind==='draft'?{target_kind:'working_draft',expected_draft_revision:target.draft_revision}:{target_kind:'immutable_version',version:target.version,acknowledge_historical_with_draft:ack};const result=await api(`/api/conversations/${state.conversation.conversation_id}/approve`,{method:'POST',body:JSON.stringify(payload)});toast('Research Run 已创建');await refreshBootstrap({keepConversation:true});await openRun(result.run_id,true);}catch(error){if(String(error.message).startsWith('WorkingDraftApprovalWarning:')){confirmAction('批准历史 Contract Version',error.message.replace('WorkingDraftApprovalWarning:',''),()=>approveContract(true),'确认执行历史版本');}else toast(error.message,true);}}
async function cloneContract(){const d=selectedContractData();if(!d||d.kind!=='version'){toast('请先选择一个 immutable Contract Version',true);return;}try{const result=await api(`/api/conversations/${state.conversation.conversation_id}/clone`,{method:'POST',body:JSON.stringify({version:d.version})});await refreshBootstrap({keepConversation:true});await openConversation(result.conversation_id);if(result.missing_sources?.length)toast(`分支已创建；缺失源文件：${result.missing_sources.join('、')}`,true);else toast('已从当前 Contract 新建独立对话');}catch(error){toast(error.message,true);}}
async function fullRegenerate(){if(isConversationLocked())return;const d=selectedContractData();const version=d?.kind==='version'?d.version:state.conversation.working_draft?.base_version||state.conversation.latest_contract_version;confirmAction('完整重新生成新版本','这会调用 Contractor、三名 fresh Auditor 与 Integrator。普通 Contract Chat 不会触发这条昂贵流程。',async()=>{try{await api(`/api/conversations/${state.conversation.conversation_id}/regenerate`,{method:'POST',body:JSON.stringify({selected_version:version,idempotency_key:uid('contract-regenerate')})});$('#generation-notice').classList.remove('hidden');await reloadConversation();toast('完整重新生成已进入后台队列');}catch(error){toast(error.message,true);}},'开始完整生成');}
async function sendContractChat(event){event.preventDefault();const input=$('#contract-chat-input');const message=input.value.trim();if(!message)return;try{const draft=state.conversation.working_draft;const selectedVersion=state.selected.kind==='version'?state.selected.version:null;const compatible=!draft||state.selected.kind==='draft'||Number(draft.base_version)===Number(selectedVersion);const result=await api(`/api/conversations/${state.conversation.conversation_id}/chat`,{method:'POST',body:JSON.stringify({message,allow_draft_changes:compatible,expected_draft_revision:compatible?(draft?.draft_revision??0):0,selected_version:selectedVersion,profile:state.conversation.contractor_profile||'high'})});input.value='';if(result.draft?.sections){state.conversation.working_draft=result.draft;state.selected={kind:'draft',version:null};}await reloadConversation();const changed=Object.keys(result.patches||{}).length;if(!compatible&&changed===0)toast('当前 Working Draft 基于另一历史版本；本轮只进行了讨论，没有修改草稿。');else toast(changed?'Contract Chat 已更新 Working Draft':'Contract Chat 已回复');}catch(error){toast(error.message,true);if(error.status===409)await reloadConversation();}}

async function openRun(runId,switchView=true){stopRunPolling();try{state.run=await api(`/api/runs/${runId}`);state.runEvents=[];state.runEventAfter=0;if(switchView)showView('run');await renderRun();startRunPolling();}catch(error){toast(error.message,true);}}
async function renderRun(){if(!state.run)return;showView('run');renderMathText($('#breadcrumb'),'Research Run');renderMathText($('#page-title'),state.conversation?.display_name||state.run.run_id);renderMathText($('#run-title'),state.conversation?.display_name||'Dynamic Research Graph');$('#run-status').textContent=state.run.status;$('#run-graph-version').textContent=`graph v${state.run.graph_version}`;$('#download-report').classList.toggle('hidden',!state.run.final_report_path);const grid=$('#node-grid');grid.replaceChildren();for(const n of state.run.nodes||[]){const card=document.createElement('article');card.className=`node-card ${n.kind}`;const tag=document.createElement('span');tag.className='kind-tag';tag.textContent=`${n.kind} · ${n.status}`;const h=document.createElement('h3');h.textContent=n.title;const p=document.createElement('p');p.textContent=n.objective;card.append(tag,h,p);grid.append(card);renderMathText(h,n.title);renderMathText(p,n.objective);}if(!state.run.nodes?.length)grid.innerHTML='<p class="muted">Research Nodes 尚未物化。</p>';$('#event-list').replaceChildren();for(const e of state.runEvents.slice(-100)){const row=document.createElement('div');row.className='event-item';row.innerHTML=`<span>${escapeHtml(e.kind)}</span><p></p><small>${escapeHtml(new Date(e.created_at).toLocaleTimeString())}</small>`;row.querySelector('p').textContent=e.message;$('#event-list').append(row);renderMathText(row.querySelector('p'),e.message);}const claims=$('#claim-list');claims.replaceChildren();for(const c of state.run.verified_claims||[]){const row=document.createElement('article');row.className='claim-item';const strong=document.createElement('strong');strong.textContent=c.verdict;const body=document.createElement('div');body.className='markdown';row.append(strong,body);claims.append(row);renderMarkdown(body,c.statement);}if(!state.run.verified_claims?.length)claims.innerHTML='<p class="muted">尚无 fresh-verified Claim。</p>';}
function startRunPolling(){stopRunPolling();if(!state.run||['completed','failed','interrupted'].includes(state.run.status))return;state.runTimer=setInterval(pollRun,1800);pollRun();}
function stopRunPolling(){if(state.runTimer)clearInterval(state.runTimer);state.runTimer=null;}
async function pollRun(){if(!state.run)return;try{const events=await api(`/api/runs/${state.run.run_id}/events?after=${state.runEventAfter}`);for(const e of events){state.runEvents.push(e);state.runEventAfter=Math.max(state.runEventAfter,e.sequence);}state.run=await api(`/api/runs/${state.run.run_id}`);await renderRun();if(['completed','failed','interrupted'].includes(state.run.status))stopRunPolling();}catch(error){console.warn(error);}}

function renderFileLibrary(){const box=$('#file-list');if(!box||!state.bootstrap)return;box.innerHTML=(state.bootstrap.files||[]).map(f=>`<div class="file-row"><div><strong>${escapeHtml(f.name)}</strong><small>${formatBytes(f.size_bytes)} · ${escapeHtml(f.sha256.slice(0,12))}</small></div><div class="file-actions"><button class="ghost compact-button" type="button" data-download-file="${f.file_id}" data-file-name="${escapeHtml(f.name)}">下载</button><button class="ghost danger-text compact-button" type="button" data-delete-file="${f.file_id}">删除</button></div></div>`).join('')||'<p class="muted">尚无文件。</p>';}
async function uploadFile(input,{selectInRequest=false}={}){const file=input.files?.[0];if(!file)return;const fd=new FormData();fd.append('file',file);try{const item=await api('/api/files',{method:'POST',body:fd});await refreshBootstrap({keepConversation:true});if(selectInRequest)state.request.sourceIds.add(item.file_id);renderRequestFiles();renderFileLibrary();input.value='';toast('文件已上传');return item;}catch(error){toast(error.message,true);return null;}}
function renderConversationSourcePicker(){const selected=new Set(state.conversation.source_manifest?.filter(x=>x.availability==='available').map(x=>x.file_id)||[]);$('#conversation-source-picker').innerHTML=(state.bootstrap.files||[]).map(f=>`<label class="file-check"><input type="checkbox" value="${f.file_id}" ${selected.has(f.file_id)?'checked':''}><span>${escapeHtml(f.name)}</span></label>`).join('')||'<p class="muted">无可用文件。</p>';}

async function createNewProject(){const name=prompt('Project name');if(!name)return;try{await api('/api/projects',{method:'POST',body:JSON.stringify({name})});await refreshBootstrap();}catch(error){toast(error.message,true);}}
function openProjectManager(id){const p=state.bootstrap.projects.find(x=>x.project_id===id);if(!p)return;state.managingProjectId=id;$('#project-manage-name').value=p.name;openDialog('#project-manage-dialog');}
async function saveManagedProject(){try{await api(`/api/projects/${state.managingProjectId}`,{method:'PATCH',body:JSON.stringify({name:$('#project-manage-name').value})});closeDialog('#project-manage-dialog');await refreshBootstrap();}catch(error){toast(error.message,true);}}
function deleteManagedProject(){confirmAction('永久删除 Project','This will permanently delete this project and all its chats and tasks. This action cannot be undone.',async()=>{try{await api(`/api/projects/${state.managingProjectId}?confirm=permanently-delete-project-and-all-chats-and-tasks`,{method:'DELETE'});closeDialog('#project-manage-dialog');clearIdentityScopedDom();await refreshBootstrap({keepConversation:false});}catch(error){toast(error.message,true);}},'永久删除');}
function openConversationManager(){if(!state.conversation)return;$('#conversation-manage-name').value=state.conversation.display_name;$('#conversation-manage-project').value=state.conversation.project_id||'';openDialog('#conversation-manage-dialog');}
async function saveManagedConversation(){try{await api(`/api/conversations/${state.conversation.conversation_id}`,{method:'PATCH',body:JSON.stringify({name:$('#conversation-manage-name').value})});await api(`/api/conversations/${state.conversation.conversation_id}/move`,{method:'POST',body:JSON.stringify({project_id:$('#conversation-manage-project').value||null})});closeDialog('#conversation-manage-dialog');await refreshBootstrap({keepConversation:true});await reloadConversation();}catch(error){toast(error.message,true);}}
function deleteManagedConversation(){confirmAction('永久删除 Contract Conversation','This will permanently delete this chat, all Contract Versions, Research Runs, tasks and mathematical artifacts. Usage history is retained.',async()=>{try{await api(`/api/conversations/${state.conversation.conversation_id}?confirm=permanently-delete-contract-conversation`,{method:'DELETE'});closeDialog('#conversation-manage-dialog');clearIdentityScopedDom();await refreshBootstrap({keepConversation:false});}catch(error){toast(error.message,true);}},'永久删除');}

async function requestOtp(event){event.preventDefault();const email=$('#login-email').value.trim();try{const result=await api('/api/auth/request-otp',{method:'POST',body:JSON.stringify({email})},{allowUnauthorized:true});state.otpEmail=email;$('#otp-request-form').classList.add('hidden');$('#otp-verify-form').classList.remove('hidden');if(result.dev_code){$('#otp-dev-hint').textContent=`DEV code: ${result.dev_code}`;$('#otp-dev-hint').classList.remove('hidden');}toast('验证码已发送');}catch(error){toast(error.message,true);}}
async function verifyOtp(event){event.preventDefault();try{await api('/api/auth/verify-otp',{method:'POST',body:JSON.stringify({email:state.otpEmail,code:$('#login-code').value})},{allowUnauthorized:true});resetOtpForm();await refreshBootstrap({keepConversation:false});}catch(error){toast(error.message,true);}}
function resetOtpForm(){$('#otp-request-form').classList.remove('hidden');$('#otp-verify-form').classList.add('hidden');$('#login-code').value='';$('#otp-dev-hint').classList.add('hidden');}
async function logout(){try{await api('/api/auth/logout',{method:'POST'});}catch(_){}clearIdentityScopedDom();state.bootstrap=null;closeDialog('#user-dialog');showLogin();}

async function openAdmin(){try{await refreshAdmin();openDialog('#admin-dialog');}catch(error){toast(error.message,true);}}
async function refreshAdmin(){const [users,groups,domains,ledger]=await Promise.all([api('/api/admin/users'),api('/api/admin/groups'),api('/api/admin/domains'),api('/api/admin/credit-ledger')]);state.admin={users,groups,domains,ledger};renderAdminUsers();renderAdminGroups();renderAdminDomains();renderAdminLedger();}
function renderAdminUsers(){$('#admin-users').innerHTML=`<div class="admin-list">${state.admin.users.map(u=>`<article class="admin-row"><div><strong>${escapeHtml(u.display_name)}</strong><small>${escapeHtml(u.email||u.user_id)}</small></div><div class="chip-row"><span class="chip">${escapeHtml(u.role)}</span><span class="chip">${escapeHtml(u.status)}</span><span class="chip">credit ${money(u.credit_balance)}</span></div><div><button class="ghost compact-button" data-admin-credit="${u.user_id}">Credit</button><button class="ghost compact-button" data-admin-toggle-user="${u.user_id}" data-current-status="${u.status}">${u.status==='active'?'Disable':'Enable'}</button></div></article>`).join('')}</div>`;}
function renderAdminGroups(){$('#admin-groups').innerHTML=`<form id="admin-group-create" class="inline-admin-form"><input id="admin-group-name" required placeholder="New group"><button class="primary">Create</button></form><div class="admin-list">${state.admin.groups.map(g=>`<article class="admin-row"><div><strong>${escapeHtml(g.name)}</strong><small>${escapeHtml(g.group_id)}</small></div><div class="chip-row"><span class="chip">${g.member_count||0} members</span></div><div><button class="ghost compact-button" data-group-add-member="${g.group_id}">Add member</button><button class="ghost compact-button" data-group-credit="${g.group_id}">Grant credit</button><button class="ghost danger-text compact-button" data-group-delete="${g.group_id}">Delete</button></div></article>`).join('')}</div>`;$('#admin-group-create')?.addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/admin/groups',{method:'POST',body:JSON.stringify({name:$('#admin-group-name').value})});await refreshAdmin();}catch(error){toast(error.message,true);}});}
function renderAdminDomains(){$('#admin-domains').innerHTML=`<form id="admin-domain-create" class="inline-admin-form"><input id="admin-domain-name" required placeholder="example.edu"><button class="primary">Add</button></form><div class="admin-list">${state.admin.domains.map(d=>`<article class="admin-row"><div><strong>${escapeHtml(d.domain)}</strong><small>${d.enabled?'enabled':'disabled'}</small></div><div></div><div><button class="ghost compact-button" data-domain-toggle="${d.domain_id}" data-domain-name="${escapeHtml(d.domain)}" data-domain-enabled="${d.enabled}">${d.enabled?'Disable':'Enable'}</button><button class="ghost danger-text compact-button" data-domain-delete="${d.domain_id}">Delete</button></div></article>`).join('')}</div>`;$('#admin-domain-create')?.addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/admin/domains',{method:'POST',body:JSON.stringify({domain:$('#admin-domain-name').value,enabled:true})});await refreshAdmin();}catch(error){toast(error.message,true);}});}
function renderAdminLedger(){$('#admin-ledger').innerHTML=`<div class="admin-list">${state.admin.ledger.slice(0,200).map(x=>`<article class="admin-row"><div><strong>${x.amount>=0?'+':''}${money(x.amount)}</strong><small>${escapeHtml(x.user_id)} · ${escapeHtml(x.reason)}</small></div><div></div><time>${escapeHtml(new Date(x.created_at).toLocaleString())}</time></article>`).join('')||'<p class="muted">No credit events.</p>'}</div>`;}

// Delegated UI actions
document.addEventListener('click',async event=>{
  const close=event.target.closest('[data-close]');if(close){const dialog=close.closest('dialog');if(dialog?.id==='section-dialog')await saveCurrentSection({silent:true});if(dialog?.id==='new-contract-dialog'&&state.request.conversationId)await saveRequestDraft({silent:true});dialog?.close();return;}
  const newContract=event.target.closest('[data-action="new-contract"],#new-contract');if(newContract){await openNewContract();return;}
  const projectMenu=event.target.closest('[data-project-menu]');if(projectMenu){event.preventDefault();event.stopPropagation();openProjectManager(projectMenu.dataset.projectMenu);return;}
  const chat=event.target.closest('[data-conversation-id]');if(chat){await openConversation(chat.dataset.conversationId);return;}
  const download=event.target.closest('[data-download-file]');if(download){try{await downloadAuthenticated(`/api/files/${download.dataset.downloadFile}/download`,download.dataset.fileName||'source-file');}catch(error){toast(error.message,true);}return;}
  const del=event.target.closest('[data-delete-file]');if(del){confirmAction('删除 Source File','历史 manifest 会显示文件缺失；已执行结果不受影响。',async()=>{try{await api(`/api/files/${del.dataset.deleteFile}`,{method:'DELETE'});await refreshBootstrap();}catch(error){toast(error.message,true);}},'删除文件');return;}
  const sw=event.target.closest('[data-user-id]');if(sw){if(state.request.conversationId)await saveRequestDraft({silent:true});await api('/api/dev/switch-user',{method:'POST',body:JSON.stringify({user_id:sw.dataset.userId})});clearIdentityScopedDom();closeDialog('#user-dialog');await refreshBootstrap({keepConversation:false});return;}
  const tab=event.target.closest('[data-admin-tab]');if(tab){$$('.admin-tabs button').forEach(b=>b.classList.toggle('active',b===tab));$$('.admin-panel').forEach(p=>p.classList.add('hidden'));$(`#admin-${tab.dataset.adminTab}`).classList.remove('hidden');return;}
  const credit=event.target.closest('[data-admin-credit]');if(credit){const amount=Number(prompt('Credit amount','20'));if(!Number.isFinite(amount))return;try{await api(`/api/admin/users/${credit.dataset.adminCredit}/credit`,{method:'POST',body:JSON.stringify({amount,reason:'admin grant'})});await refreshAdmin();await refreshBootstrap();}catch(error){toast(error.message,true);}return;}
  const toggle=event.target.closest('[data-admin-toggle-user]');if(toggle){try{await api(`/api/admin/users/${toggle.dataset.adminToggleUser}`,{method:'PATCH',body:JSON.stringify({status:toggle.dataset.currentStatus==='active'?'disabled':'active'})});await refreshAdmin();}catch(error){toast(error.message,true);}return;}
  const addMember=event.target.closest('[data-group-add-member]');if(addMember){const userId=prompt('User ID');if(!userId)return;try{await api(`/api/admin/groups/${addMember.dataset.groupAddMember}/members`,{method:'POST',body:JSON.stringify({user_id:userId})});await refreshAdmin();}catch(error){toast(error.message,true);}return;}
  const groupCredit=event.target.closest('[data-group-credit]');if(groupCredit){const amount=Number(prompt('Credit per member','20'));if(!Number.isFinite(amount))return;try{await api(`/api/admin/groups/${groupCredit.dataset.groupCredit}/credit`,{method:'POST',body:JSON.stringify({amount,reason:'group grant'})});await refreshAdmin();await refreshBootstrap();}catch(error){toast(error.message,true);}return;}
  const groupDelete=event.target.closest('[data-group-delete]');if(groupDelete&&confirm('Delete group?')){try{await api(`/api/admin/groups/${groupDelete.dataset.groupDelete}`,{method:'DELETE'});await refreshAdmin();}catch(error){toast(error.message,true);}return;}
  const domainToggle=event.target.closest('[data-domain-toggle]');if(domainToggle){try{await api('/api/admin/domains',{method:'POST',body:JSON.stringify({domain:domainToggle.dataset.domainName,enabled:domainToggle.dataset.domainEnabled!=='true'})});await refreshAdmin();}catch(error){toast(error.message,true);}return;}
  const domainDelete=event.target.closest('[data-domain-delete]');if(domainDelete&&confirm('Delete allowed domain?')){try{await api(`/api/admin/domains/${domainDelete.dataset.domainDelete}`,{method:'DELETE'});await refreshAdmin();}catch(error){toast(error.message,true);}}
});
document.addEventListener('dblclick',event=>{const s=event.target.closest('.contract-section[data-editable="true"]');if(s)openSectionEditor(s.dataset.sectionKey);});

// Direct wiring
$('#new-contract').onclick=openNewContract;
$('#new-contract-form').addEventListener('submit',generateFromRequest);
$('#save-request-draft').onclick=()=>saveRequestDraft({silent:false});
for(const id of ['request-problem','request-background','request-methods','request-partial','request-ideas','request-profile','request-project']){$(`#${id}`).addEventListener('input',scheduleRequestAutosave);$(`#${id}`).addEventListener('change',scheduleRequestAutosave);}
$('#request-file-upload').onclick=()=>uploadFile($('#request-file-input'),{selectInRequest:true});
$('#new-project').onclick=createNewProject;
$('#help-button').onclick=()=>openDialog('#help-dialog');
$('#files-button').onclick=()=>openDialog('#files-dialog');
$('#user-card').onclick=()=>openDialog('#user-dialog');
$('#contract-sources').onclick=()=>{renderConversationSourcePicker();openDialog('#sources-dialog');};
$('#version-select').onchange=async e=>{state.selected=e.target.value==='draft'?{kind:'draft',version:null}:{kind:'version',version:Number(e.target.value.split(':')[1])};await renderConversation();};
$('#save-contract-version').onclick=commitDraft;
$('#discard-working-draft').onclick=discardDraft;
$('#clone-contract').onclick=cloneContract;
$('#conversation-menu').onclick=openConversationManager;
$('#approve-contract').onclick=()=>approveContract(false);
$('#full-regenerate').onclick=fullRegenerate;
$('#contract-chat-form').addEventListener('submit',sendContractChat);
$('#execution-model-tier').onchange=patchExecutionPolicy;
$('#execution-duration-tier').onchange=patchExecutionPolicy;
$('#section-editor').addEventListener('input',()=>{scheduleSectionPreview();scheduleSectionAutosave();});
$('#section-editor').addEventListener('blur',()=>saveCurrentSection({silent:true}));
$('#section-dialog').addEventListener('cancel',event=>{event.preventDefault();saveCurrentSection({silent:true}).finally(()=>closeDialog('#section-dialog'));});
$('#file-upload-form').addEventListener('submit',e=>{e.preventDefault();uploadFile($('#file-input'));});
$('#save-conversation-sources').onclick=async()=>{const ids=$$('#conversation-source-picker input:checked').map(i=>i.value);try{await api(`/api/conversations/${state.conversation.conversation_id}/sources`,{method:'PUT',body:JSON.stringify({file_ids:ids})});closeDialog('#sources-dialog');await reloadConversation();toast('Source Materials 已更新');}catch(error){toast(error.message,true);}};
$('#preferences-form').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/users/me/preferences',{method:'PATCH',body:JSON.stringify({display_name:$('#pref-name').value,default_model_strategy:$('#pref-model').value,default_budget_preset:$('#pref-budget').value})});await refreshBootstrap();toast('Preferences 已保存');}catch(error){toast(error.message,true);}});
$('#confirm-cancel').onclick=()=>closeDialog('#confirm-dialog');
$('#confirm-accept').onclick=async()=>{const cb=state.confirmCallback;state.confirmCallback=null;closeDialog('#confirm-dialog');if(cb)await cb();};
$('#save-project').onclick=saveManagedProject;$('#delete-project').onclick=deleteManagedProject;$('#save-conversation').onclick=saveManagedConversation;$('#delete-conversation').onclick=deleteManagedConversation;
$('#download-report').onclick=async()=>{if(!state.run?.final_report_path)return;try{await downloadAuthenticated(`/api/runs/${state.run.run_id}/final-report`,`${state.run.run_id}-research-report.md`);}catch(error){toast(error.message,true);}};
$('#otp-request-form').addEventListener('submit',requestOtp);$('#otp-verify-form').addEventListener('submit',verifyOtp);$('#otp-back').onclick=resetOtpForm;$('#logout-button').onclick=logout;$('#open-admin').onclick=openAdmin;
$('#close-generation-notice').onclick=()=>$('#generation-notice').classList.add('hidden');
$('#login-dialog').addEventListener('cancel',e=>{if(!state.bootstrap)e.preventDefault();});
$('#entity-form').addEventListener('submit',async e=>{e.preventDefault();const name=$('#entity-name').value.trim();if(!name)return;try{await api('/api/projects',{method:'POST',body:JSON.stringify({name})});closeDialog('#entity-dialog');await refreshBootstrap();}catch(error){toast(error.message,true);}});

setupScrollSync();
setupDragDivider($('#section-splitter'),ratio=>{$('#section-split').style.gridTemplateColumns=`minmax(260px,${ratio}fr) 6px minmax(260px,${1-ratio}fr)`;});
setupDragDivider($('#contract-divider'),ratio=>{const clamped=Math.min(.75,Math.max(.45,ratio));document.documentElement.style.setProperty('--chat-width',`${Math.round((1-clamped)*100)}%`);clearTimeout(state._panelSaveTimer);state._panelSaveTimer=setTimeout(async()=>{try{await api('/api/users/me/preferences',{method:'PATCH',body:JSON.stringify({contract_chat_panel_width:1-clamped})});}catch(_){}},500);});

window.addEventListener('DOMContentLoaded',async()=>{try{await refreshBootstrap({keepConversation:false});}catch(error){toast(`MathLab 初始化失败：${error.message}`,true);}});
