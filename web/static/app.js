/* Foxzilla, hosted — browser side.
 *
 * The one idea worth knowing: files are hashed here, in the browser, before
 * anything is sent. That is what lets the destination check happen without
 * uploading a file only to discover it was already there.
 */
'use strict';

const $ = (s) => document.querySelector(s);
const state = { connected: false, path: '/', remote: [], local: [], selRemote: new Set() };

const human = (n) => {
  if (!n) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + u[i];
};

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: 'same-origin', ...opts });
  const ct = r.headers.get('content-type') || '';
  if (!ct.includes('json')) {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r;
  }
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

/* ---------------- connect ---------------- */

const connectDlg = $('#connectDlg');
$('#connectBtn').onclick = () => { $('#connectErr').hidden = true; connectDlg.showModal(); };

$('#cType').onchange = () => {
  const dav = $('#cType').value === 'dav';
  $('#urlField').hidden = !dav;
  $('#hostFields').hidden = dav;
  $('#cPort').placeholder = { sftp: '22', ftp: '21', ftps: '21' }[$('#cType').value] || '';
};

$('#connectForm').addEventListener('submit', async (e) => {
  if (e.submitter && e.submitter.value === 'cancel') return;
  e.preventDefault();
  const type = $('#cType').value;
  const cfg = { type, user: $('#cUser').value, password: $('#cPass').value };
  if (type === 'dav') cfg.url = $('#cUrl').value;
  else { cfg.host = $('#cHost').value; if ($('#cPort').value) cfg.port = +$('#cPort').value; }

  const go = $('#connectGo');
  go.disabled = true; go.textContent = 'Connecting…';
  try {
    const res = await api('/api/connect', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    });
    $('#cPass').value = '';                       // don't leave it in the DOM
    state.connected = true;
    $('#status').textContent = res.label;
    $('#status').classList.add('on');
    $('#connectBtn').hidden = true;
    $('#disconnectBtn').hidden = false;
    connectDlg.close();
    await browse(res.home || '/');
  } catch (err) {
    $('#connectErr').textContent = String(err.message || err);
    $('#connectErr').hidden = false;
  } finally {
    go.disabled = false; go.textContent = 'Connect';
  }
});

$('#disconnectBtn').onclick = async () => {
  await api('/api/disconnect', { method: 'POST' }).catch(() => {});
  state.connected = false; state.remote = []; state.selRemote.clear();
  $('#status').textContent = 'not connected';
  $('#status').classList.remove('on');
  $('#connectBtn').hidden = false;
  $('#disconnectBtn').hidden = true;
  $('#remotePath').textContent = 'not connected';
  renderRemote();
  updateButtons();
};

/* ---------------- remote pane ---------------- */

async function browse(path) {
  try {
    const res = await api('/api/list?path=' + encodeURIComponent(path));
    state.path = res.path;
    state.remote = res.entries;
    state.parent = res.parent;
    state.selRemote.clear();
    $('#remotePath').textContent = res.path;
    renderRemote();
  } catch (err) {
    $('#remoteList').innerHTML = '';
    const p = document.createElement('p');
    p.className = 'err';
    p.textContent = String(err.message || err);
    $('#remoteList').append(p);
  }
  updateButtons();
}

function renderRemote() {
  const box = $('#remoteList');
  box.innerHTML = '';
  if (!state.connected) { box.innerHTML = '<p class="empty">Connect to a server to browse it.</p>'; return; }
  if (!state.remote.length) { box.innerHTML = '<p class="empty">This folder is empty.</p>'; return; }
  const sorted = [...state.remote].sort((a, b) =>
    (a.dir === b.dir) ? a.name.localeCompare(b.name) : (a.dir ? -1 : 1));
  for (const e of sorted) {
    const row = document.createElement('div');
    row.className = 'row' + (e.dir ? ' dir' : '') + (state.selRemote.has(e.name) ? ' sel' : '');
    row.innerHTML = `<span class="glyph">${e.dir ? '▸' : '·'}</span>
      <span class="name"></span><span class="size">${e.dir ? '' : human(e.size)}</span>`;
    row.querySelector('.name').textContent = e.name;
    row.onclick = () => {
      if (state.selRemote.has(e.name)) state.selRemote.delete(e.name);
      else state.selRemote.add(e.name);
      renderRemote(); updateButtons();
    };
    row.ondblclick = () => { if (e.dir) browse(join(state.path, e.name)); };
    box.append(row);
  }
}

const join = (a, b) => (a.endsWith('/') ? a + b : a + '/' + b);

$('#upBtn').onclick = () => state.connected && browse(state.parent || '/');
$('#refreshBtn').onclick = () => state.connected && browse(state.path);

