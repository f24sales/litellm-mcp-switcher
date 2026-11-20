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
    throw Object.assign(new Error(typeof data.detail === 'string' ? data.detail : `Request rejected (${response.status})`), {status:response.status});
  }
  return data;
}
function base() { return identity.admin ? `/api/admin/users/${encodeURIComponent($('admin-user').value)}` : '/api'; }
function statePath() { return identity.admin ? base() : identity.orchestrator ? `/api/state?key_name=${encodeURIComponent($('target-key').value)}` : '/api/state'; }
function selectionChanged() {
  preserveAlwaysOn(draft.selection, latest?.always_on_mcps);
  if (identity.admin) {
    const selection = clone(draft.selection), revision = latest.revision;
    const path = base() + '/access';
    ++refreshId;
    draft.reset(latest, true);
    renderCards();
    return guarded(async () => {
      try {
        const state = await api(path, {method:'PUT', body:JSON.stringify({selection, revision})});
        latest = state; draft.reset(state, true); renderCards();
      } catch (error) {
        if (identity?.admin) await refresh(true);
        throw error;
      }
    });
  }
  draft.edit(draft.selection);
  updateActions(); renderCards();
}
function updateActions() {
  for (const id of ['select-all', 'clear-all', 'new-project', 'project']) $(id).disabled = busy || !latest;
  $('target-key').disabled = busy;
  $('admin-user').disabled = busy;
  $('rotate').disabled = busy || !identity?.admin || !latest ||
    latest.target_key !== $('admin-user').value;
  $('logout').disabled = busy;
  $('refresh-now').disabled = busy;
  $('save-config').hidden = !identity?.admin;
  $('save-config').disabled = busy || latest?.config_changed !== true || draft.dirty || draft.conflict ||
    ['pending', 'uncertain'].includes(latest?.operation.status);
  $('save-config').title = latest?.config_changed === false ? 'Already saved in permissions.json' :
    latest && latest.config_changed == null ? 'Cannot compare with permissions.json' : '';
  for (const control of document.querySelectorAll('.active-switch'))
    control.disabled = control.dataset.locked === 'true' || busy || !latest || draft.dirty || draft.conflict;
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
    const allowed = Object.hasOwn(latest.ceiling.servers, sid);
    const card = node('article', '', `server${selected ? ' selected' : ''}`);
    const head = node('div', '', 'server-head');
    const check = node('input', '', 'server-check'); check.type = 'checkbox'; check.checked = selected;
    check.checked = identity.orchestrator ? allowed : selected;
    check.disabled = pinned || identity.orchestrator;
    check.setAttribute('aria-label', `${identity.admin || identity.orchestrator ? 'Allowed' : 'Selected'}: ${server.alias || server.server_name}`);
    check.onchange = () => {
      if (check.checked) draft.selection.servers[sid] = null; else delete draft.selection.servers[sid];
      selectionChanged();
    };
    const heading = node('div', '', 'server-heading');
    const title = node('h2', server.server_name || server.alias, 'server-title');
    if (server.icon) {
      const icon = node('img'); icon.alt = ''; icon.width = icon.height = 16;
      icon.onerror = () => icon.remove(); icon.src = server.icon; title.prepend(icon);
    }
    if (identity.admin || identity.orchestrator) {
      const toggle = node('input', '', 'active-switch');
      toggle.type = 'checkbox'; toggle.setAttribute('role', 'switch');
      toggle.setAttribute('aria-label', `Active: ${server.alias || server.server_name}`);
      toggle.checked = Object.hasOwn(latest.active.servers, sid);
      toggle.dataset.locked = String(Boolean(pinned || (!identity.admin && !allowed) ||
        ['pending', 'uncertain'].includes(latest.operation.status)));
      toggle.disabled = busy || draft.dirty || draft.conflict || toggle.dataset.locked === 'true';
      toggle.onchange = () => {
        const enabled = toggle.checked, target = latest.target_key, revision = latest.revision;
        toggle.checked = !enabled;
        guarded(async () => {
          try {
            await api(`/api/targets/${encodeURIComponent(target)}/servers/${encodeURIComponent(sid)}/active`,
              {method:'PUT', body:JSON.stringify({enabled, revision})});
          } finally { await refresh(true); }
        });
      };
      title.append(toggle);
    }
    heading.append(title, node('div', server.alias, 'alias'));
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
    if (allowed || identity.admin) card.append(details);
    root.append(card);
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
async function guarded(fn) { if (busy) return; busy = true; $('servers').inert = true; updateActions(); hideMessage(); try { await fn(); } catch (e) { notify(e.message, true); } finally { busy = false; $('servers').inert = false; updateActions(); } }
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
    identity = await api('/api/me'); hideMessage(); $('who').textContent = identity.name; $('identity').hidden = false;
    $('login').hidden = true; $('workspace').hidden = false;
    $('target-bar').hidden = !identity.orchestrator;
    if (identity.orchestrator) $('target-key').replaceChildren(...identity.key_names.map(name => new Option(name, name)));
    $('admin-bar').hidden = !identity.admin; $('project-bar').hidden = identity.admin;
    $('workspace-label').textContent = identity.admin ? 'ADMINISTRATION' : identity.orchestrator ? 'ORCHESTRATOR' : 'YOUR WORKSPACE';
    $('mode-description').textContent = identity.admin ? 'Define the MCP servers and tools each user may choose from.' : 'Choose your tools. Save a project. Apply when you\'re ready.';
    $('apply').hidden = $('discard').hidden = identity.admin;
    $('apply-note').textContent = identity.admin ? 'Changes apply immediately. Save config keeps server grants across restarts.' : 'Applies to every client using your assigned LiteLLM key.';
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
$('admin-user').onchange = () => { catalog.clear(); expanded.clear(); latest = null; draft.dirty = false; draft.epoch++; $('servers').replaceChildren(); updateActions(); refresh(true); };
$('search').oninput = renderCards;
$('refresh').onchange = setTimer;
$('refresh-now').onclick = () => refresh();
$('select-all').onclick = () => { for (const server of latest.servers) if (identity.admin || Object.hasOwn(latest.ceiling.servers, server.server_id)) draft.selection.servers[server.server_id] = null; selectionChanged(); };
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
  const data = await api('/api/apply', {method:'POST', body:JSON.stringify({selection:draft.selection, revision:draft.revision})});
  latest = data; draft.reset(data, identity.admin); renderCards(); updateActions(); notify('Access verified in LiteLLM.');
  if (identity.orchestrator) await refresh(true);
});
window.addEventListener('beforeunload', event => { if (draft.dirty) { event.preventDefault(); event.returnValue = ''; } });
$('rotate').onclick = () => guarded(async () => {
  const name = latest.target_key, path = base() + '/rotation';
  if (!confirm(`Do you really want to rotate the virtual bearer for ${name}?\n\nOnce the replacement is verified, the old bearer will stop working. Update external clients and Citadel mappings afterwards.`)) return;
  const storageKey = 'switcher.rotation.' + name;
  let request = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
  if (!request) {
    const preview = await api(path + '/preview', {method:'POST'});
    request = {revision:preview.revision, operation_id:preview.operation_id};
    sessionStorage.setItem(storageKey, JSON.stringify(request));
  }
  try {
    const result = await api(path, {method:'POST', body:JSON.stringify(request)});
    if (['verified', 'conflict'].includes(result.status)) sessionStorage.removeItem(storageKey);
    notify(result.message, result.status !== 'verified');
  } catch (error) {
    if (error.status === 409 && /fresh rotation preview/.test(error.message)) sessionStorage.removeItem(storageKey);
    throw error;
  }
});
$('save-config').onclick = () => guarded(async () => {
  const {target_key, revision} = latest;
  await api(`${base()}/permissions`, {method:'PUT', body:JSON.stringify({revision})});
  if (latest?.target_key !== target_key) return;
  refreshId++;
  if (latest.revision === revision) latest.config_changed = false;
  notify(`Saved permissions for ${target_key}.`);
});
