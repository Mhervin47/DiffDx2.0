/**
 * Shared auth helpers — included by every page.
 * Reads/writes localStorage keys: authAccessToken, authRefreshToken, authUser.
 *
 * Task 5: the backend switched from a single long-lived opaque token to a
 * short-lived (15 min) JWT access token + a longer-lived (7 day) refresh
 * token. This file now holds both, and window.fetch is wrapped to
 * transparently refresh + retry once on a 401 instead of immediately
 * logging the user out — see _refreshAccessToken and the fetch override
 * below. The old client-side 8-hour "guess when to show a toast" timer is
 * gone; the toast now only fires when a refresh genuinely fails (the
 * refresh token itself expired, was revoked, or never existed), which is
 * the actual signal, not a guess.
 */

const _AUTH_ACCESS_KEY  = 'authAccessToken';
const _AUTH_REFRESH_KEY = 'authRefreshToken';
const _AUTH_USER_KEY    = 'authUser';

function getAccessToken() { return localStorage.getItem(_AUTH_ACCESS_KEY); }
function getRefreshToken() { return localStorage.getItem(_AUTH_REFRESH_KEY); }
/** Kept as an alias — every other frontend file calls getAuthToken() for the
 * bearer token to send, and that's still the (short-lived) access token. */
function getAuthToken() { return getAccessToken(); }
function getAuthUser() {
  const raw = localStorage.getItem(_AUTH_USER_KEY);
  try { return raw ? JSON.parse(raw) : null; } catch { return null; }
}
function setAuth(accessToken, refreshToken, user) {
  localStorage.setItem(_AUTH_ACCESS_KEY, accessToken);
  localStorage.setItem(_AUTH_REFRESH_KEY, refreshToken);
  localStorage.setItem(_AUTH_USER_KEY, JSON.stringify(user));
}
function clearAuth() {
  localStorage.removeItem(_AUTH_ACCESS_KEY);
  localStorage.removeItem(_AUTH_REFRESH_KEY);
  localStorage.removeItem(_AUTH_USER_KEY);
}
function authHeaders() {
  const t = getAccessToken();
  return t ? { 'Authorization': `Bearer ${t}` } : {};
}
function logout() {
  // Best-effort server-side revoke so the refresh token can't be reused —
  // fire-and-forget, don't block the redirect on it.
  const rt = getRefreshToken();
  if (rt) {
    _origFetch('/api/auth/logout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt }),
    }).catch(() => {});
  }
  clearAuth();
  window.location.href = '/';
}

/** Dedupe concurrent refresh attempts (several requests can 401 at once
 * right as the access token expires) into a single in-flight request, and
 * rotate: the backend issues a new refresh token on every use and revokes
 * the old one, so the rotated value must be persisted here too. */
let _refreshInFlight = null;
async function _refreshAccessToken() {
  const rt = getRefreshToken();
  if (!rt) return null;
  if (_refreshInFlight) return _refreshInFlight;
  _refreshInFlight = (async () => {
    try {
      const res = await _origFetch('/api/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: rt }),
      });
      if (!res.ok) { clearAuth(); return null; }
      const data = await res.json();
      localStorage.setItem(_AUTH_ACCESS_KEY, data.access_token);
      localStorage.setItem(_AUTH_REFRESH_KEY, data.refresh_token);
      return data.access_token;
    } catch {
      return null;
    } finally {
      _refreshInFlight = null;
    }
  })();
  return _refreshInFlight;
}

function _showSessionExpiredToast() {
  let toast = document.getElementById('_session-expired-toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = '_session-expired-toast';
    toast.style.cssText = `
      position:fixed;bottom:24px;left:50%;transform:translateX(-50%);
      background:#1e2a38;border:1px solid rgba(239,68,68,.4);border-radius:10px;
      padding:14px 22px;display:flex;align-items:center;gap:12px;
      box-shadow:0 8px 32px rgba(0,0,0,.5);z-index:99999;
      font-family:Inter,sans-serif;font-size:13px;color:#f1f5f9;
      animation:_toastIn .25s ease;
    `;
    const style = document.createElement('style');
    style.textContent = `@keyframes _toastIn{from{opacity:0;transform:translateX(-50%) translateY(12px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}`;
    document.head.appendChild(style);
    toast.innerHTML = `
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
      <span>Your session has expired.</span>
      <a href="/login.html" style="color:#00b4d8;font-weight:600;text-decoration:none;margin-left:4px;">Sign in again</a>
    `;
    document.body.appendChild(toast);
  }
  toast.style.display = 'flex';
}

