/**
 * DiffDx Medical Disclaimer + Terms & Conditions + Data Usage Consent Modal
 *
 * Provides a clean, clinical healthcare consent modal adhering strictly to
 * DiffDx theme tokens (Sora/Noto Sans, CSS variables, Light & Dark themes).
 *
 * API:
 *   window.DiffDxConsent.hasConsented()
 *   window.DiffDxConsent.show({ onAccept, onCancel, mode })
 *   window.DiffDxConsent.hide()
 *   window.DiffDxConsent.checkRegisteredUserConsent(userId)
 */
(function() {
  const CONSENT_VERSION = '1.0';
  let _activeResolve = null;
  let _activeReject = null;
  let _previousActiveElement = null;

  function _getUser() {
    try {
      if (typeof getAuthUser === 'function') return getAuthUser();
      const raw = localStorage.getItem('authUser');
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }

  function hasConsented() {
    const user = _getUser();
    if (user && (user.id || user.email)) {
      const key = 'diffdx_consent_' + (user.id || user.email);
      if (localStorage.getItem(key)) return true;
    }
    const guestConsent = sessionStorage.getItem('diffdx_guest_consent') || localStorage.getItem('diffdx_guest_consent');
    return !!guestConsent;
  }

  function recordConsent() {
    const timestamp = new Date().toISOString();
    const user = _getUser();
    if (user && (user.id || user.email)) {
      const key = 'diffdx_consent_' + (user.id || user.email);
      localStorage.setItem(key, JSON.stringify({ version: CONSENT_VERSION, accepted_at: timestamp }));
    }
    sessionStorage.setItem('diffdx_guest_consent', timestamp);
    localStorage.setItem('diffdx_guest_consent', timestamp);
  }

  function _injectStyles() {
    if (document.getElementById('diffdx-consent-styles')) return;
    const style = document.createElement('style');
    style.id = 'diffdx-consent-styles';
    style.textContent = `
      .diffdx-consent-overlay {
        position: fixed;
        inset: 0;
        z-index: 99999;
        background: rgba(15, 23, 42, 0.65);
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 16px;
        box-sizing: border-box;
      }
      html.dark-theme .diffdx-consent-overlay {
        background: rgba(3, 8, 16, 0.82);
      }

      .diffdx-consent-modal {
        background: var(--bg-card, #FFFFFF);
        border: 1px solid var(--border-card, #CBD5E1);
        border-radius: 16px;
        width: 100%;
        max-width: 580px;
        box-shadow: 0 20px 30px -10px rgba(0, 0, 0, 0.25), 0 4px 12px rgba(0, 0, 0, 0.1);
        display: flex;
        flex-direction: column;
        max-height: calc(100vh - 32px);
        overflow: hidden;
        box-sizing: border-box;
        font-family: var(--font-body, 'Noto Sans', -apple-system, BlinkMacSystemFont, sans-serif);
        color: var(--text-main, #0F172A);
      }
      html.dark-theme .diffdx-consent-modal {
        background: var(--bg-card, #0A1525);
        border-color: var(--border-card, rgba(150, 180, 210, 0.15));
        color: var(--text-main, #EAF2FA);
        box-shadow: 0 24px 48px -12px rgba(0, 0, 0, 0.7);
      }

      .diffdx-consent-header {
        padding: 22px 24px 16px;
        border-bottom: 1px solid var(--border-card, #E2E8F0);
        display: flex;
        align-items: flex-start;
        gap: 14px;
      }
      html.dark-theme .diffdx-consent-header {
        border-bottom-color: var(--border-card, rgba(150, 180, 210, 0.12));
      }

      .diffdx-consent-icon-badge {
        width: 38px;
        height: 38px;
        border-radius: 10px;
        background: rgba(13, 148, 136, 0.1);
        border: 1px solid rgba(13, 148, 136, 0.25);
        color: var(--teal-primary, #0D9488);
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
      }
      html.dark-theme .diffdx-consent-icon-badge {
        background: rgba(25, 198, 173, 0.1);
        border-color: rgba(25, 198, 173, 0.25);
        color: var(--teal-primary, #19C6AD);
      }

      .diffdx-consent-title-col {
        flex: 1;
        min-width: 0;
      }

      .diffdx-consent-title {
        font-family: var(--font-heading, 'Sora', sans-serif);
        font-size: 19px;
        font-weight: 700;
        letter-spacing: -0.02em;
        color: var(--text-main, #0F172A);
        margin: 0 0 3px;
        line-height: 1.3;
      }
      html.dark-theme .diffdx-consent-title {
        color: var(--text-main, #EAF2FA);
      }

      .diffdx-consent-subtitle {
        font-size: 13px;
        line-height: 1.4;
        color: var(--text-muted, #475569);
        margin: 0;
      }
      html.dark-theme .diffdx-consent-subtitle {
        color: var(--text-muted, #8FA2B9);
      }

      .diffdx-consent-body {
        padding: 16px 24px;
        overflow-y: auto;
        flex: 1;
        display: flex;
        flex-direction: column;
        gap: 14px;
        box-sizing: border-box;
      }

      .diffdx-consent-scrollbox {
        background: var(--fi-bg, #F8FAFC);
        border: 1px solid var(--border-card, #CBD5E1);
        border-radius: 10px;
        padding: 14px 16px;
        max-height: 220px;
        overflow-y: auto;
        box-sizing: border-box;
        scroll-behavior: smooth;
      }
      html.dark-theme .diffdx-consent-scrollbox {
        background: var(--fi-bg, #0C192A);
        border-color: var(--border-card, rgba(150, 180, 210, 0.12));
      }

      .diffdx-consent-scrollbox::-webkit-scrollbar {
        width: 6px;
      }
      .diffdx-consent-scrollbox::-webkit-scrollbar-track {
        background: transparent;
      }
      .diffdx-consent-scrollbox::-webkit-scrollbar-thumb {
        background: rgba(100, 116, 139, 0.3);
        border-radius: 4px;
      }
      html.dark-theme .diffdx-consent-scrollbox::-webkit-scrollbar-thumb {
        background: rgba(150, 180, 210, 0.25);
      }

      .diffdx-consent-section {
        margin-bottom: 14px;
        transition: background 0.3s ease;
        padding: 4px 6px;
        border-radius: 6px;
      }
      .diffdx-consent-section:last-child {
        margin-bottom: 0;
      }
      .diffdx-consent-section.highlighted {
        background: rgba(13, 148, 136, 0.1);
      }
      html.dark-theme .diffdx-consent-section.highlighted {
        background: rgba(25, 198, 173, 0.12);
      }

      .diffdx-consent-section-title {
        font-family: var(--font-heading, 'Sora', sans-serif);
        font-size: 13px;
        font-weight: 700;
        color: var(--text-main, #0F172A);
        margin: 0 0 4px;
      }
      html.dark-theme .diffdx-consent-section-title {
        color: var(--text-main, #EAF2FA);
      }

      .diffdx-consent-section-text {
        font-size: 12px;
        line-height: 1.55;
        color: var(--text-muted, #475569);
        margin: 0;
      }
      html.dark-theme .diffdx-consent-section-text {
        color: var(--text-muted, #8FA2B9);
      }

      .diffdx-consent-links {
        display: flex;
        align-items: center;
        gap: 12px;
        font-size: 11.5px;
        padding: 0 2px;
        flex-wrap: wrap;
      }
      .diffdx-consent-link {
        color: var(--teal-primary, #0D9488);
        text-decoration: none;
        font-weight: 600;
        cursor: pointer;
        background: none;
        border: none;
        padding: 0;
        font-family: inherit;
      }
      html.dark-theme .diffdx-consent-link {
        color: var(--teal-primary, #19C6AD);
      }
      .diffdx-consent-link:hover {
        text-decoration: underline;
      }
      .diffdx-consent-link:focus-visible {
        outline: 2px solid var(--teal-primary, #0D9488);
        outline-offset: 2px;
      }
      .diffdx-consent-links-sep {
        color: var(--border-card, #CBD5E1);
        font-size: 10px;
        user-select: none;
      }
      html.dark-theme .diffdx-consent-links-sep {
        color: rgba(150, 180, 210, 0.2);
      }

      .diffdx-consent-checks {
        display: flex;
        flex-direction: column;
        gap: 10px;
        padding: 2px 0 0;
      }
      .diffdx-consent-check-item {
        display: flex;
        align-items: flex-start;
        gap: 10px;
        cursor: pointer;
        user-select: none;
      }
      .diffdx-consent-checkbox {
        margin-top: 2px;
        width: 17px;
        height: 17px;
        border-radius: 4px;
        border: 1.5px solid var(--border-card, #94A3B8);
        accent-color: var(--teal-primary, #0D9488);
        cursor: pointer;
        flex-shrink: 0;
      }
      html.dark-theme .diffdx-consent-checkbox {
        border-color: rgba(150, 180, 210, 0.35);
        accent-color: var(--teal-primary, #19C6AD);
      }
      .diffdx-consent-checkbox:focus-visible {
        outline: 2px solid var(--teal-primary, #0D9488);
        outline-offset: 2px;
      }
      .diffdx-consent-check-label {
        font-size: 12px;
        line-height: 1.45;
        color: var(--text-main, #1E293B);
      }
      html.dark-theme .diffdx-consent-check-label {
        color: var(--text-main, #CBD5E1);
      }

      .diffdx-consent-footer {
        padding: 16px 24px 20px;
        border-top: 1px solid var(--border-card, #E2E8F0);
        display: flex;
        align-items: center;
        justify-content: flex-end;
        gap: 12px;
        background: var(--bg-card, #FFFFFF);
      }
      html.dark-theme .diffdx-consent-footer {
        border-top-color: var(--border-card, rgba(150, 180, 210, 0.12));
        background: var(--bg-card, #0A1525);
      }

      .diffdx-consent-btn-cancel {
        background: transparent;
        border: 1px solid var(--border-card, #CBD5E1);
        color: var(--text-muted, #475569);
        padding: 9px 18px;
        border-radius: 8px;
        font-family: var(--font-heading, 'Sora', sans-serif);
        font-size: 13px;
        font-weight: 600;
        cursor: pointer;
        transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease;
      }
      html.dark-theme .diffdx-consent-btn-cancel {
        border-color: rgba(150, 180, 210, 0.2);
        color: var(--text-muted, #8FA2B9);
      }
      .diffdx-consent-btn-cancel:hover {
        background: rgba(100, 116, 139, 0.08);
        color: var(--text-main, #0F172A);
      }
      html.dark-theme .diffdx-consent-btn-cancel:hover {
        background: rgba(255, 255, 255, 0.06);
        color: var(--text-main, #EAF2FA);
      }
      .diffdx-consent-btn-cancel:focus-visible {
        outline: 2px solid var(--teal-primary, #0D9488);
        outline-offset: 2px;
      }

      .diffdx-consent-btn-primary {
        background: var(--teal-primary, #0D9488);
        border: 1px solid transparent;
        color: #FFFFFF;
        padding: 9px 20px;
        border-radius: 8px;
        font-family: var(--font-heading, 'Sora', sans-serif);
        font-size: 13px;
        font-weight: 700;
        cursor: pointer;
        transition: opacity 0.15s ease;
      }
      html.dark-theme .diffdx-consent-btn-primary {
        background: var(--teal-primary, #19C6AD);
        color: #050D18;
      }
      .diffdx-consent-btn-primary:hover:not(:disabled) {
        opacity: 0.92;
      }
      .diffdx-consent-btn-primary:disabled {
        opacity: 0.45;
        cursor: not-allowed;
      }
      .diffdx-consent-btn-primary:focus-visible {
        outline: 2px solid var(--teal-primary, #0D9488);
        outline-offset: 2px;
      }

      @media (max-width: 600px) {
        .diffdx-consent-overlay {
          padding: 10px;
        }
        .diffdx-consent-modal {
          max-height: calc(100vh - 20px);
          border-radius: 14px;
        }
        .diffdx-consent-header {
          padding: 16px 16px 12px;
        }
        .diffdx-consent-title {
          font-size: 17px;
        }
        .diffdx-consent-body {
          padding: 14px 16px;
        }
        .diffdx-consent-scrollbox {
          max-height: 180px;
          padding: 12px 14px;
        }
        .diffdx-consent-footer {
          padding: 12px 16px 16px;
          flex-direction: column-reverse;
          gap: 8px;
        }
        .diffdx-consent-btn-cancel,
        .diffdx-consent-btn-primary {
          width: 100%;
          text-align: center;
          padding: 11px 16px;
        }
      }
    `;
    document.head.appendChild(style);
  }

  function _buildModalDOM() {
    let overlay = document.getElementById('diffdx-consent-overlay');
    if (overlay) return overlay;

    _injectStyles();

    overlay = document.createElement('div');
    overlay.id = 'diffdx-consent-overlay';
    overlay.className = 'diffdx-consent-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-labelledby', 'diffdx-consent-title');
    overlay.setAttribute('aria-describedby', 'diffdx-consent-subtitle');
    overlay.style.display = 'none';

    overlay.innerHTML = `
      <div class="diffdx-consent-modal" id="diffdx-consent-modal">
        <!-- Header -->
        <div class="diffdx-consent-header">
          <div class="diffdx-consent-icon-badge" aria-hidden="true">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
              <path d="M9 12l2 2 4-4"/>
            </svg>
          </div>
          <div class="diffdx-consent-title-col">
            <h2 class="diffdx-consent-title" id="diffdx-consent-title">Before You Begin</h2>
            <p class="diffdx-consent-subtitle" id="diffdx-consent-subtitle">Please review the following important information before using DiffDx.</p>
          </div>
        </div>

        <!-- Body with scrollable content -->
        <div class="diffdx-consent-body">
          <div class="diffdx-consent-scrollbox" id="diffdx-consent-scrollbox" tabindex="0" role="region" aria-label="Medical Disclaimer & Terms Content">
            <!-- Section 1: AI-Assisted Information -->
            <div class="diffdx-consent-section" id="consent-sec-ai">
              <h3 class="diffdx-consent-section-title">AI-Assisted Information</h3>
              <p class="diffdx-consent-section-text">DiffDx uses artificial intelligence to analyze the information you provide and generate possible conditions that may be relevant to your symptoms. The results are intended to help you understand your situation and prepare for a discussion with a qualified healthcare professional.</p>
            </div>

            <!-- Section 2: Not a Medical Diagnosis -->
            <div class="diffdx-consent-section" id="consent-sec-not-dx">
              <h3 class="diffdx-consent-section-title">Not a Medical Diagnosis</h3>
              <p class="diffdx-consent-section-text">DiffDx does <strong>not</strong> provide a definitive medical diagnosis. AI-generated results may be incomplete, incorrect, or inaccurate. Do not treat the results as medical advice or rely solely on them when making decisions about your health.</p>
            </div>

            <!-- Section 3: Consult a Doctor -->
            <div class="diffdx-consent-section" id="consent-sec-consult">
              <h3 class="diffdx-consent-section-title">Consult a Doctor</h3>
              <p class="diffdx-consent-section-text">Always consult a qualified doctor or appropriate healthcare professional for diagnosis, treatment, medication, or medical advice. If you are experiencing a medical emergency or severe symptoms, seek immediate medical attention instead of relying on DiffDx.</p>
            </div>

            <!-- Section 4: Accuracy & Limitations -->
            <div class="diffdx-consent-section" id="consent-sec-accuracy">
              <h3 class="diffdx-consent-section-title">Accuracy & Limitations</h3>
              <p class="diffdx-consent-section-text">AI systems can misunderstand symptoms, miss relevant conditions, or suggest conditions that may not apply to you. Results are provided for informational purposes only and are <strong>not guaranteed to be accurate or complete</strong>.</p>
            </div>

            <!-- Section 5: Your Data -->
            <div class="diffdx-consent-section" id="consent-sec-data">
              <h3 class="diffdx-consent-section-title">Your Data</h3>
              <p class="diffdx-consent-section-text">The information you provide may include personal and health-related information required to operate the service. Such information may be stored and processed according to the application's privacy practices.</p>
            </div>

            <!-- Section 6: AI Model Improvement & Training -->
            <div class="diffdx-consent-section" id="consent-sec-training">
              <h3 class="diffdx-consent-section-title">AI Model Improvement & Training</h3>
              <p class="diffdx-consent-section-text">Where applicable and where permitted by the application's privacy policy and your consent, submitted information may be used to evaluate, improve, or train DiffDx's AI models.</p>
            </div>
          </div>

          <!-- Legal / Info links -->
          <div class="diffdx-consent-links" aria-label="Legal document references">
            <button type="button" class="diffdx-consent-link" id="diffdx-link-terms">Terms & Conditions</button>
            <span class="diffdx-consent-links-sep" aria-hidden="true">•</span>
            <button type="button" class="diffdx-consent-link" id="diffdx-link-privacy">Privacy Policy</button>
            <span class="diffdx-consent-links-sep" aria-hidden="true">•</span>
            <button type="button" class="diffdx-consent-link" id="diffdx-link-disclaimer">Medical Disclaimer</button>
          </div>

          <!-- Required Checkboxes -->
          <div class="diffdx-consent-checks">
            <label class="diffdx-consent-check-item" for="consent-check-medical">
              <input type="checkbox" id="consent-check-medical" class="diffdx-consent-checkbox" />
              <span class="diffdx-consent-check-label">I understand that DiffDx is an AI-assisted tool and is not a substitute for professional medical advice, diagnosis, or treatment.</span>
            </label>
            <label class="diffdx-consent-check-item" for="consent-check-data">
              <input type="checkbox" id="consent-check-data" class="diffdx-consent-checkbox" />
              <span class="diffdx-consent-check-label">I understand and agree that the information I provide may be processed and, where applicable, used to improve or train DiffDx's AI models as described in the Privacy Policy.</span>
            </label>
          </div>
        </div>

        <!-- Footer Actions -->
        <div class="diffdx-consent-footer">
          <button type="button" class="diffdx-consent-btn-cancel" id="diffdx-consent-cancel-btn">Cancel</button>
          <button type="button" class="diffdx-consent-btn-primary" id="diffdx-consent-confirm-btn" disabled>I Understand & Continue</button>
        </div>
      </div>
    `;

    document.body.appendChild(overlay);

    // Event listeners
    const cb1 = document.getElementById('consent-check-medical');
    const cb2 = document.getElementById('consent-check-data');
    const confirmBtn = document.getElementById('diffdx-consent-confirm-btn');
    const cancelBtn = document.getElementById('diffdx-consent-cancel-btn');
    const scrollBox = document.getElementById('diffdx-consent-scrollbox');

    function updateConfirmButtonState() {
      confirmBtn.disabled = !(cb1.checked && cb2.checked);
    }
    cb1.addEventListener('change', updateConfirmButtonState);
    cb2.addEventListener('change', updateConfirmButtonState);

    // Quick navigation links inside terms box
    function scrollToSection(id) {
      const target = document.getElementById(id);
      if (target && scrollBox) {
        scrollBox.scrollTop = target.offsetTop - scrollBox.offsetTop - 8;
        target.classList.add('highlighted');
        setTimeout(() => target.classList.remove('highlighted'), 1200);
      }
    }
    document.getElementById('diffdx-link-terms').addEventListener('click', () => scrollToSection('consent-sec-ai'));
    document.getElementById('diffdx-link-privacy').addEventListener('click', () => scrollToSection('consent-sec-data'));
    document.getElementById('diffdx-link-disclaimer').addEventListener('click', () => scrollToSection('consent-sec-not-dx'));

    confirmBtn.addEventListener('click', () => {
      if (confirmBtn.disabled) return;
      recordConsent();
      hideModal();
      if (_activeResolve) {
        const fn = _activeResolve;
        _activeResolve = null;
        _activeReject = null;
        fn(true);
      }
    });

    cancelBtn.addEventListener('click', () => {
      hideModal();
      if (_activeReject) {
        const fn = _activeReject;
        _activeResolve = null;
        _activeReject = null;
        fn(false);
      }
    });

    // Close on backdrop click
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) {
        cancelBtn.click();
      }
    });

    // Keyboard accessibility: Escape to cancel, Tab trapping
    document.addEventListener('keydown', (e) => {
      if (overlay.style.display === 'none') return;
      if (e.key === 'Escape') {
        e.preventDefault();
        cancelBtn.click();
        return;
      }
      if (e.key === 'Tab') {
        const focusables = overlay.querySelectorAll('button, input[type="checkbox"], [tabindex="0"]');
        if (!focusables.length) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    });

    return overlay;
  }

  function showModal(options = {}) {
    const overlay = _buildModalDOM();
    const cb1 = document.getElementById('consent-check-medical');
    const cb2 = document.getElementById('consent-check-data');
    const confirmBtn = document.getElementById('diffdx-consent-confirm-btn');

    // Reset checkboxes
    cb1.checked = false;
    cb2.checked = false;
    confirmBtn.disabled = true;

    _previousActiveElement = document.activeElement;
    overlay.style.display = 'flex';
    document.body.style.overflow = 'hidden';

    // Focus first interactive element
    setTimeout(() => {
      cb1.focus();
    }, 50);

    return new Promise((resolve, reject) => {
      _activeResolve = (val) => {
        if (typeof options.onAccept === 'function') options.onAccept(val);
        resolve(val);
      };
      _activeReject = (val) => {
        if (typeof options.onCancel === 'function') options.onCancel(val);
        resolve(false);
      };
    });
  }

  function hideModal() {
    const overlay = document.getElementById('diffdx-consent-overlay');
    if (overlay) {
      overlay.style.display = 'none';
      document.body.style.overflow = '';
      if (_previousActiveElement && typeof _previousActiveElement.focus === 'function') {
        _previousActiveElement.focus();
      }
    }
  }

  /**
   * For registered users: checks if current user has already consented.
   * If not, presents the modal.
   */
  function checkRegisteredUserConsent(userId) {
    if (!userId) {
      const u = _getUser();
      userId = u ? (u.id || u.email) : null;
    }
    if (!userId) return;
    const key = 'diffdx_consent_' + userId;
    if (localStorage.getItem(key)) return; // Already consented
    showModal({
      mode: 'registered'
    });
  }

  window.DiffDxConsent = {
    hasConsented,
    recordConsent,
    show: showModal,
    hide: hideModal,
    checkRegisteredUserConsent
  };
})();
