/**
 * Shared "Suggested Tests" widget — same AI-recommended test list, progress
 * tracking and upload flow as report.html's own copy, but built to render
 * more than one instance on the same page (each keyed by session_id), for
 * health-history.html (one per past visit) and pre-visit-intake.html (one
 * per upcoming appointment). See SUGGESTED_TESTS_EXPANSION_PLAN.md.
 *
 * report.html keeps its own long-standing implementation untouched — this
 * file is additive, not a replacement for it.
 *
 * Additional tests (beyond the initial LLM pass) are checked for
 * automatically in the background after the first render — no button, no
 * user action; only a "New" badge appears on any cards it actually adds.
 * Pass { autoCheckMore: false } to opt out (used on the prep/intake page,
 * where regeneration doesn't make sense mid-checklist).
 *
 * Usage: SugTestsWidget.render(containerEl, sessionId, { autoCheckMore }).
 */
(function () {
  const API = window.API_BASE || '';
  const _instances = {}; // sessionId -> { tests, uploads, newIds }

  // Inject the card/progress-bar CSS + a spinner keyframe once. Pages that
  // already define these classes (report.html) get harmless duplicates;
  // pages that don't (health-history.html, pre-visit-intake.html) get them
  // for free. --bg-input isn't defined on every page's token set, so it
  // falls back to --surface-2.
  if (!document.getElementById('_sug-widget-styles')) {
    const style = document.createElement('style');
    style.id = '_sug-widget-styles';
    style.textContent = `
      .sug-loading { display:flex; align-items:center; gap:10px; padding:16px 0; font-size:13px; color:var(--text-muted); }
      .sug-loading .spinner { width:16px; height:16px; border:2px solid var(--border-card); border-top-color:var(--teal-primary); border-radius:50%; animation:_sug-spin .7s linear infinite; }
      @keyframes _sug-spin { to { transform:rotate(360deg); } }
      .sug-test-card { display:flex; align-items:flex-start; gap:14px; padding:16px 18px; border-radius:16px; border:1px solid var(--border-card); background:var(--bg-card); transition:border-color .2s; position:relative; overflow:hidden; margin-bottom:10px; }
      .sug-test-card:hover { border-color:var(--teal-primary); }
      .sug-icon { width:42px; height:42px; border-radius:12px; flex-shrink:0; display:flex; align-items:center; justify-content:center; }
      .sug-icon-blood { background:rgba(239,68,68,.12); } .sug-icon-urine { background:rgba(245,158,11,.12); }
      .sug-icon-imaging { background:rgba(99,102,241,.12); } .sug-icon-vitals { background:rgba(16,185,129,.12); } .sug-icon-other { background:rgba(100,116,139,.12); }
      .sug-test-name { font-family:var(--font-heading); font-size:14px; font-weight:700; color:var(--text-main); }
      .sug-test-name-done { text-decoration:line-through; opacity:.6; }
      .sug-test-why { font-size:12px; color:var(--text-muted); line-height:1.55; margin-top:3px; }
      .sug-checkbox { width:22px; height:22px; border-radius:7px; flex-shrink:0; border:1.5px solid var(--border-card); background:var(--bg-input, var(--surface-2)); display:flex; align-items:center; justify-content:center; cursor:pointer; transition:all .15s; }
      .sug-checkbox:hover { border-color:var(--teal-primary); }
      .sug-checkbox-done { background:var(--teal-primary); border-color:var(--teal-primary); color:#fff; }
      .sug-upload-btn { display:inline-flex; align-items:center; gap:5px; font-family:var(--font-heading); font-size:11px; font-weight:700; padding:5px 12px; border-radius:9999px; border:1px solid var(--border-card); background:var(--bg-card); color:var(--text-dim); cursor:pointer; transition:all .15s; }
      .sug-upload-btn:hover { border-color:var(--teal-primary); color:var(--teal-primary); }
      .sug-header { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:14px; }
      .sug-rationale { font-size:13px; color:var(--text-dim); margin-bottom:16px; padding:12px 16px; background:rgba(13,148,136,.05); border-left:3px solid var(--teal-primary); border-radius:0 12px 12px 0; line-height:1.6; }
      .sug-none { display:flex; align-items:center; gap:12px; padding:18px 20px; background:rgba(13,148,136,.06); border:1px solid rgba(13,148,136,.25); border-radius:16px; font-size:13.5px; color:var(--teal-primary); font-weight:600; }
      .sug-status-bar { display:flex; align-items:center; gap:14px; margin-bottom:16px; background:var(--surface-2); border:1px solid var(--border-card); border-radius:14px; padding:12px 16px; }
      .sug-status-count { font-family:var(--font-heading); font-size:13px; font-weight:800; color:var(--text-main); white-space:nowrap; }
      .sug-progress { flex:1; height:8px; background:var(--border-card); border-radius:9999px; overflow:hidden; }
      .sug-progress-fill { height:100%; border-radius:9999px; transition:width .35s cubic-bezier(.16,1,.3,1); background:linear-gradient(90deg,#059669,#10b981,#34d399); }
      .sug-badge { font-family:var(--font-heading); font-size:10px; font-weight:800; padding:3px 9px; border-radius:9999px; text-transform:uppercase; letter-spacing:.07em; }
      .sug-badge-routine { background:rgba(16,185,129,.12); color:#10b981; border:1px solid rgba(16,185,129,.3); }
      .sug-badge-urgent { background:rgba(245,158,11,.12); color:#f59e0b; border:1px solid rgba(245,158,11,.3); }
      .sug-meta-pill { display:inline-flex; align-items:center; gap:4px; font-size:11px; color:var(--text-dim); background:var(--bg-card); border:1px solid var(--border-card); border-radius:9999px; padding:3px 10px; }
      .sug-prep { margin-top:8px; font-size:11.5px; color:var(--text-dim); background:var(--bg-input, var(--surface-2)); border-radius:8px; padding:6px 12px; border-left:2px solid var(--border-card); }
      .sug-done-badge { flex-shrink:0; font-family:var(--font-heading); font-size:11px; font-weight:800; padding:4px 12px; border-radius:9999px; background:rgba(13,148,136,.15); color:var(--teal-primary); border:1px solid rgba(13,148,136,.3); }
      .sug-uploaded-badge { display:inline-flex; align-items:center; gap:5px; font-family:var(--font-heading); font-size:11px; font-weight:700; padding:4px 12px; border-radius:9999px; border:1px solid rgba(13,148,136,.3); background:rgba(13,148,136,.1); color:var(--teal-primary); white-space:nowrap; }
      .sug-new-badge { display:inline-block; font-family:var(--font-heading); font-size:9px; font-weight:800; letter-spacing:.05em; text-transform:uppercase; padding:2px 7px; border-radius:9999px; background:rgba(99,102,241,.15); color:#818cf8; border:1px solid rgba(99,102,241,.35); vertical-align:middle; margin-left:4px; }
    `;
    document.head.appendChild(style);
  }

  function _toast(msg, color) {
    if (typeof _showReportToast === 'function') { _showReportToast(msg, color); return; }
    if (typeof showToast === 'function') { showToast(msg, color); return; }
    let t = document.getElementById('_sug-widget-toast');
    if (!t) {
      t = document.createElement('div');
      t.id = '_sug-widget-toast';
      t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#1e2a38;border-radius:10px;padding:12px 20px;color:#f1f5f9;font-family:Inter,sans-serif;font-size:13px;z-index:99999;box-shadow:0 8px 32px rgba(0,0,0,.5);';
      document.body.appendChild(t);
    }
    t.style.background = color || '#1e2a38';
    t.textContent = msg;
    t.style.display = 'block';
    clearTimeout(t._hideTimer);
    t._hideTimer = setTimeout(() => { t.style.display = 'none'; }, 3500);
  }

  function _storageKey(sid) { return `sug_tests_${sid}`; }
  function _loadDone(sid) {
    try { return new Set(JSON.parse(localStorage.getItem(_storageKey(sid)) || '[]')); }
    catch { return new Set(); }
  }
  function _saveDone(sid, doneSet) {
    try { localStorage.setItem(_storageKey(sid), JSON.stringify([...doneSet])); } catch {}
  }

  const _ICONS = {
    blood:   `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" style="color:#ef4444;"><path d="M12 2C6 9 4 13.5 4 16a8 8 0 0016 0c0-2.5-2-7-8-14z"/></svg>`,
    urine:   `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" style="color:#f59e0b;"><path d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2V9M9 21H5a2 2 0 01-2-2V9m0 0h18"/></svg>`,
    imaging: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" style="color:#818cf8;"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>`,
    vitals:  `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" style="color:#10b981;"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>`,
    other:   `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" style="color:#94a3b8;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
  };
  const _ICON_CLS = { blood: 'sug-icon-blood', urine: 'sug-icon-urine', imaging: 'sug-icon-imaging', vitals: 'sug-icon-vitals', other: 'sug-icon-other' };

  function _esc(s) {
    return (s || '').toString().replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  async function render(containerEl, sessionId, opts) {
    opts = opts || {};
    const autoCheckMore = opts.autoCheckMore !== false;
    _instances[sessionId] = { tests: [], uploads: {}, newIds: new Set() };
    containerEl.dataset.sugSession = sessionId;
    containerEl.innerHTML = `<div class="sug-loading"><div class="spinner"></div> Analysing test requirements&hellip;</div>`;

    try {
      if (typeof getAuthToken === 'function' && getAuthToken()) {
        const ur = await fetch(`${API}/api/session/${sessionId}/suggested-test-files`, {
          headers: typeof authHeaders === 'function' ? authHeaders() : {},
        });
        if (ur.ok) {
          const ud = await ur.json();
          _instances[sessionId].uploads = ud.uploads || {};
        }
      }
    } catch { /* ignore */ }

    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 40000);
      const res = await fetch(`${API}/api/session/${sessionId}/suggested-tests`, { signal: ctrl.signal });
      clearTimeout(timer);
      if (!res.ok) { containerEl.innerHTML = ''; return; }
      const data = await res.json();
      if (!data.necessary || !data.tests || data.tests.length === 0) {
        containerEl.innerHTML = `<div class="sug-none">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
          No basic tests required.${data.rationale ? ' ' + _esc(data.rationale) : ''}
        </div>`;
        if (autoCheckMore) _autoCheckMore(containerEl, sessionId);
        return;
      }
      _instances[sessionId].tests = data.tests;
      containerEl.innerHTML = _buildHtml(sessionId, data);
      if (autoCheckMore) _autoCheckMore(containerEl, sessionId);
    } catch (e) {
      containerEl.innerHTML = e.name === 'AbortError'
        ? `<div class="sug-none" style="color:var(--text-muted);background:none;border:none;">Test analysis timed out &mdash; try again.</div>`
        : '';
    }
  }

  // Automatic, not user-triggered — the backend is idempotent per session
  // (only the first call ever actually invokes the LLM), so this is safe
  // to fire every time this widget renders. Silent: no loading state, no
  // "nothing found" message — only a "New" badge on any cards it adds.
  async function _autoCheckMore(containerEl, sessionId) {
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 40000);
      const res = await fetch(`${API}/api/session/${sessionId}/suggested-tests/more`, { method: 'POST', signal: ctrl.signal });
      clearTimeout(timer);
      if (!res.ok) return;
      const data = await res.json();
      const newIds = new Set(data.new_ids || []);
      if (newIds.size === 0) return;
      const inst = _instances[sessionId];
      inst.newIds = newIds;
      inst.tests = data.tests || [];
      containerEl.innerHTML = _buildHtml(sessionId, data);
    } catch { /* silent */ }
  }

  function _buildHtml(sessionId, data) {
    const inst = _instances[sessionId];
    const done = _loadDone(sessionId);
    const tests = inst.tests;
    const doneCount = tests.filter(t => done.has(t.id)).length;

    const cards = tests.map(t => {
      const isDone = done.has(t.id);
      const uploaded = inst.uploads[t.id];
      const badgeCls = t.priority === 'urgent' ? 'sug-badge-urgent' : 'sug-badge-routine';
      const iconCls = _ICON_CLS[t.category] || 'sug-icon-other';
      const icon = _ICONS[t.category] || '';
      const isNew = inst.newIds.has(t.id);
      const uploadBtn = uploaded
        ? `<span class="sug-uploaded-badge">
             <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
             Result uploaded
           </span>`
        : `<label class="sug-upload-btn" title="Upload test result">
             <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
             Upload result
             <input type="file" accept=".pdf,.jpg,.jpeg,.png,.gif,.webp,.heic,image/*,application/pdf"
               style="display:none" onchange="SugTestsWidget.uploadResult(event,'${_esc(sessionId)}','${_esc(t.id)}','${_esc(t.name)}')">
           </label>`;
      const accentCls = t.priority === 'urgent' ? 'card-urgent' : 'card-routine';
      return `
        <div class="sug-test-card${isDone ? ' done' : ''} ${accentCls}" id="sugcard-${_esc(sessionId)}-${_esc(t.id)}">
          <button class="sug-checkbox${isDone ? ' sug-checkbox-done' : ''}" onclick="SugTestsWidget.toggleDone('${_esc(sessionId)}','${_esc(t.id)}')" title="${isDone ? 'Mark as not done' : 'Mark as done'}">
            ${isDone ? `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>` : ''}
          </button>
          <div class="sug-icon ${iconCls}">${icon}</div>
          <div style="flex:1;min-width:0;">
            <div class="sug-test-name${isDone ? ' sug-test-name-done' : ''}">${_esc(t.name)}${isNew ? ' <span class="sug-new-badge">New</span>' : ''}</div>
            <div class="sug-test-why">${_esc(t.why)}</div>
            <div class="sug-test-meta" style="margin-top:8px;">
              <span class="sug-badge ${badgeCls}">${_esc(t.priority)}</span>
              ${t.where ? `<span class="sug-meta-pill">${_esc(t.where)}</span>` : ''}
            </div>
            ${t.preparation && t.preparation !== 'No special preparation required.' ? `<div class="sug-prep">${_esc(t.preparation)}</div>` : ''}
          </div>
          <div style="display:flex;flex-direction:column;align-items:flex-end;gap:8px;flex-shrink:0;">
            ${isDone ? `<span class="sug-done-badge">Done</span>` : ''}
            <div id="sug-upload-${_esc(sessionId)}-${_esc(t.id)}">${uploadBtn}</div>
          </div>
        </div>`;
    }).join('');

    return `
      <div class="sug-header">
        <div class="rd-hint" style="margin-top:0;">Worth getting done &mdash; upload if you have results, skip if not.</div>
      </div>
      ${data.rationale ? `<div class="sug-rationale">${_esc(data.rationale)}</div>` : ''}
      <div class="sug-status-bar">
        <span id="sug-status-text-${_esc(sessionId)}" class="sug-status-count">${doneCount} of ${tests.length} completed</span>
        <div class="sug-progress"><div class="sug-progress-fill" id="sug-progress-fill-${_esc(sessionId)}" style="width:${tests.length ? Math.round(doneCount / tests.length * 100) : 0}%"></div></div>
      </div>
      <div id="sug-cards-${_esc(sessionId)}">${cards}</div>`;
  }

  function toggleDone(sessionId, id) {
    const done = _loadDone(sessionId);
    if (done.has(id)) done.delete(id); else done.add(id);
    _saveDone(sessionId, done);
    _refreshCards(sessionId);
  }

  function _refreshCards(sessionId) {
    const inst = _instances[sessionId];
    if (!inst) return;
    const done = _loadDone(sessionId);
    const tests = inst.tests;
    const doneCount = tests.filter(t => done.has(t.id)).length;
    const fill = document.getElementById(`sug-progress-fill-${sessionId}`);
    const text = document.getElementById(`sug-status-text-${sessionId}`);
    if (fill) fill.style.width = tests.length ? Math.round(doneCount / tests.length * 100) + '%' : '0%';
    if (text) text.textContent = `${doneCount} of ${tests.length} completed`;

    tests.forEach(t => {
      const card = document.getElementById(`sugcard-${sessionId}-${t.id}`);
      if (!card) return;
      const isDone = done.has(t.id);
      const accentCls = t.priority === 'urgent' ? 'card-urgent' : 'card-routine';
      card.className = `sug-test-card ${accentCls}${isDone ? ' done' : ''}`;
      const btn = card.querySelector('.sug-checkbox');
      if (btn) {
        btn.className = 'sug-checkbox' + (isDone ? ' sug-checkbox-done' : '');
        btn.title = isDone ? 'Mark as not done' : 'Mark as done';
        btn.innerHTML = isDone ? `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>` : '';
      }
      const nameEl = card.querySelector('.sug-test-name');
      if (nameEl) nameEl.classList.toggle('sug-test-name-done', isDone);
      let badge = card.querySelector('.sug-done-badge');
      if (isDone && !badge) {
        badge = document.createElement('span');
        badge.className = 'sug-done-badge';
        badge.textContent = 'Done';
        card.appendChild(badge);
      } else if (!isDone && badge) {
        badge.remove();
      }
    });
  }

  async function uploadResult(event, sessionId, testId, testName) {
    const file = event.target.files[0];
    if (!file) return;
    const zone = document.getElementById(`sug-upload-${sessionId}-${testId}`);
    if (typeof getAccessToken !== 'function' || !getAccessToken()) {
      event.target.value = '';
      _toast('Please sign in to upload test results', '#ef4444');
      return;
    }
    if (zone) zone.innerHTML = '<span style="font-size:11px;color:var(--text-muted);padding:4px 10px;">Uploading&hellip;</span>';
    try {
      const fd = new FormData();
      fd.append('file', file);
      const url = `/api/session/${sessionId}/suggested-test-files?suggested_test_id=${encodeURIComponent(testId)}&suggested_test_name=${encodeURIComponent(testName)}`;
      const res = await fetch(url, { method: 'POST', headers: authHeaders(), body: fd });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Upload failed');
      }
      _instances[sessionId].uploads[testId] = { filename: file.name, uploaded_at: new Date().toISOString(), test_name: testName };
      const done = _loadDone(sessionId);
      done.add(testId);
      _saveDone(sessionId, done);
      if (zone) {
        zone.innerHTML = `<span class="sug-uploaded-badge">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
          Result uploaded
        </span>`;
      }
      _refreshCards(sessionId);
      _toast(`Result uploaded for ${testName}`);
    } catch (e) {
      if (zone) {
        zone.innerHTML = `<label class="sug-upload-btn" title="Upload test result">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          Upload result
          <input type="file" accept=".pdf,.jpg,.jpeg,.png,.gif,.webp,.heic,image/*,application/pdf" style="display:none"
            onchange="SugTestsWidget.uploadResult(event,'${_esc(sessionId)}','${_esc(testId)}','${_esc(testName)}')">
        </label>`;
      }
      _toast('Upload failed: ' + e.message, '#ef4444');
    }
  }

  window.SugTestsWidget = { render, toggleDone, uploadResult };
})();