/**
 * Intercept fetch calls — on a 401, try a transparent refresh + one retry
 * before giving up. Only genuinely-expired-refresh-token (or no refresh
 * token at all) cases fall through to the session-expired toast.
 */
const _origFetch = window.fetch;
window.fetch = async function(...args) {
  const [url, init] = args;
  const res = await _origFetch(...args);
  if (res.status !== 401) return res;

  const urlStr = typeof url === 'string' ? url : (url && url.url) || '';
  const isAuthTokenEndpoint = /\/api\/auth\/(refresh|login|register)(\?|$)/.test(urlStr);
  const alreadyRetried = !!(init && init._diffdxRetried);
  if (isAuthTokenEndpoint || alreadyRetried || !getRefreshToken()) {
    if (getAccessToken()) {
      clearAuth();
      _showSessionExpiredToast();
    }
    return res;
  }

  const newAccessToken = await _refreshAccessToken();
  if (!newAccessToken) {
    _showSessionExpiredToast();
    return res;
  }
  const headers = new Headers((init && init.headers) || {});
  headers.set('Authorization', `Bearer ${newAccessToken}`);
  const retryInit = { ...(init || {}), headers, _diffdxRetried: true };
  return _origFetch(url, retryInit);
};

function _escHtml(s) {
  return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

/** Inject auth state into any element with id="nav-auth" */
function initAuthNav() {
  const el = document.getElementById('nav-auth');
  if (!el) return;
  const user = getAuthUser();
  if (user) {
    if (user.role === 'doctor') {
      el.innerHTML = `
        <div class="nav-user-menu" id="nav-user-menu">
          <button class="nav-user-btn" onclick="_toggleUserMenu(event)" aria-expanded="false">
            <span class="nav-user-avatar">${_escHtml(user.name.charAt(0).toUpperCase())}</span>
            <span class="nav-user-name">${_escHtml(user.name)}</span>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
          </button>
          <div class="nav-user-dropdown" id="nav-user-dropdown">
            <div class="nav-user-dropdown-header">
              <div style="font-weight:600;font-size:13px;">${_escHtml(user.name)}</div>
              <div style="font-size:11px;color:var(--text-muted);">${_escHtml(user.email || '')}</div>
            </div>
            <a class="nav-user-dropdown-item" href="/doctor-overview.html">Overview</a>
            <a class="nav-user-dropdown-item" href="/doctor-portal.html">Patient Queue</a>
            <a class="nav-user-dropdown-item" href="/doctor-profile.html">My Profile</a>
            <a class="nav-user-dropdown-item" href="/doctor-analytics.html">Analytics</a>
            <a class="nav-user-dropdown-item" href="/messages.html">Messages <span id="nav-msg-badge" style="display:none;margin-left:auto;background:#ef4444;color:#fff;font-size:9px;font-weight:800;border-radius:99px;padding:1px 6px;"></span></a>
            <button class="nav-user-dropdown-item nav-user-dropdown-item--danger" onclick="logout()">Sign Out</button>
          </div>
        </div>
      `;
    } else {
      el.innerHTML = `
        <a href="/notifications.html" class="nav-icon-btn" title="Notifications" id="nav-notif-btn">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 01-3.46 0"/></svg>
          <span id="nav-notif-badge" style="display:none;position:absolute;top:-4px;right:-4px;background:#00b4d8;color:#000;font-size:9px;font-weight:800;border-radius:99px;padding:1px 5px;line-height:1.6;min-width:16px;text-align:center;"></span>
        </a>
        <div class="nav-user-menu" id="nav-user-menu">
          <button class="nav-user-btn" onclick="_toggleUserMenu(event)" aria-expanded="false">
            <span class="nav-user-avatar">${_escHtml(user.name.charAt(0).toUpperCase())}</span>
            <span class="nav-user-name">${_escHtml(user.name)}</span>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
          </button>
          <div class="nav-user-dropdown" id="nav-user-dropdown">
            <div class="nav-user-dropdown-header">
              <div style="font-weight:600;font-size:13px;">${_escHtml(user.name)}</div>
              <div style="font-size:11px;color:var(--text-muted);">${_escHtml(user.email || '')}</div>
            </div>
            <a class="nav-user-dropdown-item" href="/patient-overview.html">Overview</a>
            <a class="nav-user-dropdown-item" href="/my-sessions.html">My Sessions</a>
            <a class="nav-user-dropdown-item" href="/my-profile.html">My Profile</a>
            <a class="nav-user-dropdown-item" href="/health-history.html">Health History</a>
            <a class="nav-user-dropdown-item" href="/find-doctors.html">Find Doctors</a>
            <a class="nav-user-dropdown-item" href="/messages.html">Messages <span id="nav-msg-badge" style="display:none;margin-left:auto;background:#ef4444;color:#fff;font-size:9px;font-weight:800;border-radius:99px;padding:1px 6px;"></span></a>
            <button class="nav-user-dropdown-item nav-user-dropdown-item--danger" onclick="logout()">Sign Out</button>
          </div>
        </div>
      `;
      _loadTestNotifBadge();
    }
    _loadMsgBadge();
  } else {
    el.innerHTML = `
      <a href="/login.html" class="btn btn-ghost btn-sm" style="font-size:12px;padding:5px 10px;">Sign In</a>
    `;
  }
}

function _toggleUserMenu(e) {
  e.stopPropagation();
  const dropdown = document.getElementById('nav-user-dropdown');
  const btn = e.currentTarget;
  const open = dropdown.classList.toggle('open');
  btn.setAttribute('aria-expanded', open);
  if (open) {
    const close = (ev) => {
      if (!document.getElementById('nav-user-menu')?.contains(ev.target)) {
        dropdown.classList.remove('open');
        btn.setAttribute('aria-expanded', 'false');
        document.removeEventListener('click', close);
      }
    };
    document.addEventListener('click', close);
  }
}

function _seenTestIds() {
  try { return new Set(JSON.parse(localStorage.getItem('seen_test_notifs') || '[]')); } catch { return new Set(); }
}
function _markTestIdsSeen(ids) {
  try {
    const seen = _seenTestIds();
    ids.forEach(id => seen.add(id));
    localStorage.setItem('seen_test_notifs', JSON.stringify([...seen]));
  } catch {}
}

async function _loadTestNotifBadge() {
  try {
    const [notifRes, apptRes, msgRes] = await Promise.all([
      _origFetch('/api/patient/test-notifications', { headers: { ...authHeaders() } }),
      _origFetch('/api/appointments', { headers: { ...authHeaders() } }),
      _origFetch('/api/messages/unread-count', { headers: { ...authHeaders() } }),
    ]);

    // Test orders badge (My Sessions button)
    if (notifRes.ok) {
      const data = await notifRes.json();
      const seen = _seenTestIds();
      const unseen = (data.appointments || []).filter(a => !seen.has(a.appt_id));
      const count = unseen.reduce((s, a) => s + (a.test_count || 0), 0);
      if (count > 0) {
        const badge = document.getElementById('test-notif-badge');
        if (badge) { badge.textContent = count; badge.style.display = 'inline'; }
      }
    }

    // Bell badge = unread notifications across all types
    const seenNotifIds = (() => { try { return new Set(JSON.parse(localStorage.getItem('notif_seen_ids') || '[]')); } catch { return new Set(); } })();
    let bellCount = 0;
    if (apptRes.ok) {
      const apptData = await apptRes.json();
      for (const a of (apptData.appointments || [])) {
        if (a.referral && !seenNotifIds.has(`ref-${a.id}`)) bellCount++;
        if (a.test_orders?.length && !seenNotifIds.has(`tests-${a.id}`)) bellCount++;
        if (a.prescriptions?.length && !seenNotifIds.has(`rx-${a.id}`)) bellCount++;
        if (a.refill_request?.status === 'fulfilled' && !seenNotifIds.has(`refill-${a.id}`)) bellCount++;
      }
    }
    if (msgRes.ok) { const d = await msgRes.json(); if (d.count > 0 && !seenNotifIds.has('unread-messages')) bellCount++; }
    if (bellCount > 0) {
      const badge = document.getElementById('nav-notif-badge');
      if (badge) { badge.textContent = bellCount; badge.style.display = 'inline'; }
    }
  } catch { /* ignore */ }
}

async function _loadDoctorRefillBadge() {
  try {
    const res = await _origFetch('/api/doctor/pending-refills', { headers: { ...authHeaders() } });
    if (!res.ok) return;
    const data = await res.json();
    if (data.pending > 0) {
      const badge = document.getElementById('nav-refill-badge');
      if (badge) { badge.textContent = data.pending; badge.style.display = 'inline'; }
    }
  } catch { /* ignore */ }
}

async function _loadMsgBadge() {
  try {
    const res = await _origFetch('/api/messages/unread-count', { headers: { ...authHeaders() } });
    if (!res.ok) return;
    const data = await res.json();
    if (data.count > 0) {
      const badge = document.getElementById('nav-msg-badge');
      if (badge) { badge.textContent = data.count; badge.style.display = 'inline'; }
    }
  } catch { /* ignore */ }
}

document.addEventListener('DOMContentLoaded', initAuthNav);

/** Auto-inject ambient gradient background on pages that don't already have one */
function _injectAmbientBg() {
  if (document.getElementById('_ambient-bg')) return;
  if (document.querySelector('.bg-blobs, .blob-scene, .bg-grid, .bg-glow-left')) return; // already has background

  const bg = document.createElement('div');
  bg.id = '_ambient-bg';
  bg.setAttribute('aria-hidden', 'true');
  bg.style.cssText = 'position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden;';
  bg.innerHTML = `
    <div style="position:absolute;inset:0;
      background:linear-gradient(125deg,rgba(16,185,129,.14) 0%,transparent 35%,rgba(168,85,247,.11) 65%,transparent 100%);
      background-size:300% 300%;animation:_amb-flow 14s linear infinite;"></div>
    <div style="position:absolute;width:700px;height:700px;top:-220px;left:-200px;border-radius:50%;
      filter:blur(90px);opacity:.58;
      background:radial-gradient(circle,rgba(16,185,129,.45) 0%,transparent 70%);
      animation:_amb-drift-1 18s ease-in-out infinite;"></div>
    <div style="position:absolute;width:580px;height:580px;bottom:-180px;right:-160px;border-radius:50%;
      filter:blur(80px);opacity:.52;
      background:radial-gradient(circle,rgba(168,85,247,.45) 0%,transparent 70%);
      animation:_amb-drift-2 22s ease-in-out infinite;"></div>
    <div style="position:absolute;width:380px;height:380px;top:50%;left:50%;
      transform:translate(-50%,-50%);border-radius:50%;
      filter:blur(70px);opacity:.24;
      background:radial-gradient(circle,rgba(168,85,247,.5) 0%,transparent 70%);
      animation:_amb-drift-1 28s ease-in-out infinite reverse;"></div>
  `;
  document.body.insertBefore(bg, document.body.firstChild);
}
document.addEventListener('DOMContentLoaded', _injectAmbientBg);

/** Auto-inject universal reloader fallback if not already in head */
(function _ensureReloader() {
  if (!document.querySelector('link[href*="reloader.css"]')) {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/reloader.css';
    document.head.appendChild(link);
  }
  if (!window.DiffDxReloader && !document.querySelector('script[src*="reloader.js"]')) {
    const s = document.createElement('script');
    s.src = '/reloader.js';
    document.head.appendChild(s);
  }
})();
