import { Draft, preserveAlwaysOn } from './state.js';
const $ = id => document.getElementById(id);
const draft = new Draft();
let token = '', identity, latest, projectId = '', timer, busy = false, refreshId = 0;
const catalog = new Map();
const expanded = new Set();
const clone = value => structuredClone(value);
function notify(text, error = false) { $('message').textContent = text; $('message').classList.toggle('error', error); $('message').hidden = false; }
function hideMessage() { $('message').hidden = true; }
function node(tag, text, cls) { const el = document.createElement(tag); if (text) el.textContent = text; if (cls) el.className = cls; return el; }
async function api(path, options = {}) {
  if (identity?.orchestrator && path.startsWith('/api/') && path !== '/api/me' && !path.includes('key_name=')) {
    path += `${path.includes('?') ? '&' : '?'}key_name=${encodeURIComponent($('target-key').value)}`;
  }
  const response = await fetch(path, { ...options, headers: {'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json', ...(options.headers || {})}, cache: 'no-store' });
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) logout();
    throw new Error(typeof data.detail === 'string' ? data.detail : `Request rejected (${response.status})`);
  }
  return data;
}
function base() { return identity.admin ? `/api/admin/users/${encodeURIComponent($('admin-user').value)}` : '/api'; }
function statePath() { return identity.admin ? base() : identity.orchestrator ? `/api/state?key_name=${encodeURIComponent($('target-key').value)}` : '/api/state'; }
function selectionChanged() {
  preserveAlwaysOn(draft.selection, latest?.always_on_mcps);
  draft.edit(draft.selection);
  updateActions(); renderCards();
}
function updateActions() {
  for (const id of ['select-all', 'clear-all', 'new-project', 'project']) $(id).disabled = busy || !latest;
  $('target-key').disabled = busy;
  $('conflict').hidden = !draft.conflict;
  $('draft-state').textContent = draft.dirty ? 'Unsaved selection' : 'In sync';
  $('selection-count').textContent = `${Object.keys(draft.selection.servers).length} selected`;
  const pending = latest && ['pending', 'uncertain'].includes(latest.operation.status);
  $('apply').disabled = busy || !latest || !draft.dirty || draft.conflict || pending;
  $('discard').disabled = busy || (!draft.dirty && !draft.conflict);
  $('save-project').disabled = busy || !latest;
  $('delete-project').disabled = busy || !projectId;
  $('rename-project').disabled = busy || !projectId;
}
function rebuildProjects() {
  const select = $('project'); select.replaceChildren(new Option('Current LiteLLM selection', ''));
  for (const p of latest.projects) select.add(new Option(p.name, p.id));
  if (!latest.projects.some(p => p.id === projectId)) projectId = '';
  select.value = projectId;
}
async function refresh(force = false) {
  if (!identity || (identity.admin && !$('admin-user').value)) return;
  const epoch = draft.epoch, id = ++refreshId, path = statePath();
  try {
    const state = await api(path);
    if (id !== refreshId || path !== statePath()) return;
    latest = state;
    if (identity.orchestrator) $('apply-note').textContent = `Applies to every client using LiteLLM key ${state.target_key}.`;
    if (force) draft.reset(state, identity.admin); else draft.accept(state, epoch, identity.admin);
    preserveAlwaysOn(draft.selection, state.always_on_mcps);
    rebuildProjects(); renderCards(); updateActions();
    $('sync-state').textContent = `Synced ${new Date().toLocaleTimeString()}`;
    $('operation').hidden = !['pending', 'uncertain', 'conflict'].includes(state.operation.status);
    $('operation').textContent = state.operation.message || '';
  } catch (e) { $('sync-state').textContent = 'Refresh failed · your selection is preserved'; notify(e.message, true); }
}
function renderCards() {
  if (!latest) return;
  const root = $('servers'); root.replaceChildren();
  const search = $('search').value.toLowerCase();
  $('empty').hidden = latest.servers.length > 0;
  for (const server of latest.servers) {
    const sid = server.server_id;
    const pinned = latest.always_on_mcps?.includes(sid);
    if (!`${server.alias} ${server.server_name}`.toLowerCase().includes(search)) continue;
    const selected = Object.hasOwn(draft.selection.servers, sid);
    const card = node('article', '', `server${selected ? ' selected' : ''}`);
    const head = node('div', '', 'server-head');
    const check = node('input', '', 'server-check'); check.type = 'checkbox'; check.checked = selected;
    check.disabled = pinned;
    check.setAttribute('aria-label', `Select ${server.alias || server.server_name}`);
    check.onchange = () => {
      if (check.checked) draft.selection.servers[sid] = null; else delete draft.selection.servers[sid];
      selectionChanged();
    };
    const heading = node('div', '', 'server-heading');
    heading.append(node('h2', server.server_name || server.alias, 'server-title'), node('div', server.alias, 'alias'));
    if (pinned) heading.append(node('div', 'Always enabled', 'status reachable'));
    const entry = catalog.get(sid);
    head.append(check, heading, node('span', entry?.status || 'unknown', `status ${entry?.status || ''}`));
    card.append(head);
    const details = node('details'); details.open = expanded.has(sid);
    const chosen = draft.selection.servers[sid];
    const summary = node('summary', selected ? (chosen === null ? 'All permitted tools' : `${chosen.length} tools selected`) : 'Inspect tools');
    const body = node('div', '', 'tools');
    details.append(summary, body);
    if (details.open) renderTools(sid, body);
    details.ontoggle = async () => {
      if (!details.open) { expanded.delete(sid); return; }
      expanded.add(sid);
      if (!catalog.has(sid)) {
        body.replaceChildren(node('p', 'Discovering tools…'));
        const target = latest.target_key;
        try { const result = await api(`${base()}/servers/${encodeURIComponent(sid)}/tools`);
          if (latest?.target_key !== target) return;
          catalog.set(sid, result); }
        catch (e) { if (latest?.target_key !== target) return; catalog.set(sid, {status:'error', tools:[], message:e.message}); }
        if (details.isConnected && details.open) renderCards();
      }
    };
    card.append(details); root.append(card);
  }
}
function renderTools(sid, body) {
  const entry = catalog.get(sid); body.replaceChildren();
  if (!entry) { body.append(node('p', 'Discovering tools…')); return; }
  if (entry.status !== 'reachable') {
    body.append(node('p', entry.message || 'Tool discovery failed. Saved access is preserved.'));
    const retry = node('button', 'Retry discovery'); retry.onclick = async () => {
      const target = latest.target_key;
      try { const result = await api(`${base()}/servers/${encodeURIComponent(sid)}/tools?refresh=true`);
        if (latest?.target_key !== target) return; catalog.set(sid, result); renderCards(); }
      catch (e) { notify(e.message, true); }
    }; body.append(retry); return;
  }
  const selected = Object.hasOwn(draft.selection.servers, sid);
  const pinned = latest.always_on_mcps?.includes(sid);
  const chosen = draft.selection.servers[sid];
  const actions = node('div', '', 'tool-actions');
  const all = node('button', 'All permitted tools'); all.onclick = () => { draft.selection.servers[sid] = null; selectionChanged(); };
  const none = node('button', 'Disable server'); none.onclick = () => { delete draft.selection.servers[sid]; selectionChanged(); };
  all.disabled = none.disabled = pinned;
  actions.append(all, none); body.append(actions);
  const list = node('div', '', 'tool-list');
  for (const tool of entry.tools) {
    const row = node('label', '', 'tool-row'), check = node('input'); check.type = 'checkbox';
    check.checked = selected && (chosen === null || chosen.includes(tool.name));
    check.disabled = pinned;
    check.onchange = () => {
      const names = new Set(selected ? (chosen === null ? entry.tools.map(t => t.name) : chosen) : []);
      if (check.checked) names.add(tool.name); else names.delete(tool.name);
      if (names.size) draft.selection.servers[sid] = [...names].sort(); else delete draft.selection.servers[sid];
      selectionChanged();
    };
    const text = node('span', tool.name); text.append(node('small', tool.description)); row.append(check, text); list.append(row);
  }
  body.append(list);
  if (!entry.tools.length) body.append(node('p', 'Discovery succeeded; this server returned no tools.'));
}
async function guarded(fn) { if (busy) return; busy = true; updateActions(); hideMessage(); try { await fn(); } catch (e) { notify(e.message, true); } finally { busy = false; updateActions(); } }
function askName(title, value = '') {
  return new Promise(resolve => { const dialog = $('name-dialog'); $('dialog-title').textContent = title; $('project-name').value = value;
    dialog.returnValue = ''; dialog.addEventListener('close', () => resolve(dialog.returnValue === 'save' ? $('project-name').value.trim() : null), {once:true}); dialog.showModal(); });
}
async function saveProject(rename = false, create = false) {
  const project = latest.projects.find(p => p.id === projectId);
  let name = project && !create ? project.name : null;
  if (rename || !name) name = await askName(rename ? 'Rename project' : 'Name your project', name || '');
  if (!name) return;
  const current = create ? null : project;
  const result = await api(current ? `/api/projects/${current.id}` : '/api/projects', {
    method: current ? 'PUT' : 'POST', body: JSON.stringify({name, selection: clone(draft.selection), version: current?.version || null})});
  projectId = result.id;
  await refresh(); notify('Project saved. Use Apply to activate this selection.');
}
function setTimer() { clearInterval(timer); const seconds = Number($('refresh').value); if (seconds) timer = setInterval(() => { if (!busy && !document.hidden) refresh(); }, seconds * 1000); }
function logout() { clearInterval(timer); token = ''; identity = null; latest = null; refreshId++; catalog.clear(); expanded.clear(); projectId = ''; draft.dirty = false; draft.epoch++; $('workspace').hidden = true; $('identity').hidden = true; $('login').hidden = false; $('bearer').value = ''; }
$('login-form').onsubmit = event => { event.preventDefault(); token = $('bearer').value.trim(); $('bearer').value = ''; (async () => {
  try {
    identity = await api('/api/me'); $('who').textContent = identity.name; $('identity').hidden = false;
    $('login').hidden = true; $('workspace').hidden = false;
    $('target-bar').hidden = !identity.orchestrator;
    if (identity.orchestrator) $('target-key').replaceChildren(...identity.key_names.map(name => new Option(name, name)));
    $('admin-bar').hidden = !identity.admin; $('project-bar').hidden = identity.admin;
    $('workspace-label').textContent = identity.admin ? 'ADMINISTRATION' : identity.orchestrator ? 'ORCHESTRATOR' : 'YOUR WORKSPACE';
    $('mode-description').textContent = identity.admin ? 'Define the MCP servers and tools each user may choose from.' : 'Choose your tools. Save a project. Apply when you\'re ready.';
    $('apply').textContent = identity.admin ? 'Update access ↗' : 'Apply selection ↗';
    $('apply-note').textContent = identity.admin ? 'Revocations also limit existing projects and active selections.' : 'Applies to every client using your assigned LiteLLM key.';
    if (identity.admin) { const data = await api('/api/admin/users'); $('admin-user').replaceChildren(...data.users.map(name => new Option(name, name))); }
    const value = String(identity.refresh_seconds); if (![...$('refresh').options].some(o => o.value === value)) $('refresh').add(new Option(`${value} seconds`, value));
    $('refresh').value = value; await refresh(true); setTimer();
  } catch (e) { logout(); notify(e.message, true); }
})(); };
$('logout').onclick = logout;
$('target-key').onchange = () => {
  if (busy || (draft.dirty && !confirm('Discard this key’s unsaved selection and change target?'))) {
    $('target-key').value = latest?.target_key || identity.key_names[0]; return;
  }
  catalog.clear(); expanded.clear(); projectId = ''; latest = null;
  refreshId++; draft.dirty = false; draft.epoch++; $('servers').replaceChildren(); updateActions(); refresh(true);
};
$('admin-user').onchange = () => { catalog.clear(); expanded.clear(); draft.dirty = false; draft.epoch++; refresh(true); };
$('search').oninput = renderCards;
$('refresh').onchange = setTimer;
$('refresh-now').onclick = () => refresh();
$('select-all').onclick = () => { for (const server of latest.servers) draft.selection.servers[server.server_id] = null; selectionChanged(); };
$('clear-all').onclick = () => { draft.selection.servers = {}; selectionChanged(); };
$('discard').onclick = () => { projectId = ''; refresh(true); };
$('project').onchange = () => {
  projectId = $('project').value;
  const project = latest.projects.find(p => p.id === projectId);
  if (project) draft.edit(project.selection); else draft.reset(latest, identity.admin);
  preserveAlwaysOn(draft.selection, latest.always_on_mcps);
  renderCards(); updateActions();
};
$('new-project').onclick = () => guarded(() => saveProject(false, true));
$('save-project').onclick = () => guarded(() => saveProject());
$('rename-project').onclick = () => guarded(() => saveProject(true));
$('delete-project').onclick = () => guarded(async () => {
  const project = latest.projects.find(p => p.id === projectId);
  if (project && confirm(`Delete project “${project.name}”? Active permissions will stay as they are.`)) {
    await api(`/api/projects/${project.id}?version=${project.version}`, {method:'DELETE'}); projectId = ''; await refresh(); notify('Project deleted.');
  }
});
$('apply').onclick = () => guarded(async () => {
  const data = await api(identity.admin ? `${base()}/access` : '/api/apply', {method:identity.admin ? 'PUT' : 'POST', body:JSON.stringify({selection:draft.selection, revision:draft.revision})});
  latest = data; draft.reset(data, identity.admin); renderCards(); updateActions(); notify('Access verified in LiteLLM.');
});
window.addEventListener('beforeunload', event => { if (draft.dirty) { event.preventDefault(); event.returnValue = ''; } });