$('#mkdirBtn').onclick = async () => {
  const name = await prompt2('New folder', 'Name');
  if (!name) return;
  try {
    await api('/api/mkdir', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: state.path, name }),
    });
    browse(state.path);
  } catch (e) { alert(e.message); }
};

$('#delBtn').onclick = async () => {
  const names = [...state.selRemote];
  if (!names.length) return;
  if (!confirm(`Delete ${names.length} item(s) from the server?`)) return;
  for (const n of names) {
    const e = state.remote.find((x) => x.name === n);
    try {
      await api('/api/delete', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: join(state.path, n), dir: !!(e && e.dir) }),
      });
    } catch (err) { alert(`${n}: ${err.message}`); }
  }
  browse(state.path);
};

/* ---------------- local pane ---------------- */

const picker = document.createElement('input');
picker.type = 'file';
picker.multiple = true;
picker.onchange = () => addLocal([...picker.files]);
$('#pickBtn').onclick = () => picker.click();
$('#clearLocalBtn').onclick = () => { state.local = []; renderLocal(); updateButtons(); };

function addLocal(files) {
  for (const f of files) if (!state.local.some((x) => x.name === f.name && x.size === f.size)) state.local.push(f);
  renderLocal(); updateButtons();
}

function renderLocal() {
  const box = $('#localList');
  box.innerHTML = '';
  if (!state.local.length) { box.innerHTML = '<p class="empty">Nothing selected yet.</p>'; return; }
  $('#localPath').textContent = `${state.local.length} file(s), ${human(state.local.reduce((a, f) => a + f.size, 0))}`;
  for (const f of state.local) {
    const row = document.createElement('div');
    row.className = 'row';
    row.innerHTML = `<span class="glyph">·</span><span class="name"></span>
      <span class="size">${human(f.size)}</span>`;
    row.querySelector('.name').textContent = f.name;
    box.append(row);
  }
}

const pane = $('#localPane');
['dragenter', 'dragover'].forEach((ev) => pane.addEventListener(ev, (e) => {
  e.preventDefault(); pane.classList.add('drop');
}));
['dragleave', 'drop'].forEach((ev) => pane.addEventListener(ev, (e) => {
  e.preventDefault(); pane.classList.remove('drop');
}));
pane.addEventListener('drop', (e) => { if (e.dataTransfer?.files) addLocal([...e.dataTransfer.files]); });

/* ---------------- hashing, in the browser ---------------- */

async function sha256(file) {
  // Files can be large; hash in chunks so we never hold the whole thing twice.
  if (!crypto.subtle) return '';
  try {
    const buf = await file.arrayBuffer();
    const d = await crypto.subtle.digest('SHA-256', buf);
    return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, '0')).join('');
  } catch (e) { return ''; }
}

/* ---------------- transfer ---------------- */

function updateButtons() {
  $('#sendBtn').disabled = !(state.connected && state.local.length);
  $('#fetchBtn').disabled = !(state.connected && state.selRemote.size);
  $('#mkdirBtn').disabled = $('#delBtn').disabled = !state.connected;
}

$('#sendBtn').onclick = async () => {
  if (!state.local.length) return;
  let plan = null;
  if ($('#scanFirst').checked) {
    setStatus('checking the destination…');
    const files = [];
    for (const f of state.local) files.push({ name: f.name, size: f.size, sha256: await sha256(f) });
    try {
      plan = await api('/api/scan', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: state.path, files }),
      });
    } catch (e) { alert(e.message); setStatus(''); return; }
    setStatus('');
    const already = (plan.counts.identical || 0) + (plan.counts['same size'] || 0) + (plan.counts.conflict || 0);
    if (already) {
      const mode = await askScan(plan);
      if (!mode) return;
      plan.mode = mode;
    } else { plan.mode = 'all'; }
  }
  const wanted = { skip: ['clear'], replace: ['clear', 'conflict'], all: null };
  const allow = plan ? wanted[plan.mode] : null;
  const verdicts = plan ? Object.fromEntries(plan.items.map((i) => [i.name, i.verdict])) : {};

  const jobs = state.local.filter((f) => !allow || allow.includes(verdicts[f.name] ?? 'clear'));
  const skipped = state.local.length - jobs.length;
  if (!jobs.length) { alert(`Nothing to send — all ${skipped} file(s) are already there.`); return; }
  await runQueue(jobs, skipped);
};

$('#fetchBtn').onclick = async () => {
  for (const name of state.selRemote) {
    const e = state.remote.find((x) => x.name === name);
    if (e && e.dir) continue;                     // folders are not fetched in one go
    const url = '/api/download?path=' + encodeURIComponent(join(state.path, name));
    const a = document.createElement('a');
    a.href = url; a.download = name;
    document.body.append(a); a.click(); a.remove();
  }
};

