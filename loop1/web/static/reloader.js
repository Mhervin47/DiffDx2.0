/**
 * DiffDx — Universal Theme-Aware Clinical Reloader & Preloader
 * Executes early in <head> for zero-flicker reload & page transitions.
 */

(function () {
  'use strict';

  const STATUS_MESSAGES = [
    'Synchronizing clinical environment...',
    'Calibrating diagnostic reasoning pipeline...',
    'Securing HIPAA-compliant channel...',
    'Loading clinical workspace...',
    'DiffDx Clinical Intelligence active...'
  ];

  let _startTime = Date.now();
  let _hasDismissed = false;
  let _msgInterval = null;

  function getActiveTheme() {
    try {
      const saved = localStorage.getItem('theme');
      if (saved === 'light') return 'light';
      if (saved === 'dark') return 'dark';
    } catch (e) {}

    if (document.documentElement.classList.contains('light-theme') || 
        document.body?.classList.contains('light-theme')) {
      return 'light';
    }
    if (document.documentElement.classList.contains('dark-theme')) {
      return 'dark';
    }
    return 'dark'; // Default DiffDx theme
  }

  function updateReloaderTheme() {
    const el = document.getElementById('diffdx-page-reloader');
    if (!el) return;
    const theme = getActiveTheme();
    if (theme === 'light') {
      el.classList.remove('reloader-dark');
      el.classList.add('reloader-light');
    } else {
      el.classList.remove('reloader-light');
      el.classList.add('reloader-dark');
    }
  }

  function createReloaderElement() {
    const theme = getActiveTheme();
    const themeClass = theme === 'light' ? 'reloader-light' : 'reloader-dark';

    const overlay = document.createElement('div');
    overlay.id = 'diffdx-page-reloader';
    overlay.className = themeClass;
    overlay.setAttribute('aria-live', 'polite');
    overlay.setAttribute('aria-label', 'Loading DiffDx Clinical Workspace');

    overlay.innerHTML = `
      <div class="reloader-card">
        <!-- Concentric Rings & Original DiffDx Logo -->
        <div class="reloader-emblem-wrap">
          <div class="reloader-pulse-ring"></div>
          <div class="reloader-pulse-ring reloader-pulse-ring-2"></div>
          <div class="reloader-emblem-core">
            <svg class="reloader-original-logo" width="60" height="60" viewBox="0 0 28 28" fill="none" xmlns="http://www.w3.org/2000/svg">
              <defs>
                <linearGradient id="rel-ring" x1="0" y1="0" x2="28" y2="28" gradientUnits="userSpaceOnUse">
                  <stop offset="0%" stop-color="#0fa88d"/>
                  <stop offset="100%" stop-color="#2879d8"/>
                </linearGradient>
                <linearGradient id="rel-ft" x1="13" y1="14" x2="19" y2="8" gradientUnits="userSpaceOnUse">
                  <stop offset="0%" stop-color="#0fa88d"/>
                  <stop offset="100%" stop-color="#14b8a6"/>
                </linearGradient>
                <linearGradient id="rel-fb" x1="13" y1="14" x2="19" y2="20" gradientUnits="userSpaceOnUse">
                  <stop offset="0%" stop-color="#0fa88d"/>
                  <stop offset="100%" stop-color="#2879d8"/>
                </linearGradient>
              </defs>
              <circle cx="14" cy="14" r="12.5" stroke="url(#rel-ring)" stroke-width="1.3" fill="rgba(15,168,141,.08)"/>
              <line x1="6" y1="14" x2="13" y2="14" stroke="#0fa88d" stroke-width="2" stroke-linecap="round"/>
              <path d="M13 14 L19 8.5" stroke="url(#rel-ft)" stroke-width="2" stroke-linecap="round"/>
              <path d="M13 14 L19 19.5" stroke="url(#rel-fb)" stroke-width="2" stroke-linecap="round"/>
              <circle cx="20" cy="8.5" r="2" fill="#14b8a6"/>
              <circle cx="20" cy="19.5" r="2" fill="#2879d8"/>
            </svg>
          </div>
        </div>

        <!-- DiffDx Brand Header -->
        <div class="reloader-brand">
          Diff<span class="reloader-brand-accent">Dx</span>
        </div>
        <div class="reloader-subtitle">
          Clinical Intelligence
        </div>

        <!-- Telemetry ECG Cardiac Waveform -->
        <div class="reloader-ecg-container">
          <svg class="reloader-ecg-svg" viewBox="0 0 300 40">
            <path d="M 0 20 L 70 20 L 80 12 L 90 28 L 100 20 L 115 20 L 125 4 L 135 36 L 145 10 L 155 24 L 165 20 L 190 20 L 200 14 L 210 24 L 220 20 L 300 20" />
          </svg>
        </div>

        <!-- Dynamic Shimmer Track -->
        <div class="reloader-track">
          <div class="reloader-bar"></div>
        </div>

        <!-- Dynamic Live Telemetry Status -->
        <div class="reloader-status-pill">
          <span class="reloader-dot-live"></span>
          <span id="diffdx-reloader-msg">Synchronizing clinical environment...</span>
        </div>
      </div>
    `;

    return overlay;
  }

  function mountReloader() {
    let el = document.getElementById('diffdx-page-reloader');
    if (!el) {
      el = createReloaderElement();
      if (document.body) {
        document.body.insertBefore(el, document.body.firstChild);
      } else if (document.documentElement) {
        document.documentElement.appendChild(el);
      }
    }
    updateReloaderTheme();
    startStatusRotation();
  }

  function startStatusRotation() {
    if (_msgInterval) clearInterval(_msgInterval);
    let idx = 0;
    _msgInterval = setInterval(() => {
      const msgEl = document.getElementById('diffdx-reloader-msg');
      if (msgEl && !_hasDismissed) {
        idx = (idx + 1) % STATUS_MESSAGES.length;
        msgEl.style.opacity = '0';
        setTimeout(() => {
          if (msgEl) {
            msgEl.textContent = STATUS_MESSAGES[idx];
            msgEl.style.opacity = '1';
          }
        }, 150);
      }
    }, 1200);
  }

  function dismissReloader(minPerceptionMs = 350) {
    if (_hasDismissed) return;

    const elapsed = Date.now() - _startTime;
    const remaining = Math.max(0, minPerceptionMs - elapsed);

    setTimeout(() => {
      const el = document.getElementById('diffdx-page-reloader');
      if (el) {
        el.classList.add('reloader-hidden');
      }
      _hasDismissed = true;
      if (_msgInterval) clearInterval(_msgInterval);
    }, remaining);
  }

  function showReloaderForReload() {
    const el = document.getElementById('diffdx-page-reloader');
    if (!el) return;
    _hasDismissed = false;
    _startTime = Date.now();
    updateReloaderTheme();

    const msgEl = document.getElementById('diffdx-reloader-msg');
    if (msgEl) {
      msgEl.textContent = 'Refreshing clinical session...';
      msgEl.style.opacity = '1';
    }

    el.classList.remove('reloader-hidden');
  }

  // 1. Mount immediately so screen is painted with loader during refresh
  if (document.readyState === 'loading') {
    if (document.body) {
      mountReloader();
    } else if (document.documentElement) {
      mountReloader();
    }
    document.addEventListener('DOMContentLoaded', () => {
      // Re-parent to body if needed
      const el = document.getElementById('diffdx-page-reloader');
      if (el && document.body && el.parentNode !== document.body) {
        document.body.insertBefore(el, document.body.firstChild);
      }
      updateReloaderTheme();
      // Dismiss on DOMContentLoaded if load takes too long
      setTimeout(() => dismissReloader(400), 500);
    });
  } else {
    mountReloader();
    dismissReloader(250);
  }

  // 2. Dismiss when everything (images, styles, audio) finishes loading
  window.addEventListener('load', () => {
    dismissReloader(350);
  });

  // 3. Fallback safety timer: never lock page for more than 1.8s
  setTimeout(() => {
    dismissReloader(0);
  }, 1800);

  // 4. Trigger instantly on reload / refresh / page exit
  window.addEventListener('beforeunload', () => {
    showReloaderForReload();
  });

  // 5. Back-forward cache handler
  window.addEventListener('pageshow', (event) => {
    dismissReloader(150);
  });

  // 6. Listen for theme toggles dynamically
  try {
    const observer = new MutationObserver(() => {
      updateReloaderTheme();
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    if (document.body) {
      observer.observe(document.body, { attributes: true, attributeFilter: ['class'] });
    }
  } catch (e) {}

  // Expose API for programmatic access if needed
  window.DiffDxReloader = {
    show: showReloaderForReload,
    hide: () => dismissReloader(0),
    updateTheme: updateReloaderTheme
  };
})();
