/**
 * Shared patient tab navigation.
 * Include after auth.js on every patient-facing page.
 * Auto-injects a tab bar below the nav and standardises page headers.
 */
(function () {
  const TABS = [
    {
      id: 'overview',
      label: 'Overview',
      href: '/patient-overview.html',
      match: ['/patient-overview.html', '/patient-landing.html'],
      icon: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>`,
    },
    {
      id: 'sessions',
      label: 'Sessions',
      href: '/my-sessions.html',
      match: ['/my-sessions.html'],
      icon: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>`,
    },
    {
      id: 'messages',
      label: 'Messages',
      href: '/messages.html',
      match: ['/messages.html'],
      icon: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`,
    },
    {
      id: 'book',
      label: 'Book',
      href: '/book-slot.html',
      match: ['/book-slot.html'],
      icon: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>`,
    },
    {
      id: 'history',
      label: 'History',
      href: '/health-history.html',
      match: ['/health-history.html'],
      icon: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 15"/></svg>`,
    },
    {
      id: 'profile',
      label: 'Profile',
      href: '/my-profile.html',
      match: ['/my-profile.html'],
      icon: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`,
    },
  ];

  // ── CSS ────────────────────────────────────────────────────────────────
  const style = document.createElement('style');
  style.textContent = `
    /* Widen the nav to fit brand + tabs + auth in one bar */
    nav.nav.patient-nav-merged {
      max-width: calc(100% - 32px);
      width: calc(100% - 32px);
      min-width: unset;
      border-radius: 14px;
      padding: 0 20px;
      gap: 4px;
    }
    .patient-nav-tabs {
      display: flex; align-items: center; gap: 6px;
      justify-content: flex-start;
      margin-left: 20px;
      margin-right: auto;
      padding: 4px 6px;
      background: rgba(13, 20, 38, 0.45);
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 999px;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
    }
    .patient-tab {
      display: inline-flex; align-items: center; gap: 7px;
      padding: 6px 14px; border-radius: 999px;
      font-family: 'Manrope', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 12.5px; font-weight: 700; color: #cbd5e1;
      cursor: pointer; border: none; background: none;
      text-decoration: none;
      transition: all 0.2s ease;
      white-space: nowrap;
    }
    .patient-tab:hover {
      color: #14b8a6;
      background: rgba(20, 184, 166, 0.12);
    }
    .patient-tab.active {
      background: linear-gradient(135deg, #0fa88d 0%, #07816e 100%) !important;
      color: #ffffff !important;
      box-shadow: 0 2px 10px rgba(15, 168, 141, 0.3) !important;
    }
    .patient-tab-badge {
      display: inline-flex; align-items: center; justify-content: center;
      min-width: 16px; height: 16px; border-radius: 99px;
      background: #10b981; color: #07090f;
      font-size: 9px; font-weight: 800; padding: 0 4px;
    }

    /* Standardise page header across all patient pages */
    .patient-page-header {
      display: flex; align-items: flex-start; justify-content: space-between;
      gap: 16px; margin-bottom: 24px; flex-wrap: wrap;
    }
    .patient-page-title {
      font-size: 22px; font-weight: 800; letter-spacing: -.03em;
      color: var(--text, #e2e8f0); line-height: 1.2; margin-bottom: 3px;
    }
    .patient-page-sub {
      font-size: 13px; color: var(--text-muted, #64748b);
    }
    @media (max-width: 700px) {
      .patient-tab { font-size: 11px; padding: 5px 8px; gap: 4px; }
      .patient-tab svg { display: none; }
    }
  `;
  document.head.appendChild(style);

  // ── Merge tabs into nav ────────────────────────────────────────────────
  function injectTabBar() {
    if (document.getElementById('patient-nav-tabs')) return;
    const nav = document.querySelector('nav.nav');
    if (!nav) return;

    // Update brand to DiffDx
    const brand = nav.querySelector('.nav-brand');
    if (brand) {
      brand.innerHTML = `
        <svg width="28" height="28" viewBox="0 0 28 28" fill="none" xmlns="http://www.w3.org/2000/svg" style="filter:drop-shadow(0 0 8px rgba(16,185,129,.5))">
          <defs>
            <linearGradient id="logo-ring" x1="0" y1="0" x2="28" y2="28" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stop-color="#10b981"/>
              <stop offset="100%" stop-color="#a855f7"/>
            </linearGradient>
            <linearGradient id="logo-fork-top" x1="13" y1="14" x2="19" y2="8" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stop-color="#10b981"/>
              <stop offset="100%" stop-color="#34d399"/>
            </linearGradient>
            <linearGradient id="logo-fork-bot" x1="13" y1="14" x2="19" y2="20" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stop-color="#10b981"/>
              <stop offset="100%" stop-color="#a855f7"/>
            </linearGradient>
          </defs>
          <!-- Outer ring -->
          <circle cx="14" cy="14" r="12.5" stroke="url(#logo-ring)" stroke-width="1.25" fill="rgba(16,185,129,.08)"/>
          <!-- Stem line -->
          <line x1="6" y1="14" x2="13" y2="14" stroke="#10b981" stroke-width="2" stroke-linecap="round"/>
          <!-- Fork top branch -->
          <path d="M13 14 L19 8.5" stroke="url(#logo-fork-top)" stroke-width="2" stroke-linecap="round"/>
          <!-- Fork bottom branch -->
          <path d="M13 14 L19 19.5" stroke="url(#logo-fork-bot)" stroke-width="2" stroke-linecap="round"/>
          <!-- End dots -->
          <circle cx="20" cy="8.5" r="2" fill="#34d399"/>
          <circle cx="20" cy="19.5" r="2" fill="#a855f7"/>
        </svg>
        <span style="font-family:'Sora',sans-serif;font-size:15px;font-weight:800;letter-spacing:-.03em;color:#f1f5f9;">Diff<span style="background:linear-gradient(90deg,#10b981,#34d399);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;">/Dx</span></span>
      `;
    }

    // Build inline tabs container
    const path = location.pathname;
    const tabsEl = document.createElement('div');
    tabsEl.id = 'patient-nav-tabs';
    tabsEl.className = 'patient-nav-tabs patient-topbar-tabs';
    tabsEl.innerHTML = TABS.map(t => {
      const isActive = t.match.some(m => path === m || path.startsWith(m));
      return `<a class="patient-nav-tab patient-topbar-tab patient-tab${isActive ? ' active' : ''}" href="${t.href}" id="ptab-${t.id}">
        ${t.icon}<span>${t.label}</span>
      </a>`;
    }).join('');

    // Insert tabs before the spacer (or before nav-auth if no spacer)
    const spacer = nav.querySelector('.nav-spacer');
    const authEl = nav.querySelector('#nav-auth');
    if (spacer) {
      nav.insertBefore(tabsEl, spacer);
    } else if (authEl) {
      nav.insertBefore(tabsEl, authEl);
    } else {
      nav.appendChild(tabsEl);
    }

    // Widen the nav to accommodate everything
    nav.classList.add('patient-nav-merged');

    // Clear fixed nav (top:16px + height:52px + gap:36px = 104px)
    document.querySelectorAll('.page, .bs-page, .ph-page, .hh-page').forEach(el => {
      el.style.paddingTop = '104px';
    });

    // Load unread badge for messages
    _loadPatientTabBadges();
  }

  async function _loadPatientTabBadges() {
    try {
      if (typeof authHeaders !== 'function') return;
      const r = await fetch('/api/messages/unread-count', { headers: authHeaders() });
      if (!r.ok) return;
      const { count } = await r.json();
      if (count > 0) {
        const tab = document.getElementById('ptab-messages');
        if (tab) {
          const badge = document.createElement('span');
          badge.className = 'patient-tab-badge';
          badge.textContent = count > 9 ? '9+' : count;
          tab.appendChild(badge);
        }
      }
    } catch (_) {}
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectTabBar);
  } else {
    injectTabBar();
  }
})();