function setStatus(msg) {
  $('#status').textContent = msg || (state.connected ? ($('#status').dataset.label || 'connected') : 'not connected');
}

async function runQueue(files, skipped) {
  const wrap = $('#queueWrap'), q = $('#queue');
  wrap.hidden = false;
  const rows = new Map();
  for (const f of files) {
    const row = document.createElement('div');
    row.className = 'job';
    row.innerHTML = `<span class="jname"></span>
      <span class="barwrap"><span class="fill"></span></span>
      <span class="state">queued</span>`;
    row.querySelector('.jname').textContent = f.name;
    q.prepend(row); rows.set(f, row);
  }
  if (skipped) {
    const note = document.createElement('div');
    note.className = 'job skipped';
    note.innerHTML = `<span class="jname">${skipped} file(s) already there</span><span class="state">skipped</span>`;
    q.prepend(note);
  }

  for (const f of files) {
    const row = rows.get(f);
    row.querySelector('.state').textContent = 'sending';
    try {
      await upload(f, (pct) => { row.querySelector('.fill').style.width = pct + '%'; });
      row.classList.add('done');
      row.querySelector('.state').textContent = 'done';
      row.querySelector('.fill').style.width = '100%';
    } catch (err) {
      row.classList.add('failed');
      row.querySelector('.state').textContent = String(err.message || err).slice(0, 60);
    }
  }
  browse(state.path);
}

function upload(file, onProgress) {
  // XHR rather than fetch: it is the only way to get upload progress.
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const url = `/api/upload?path=${encodeURIComponent(state.path)}&name=${encodeURIComponent(file.name)}`;
    xhr.open('POST', url);
    xhr.withCredentials = true;
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(Math.round(e.loaded / e.total * 100)); };
    xhr.onload = () => {
      let msg = `HTTP ${xhr.status}`;
      try { const d = JSON.parse(xhr.responseText); if (d.error) msg = d.error; if (d.ok) return resolve(d); } catch (e) {}
      reject(new Error(msg));
    };
    xhr.onerror = () => reject(new Error('network error'));
    xhr.send(file);
  });
}

/* ---------------- dialogs ---------------- */

function askScan(plan) {
  return new Promise((resolve) => {
    const dlg = $('#scanDlg');
    const c = plan.counts;
    const bits = [];
    if (c.identical) bits.push(`${c.identical} identical (verified by hash)`);
    if (c['same size']) bits.push(`${c['same size']} the same size — the server could not hash them, so this is not certain`);
    if (c.conflict) bits.push(`${c.conflict} same name, different content`);
    if (c.clear) bits.push(`${c.clear} new`);
    $('#scanSub').textContent = bits.join(' · ');
    const list = $('#scanList');
    list.innerHTML = '';
    const label = { clear: 'new', identical: 'identical — skip',
                    'same size': 'same size, unverified', conflict: 'differs — would overwrite' };
    for (const it of [...plan.items].sort((a, b) => (a.verdict === 'clear') - (b.verdict === 'clear'))) {
      const row = document.createElement('div');
      row.className = 'row';
      row.innerHTML = `<span class="name"></span><span class="size">${human(it.size)}</span>
        <span class="v ${it.verdict.replace(' ', '-')}">${label[it.verdict] || it.verdict}</span>`;
      row.querySelector('.name').textContent = it.name;
      list.append(row);
    }
    const done = (val) => { dlg.close(); $('#scanGo').onclick = null; $('#scanCancel').onclick = null; resolve(val); };
    $('#scanGo').onclick = () => done(document.querySelector('input[name=mode]:checked').value);
    $('#scanCancel').onclick = () => done(null);
    dlg.showModal();
  });
}

function prompt2(title, label) {
  return new Promise((resolve) => {
    const dlg = $('#promptDlg');
    $('#promptTitle').textContent = title;
    $('#promptLabel').textContent = label;
    $('#promptInput').value = '';
    dlg.onclose = () => resolve(dlg.returnValue === 'ok' ? $('#promptInput').value.trim() : null);
    dlg.showModal();
  });
}

/* ---------------- boot ---------------- */

(async () => {
  try {
    const s = await api('/api/session');
    if (s.connected) {
      state.connected = true;
      $('#status').textContent = s.label;
      $('#status').classList.add('on');
      $('#connectBtn').hidden = true;
      $('#disconnectBtn').hidden = false;
      await browse('/');
    }
  } catch (e) { /* not connected; the page is still usable */ }
  renderLocal();
  updateButtons();
  $('#cType').onchange();
})();
