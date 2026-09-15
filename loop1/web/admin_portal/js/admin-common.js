/*
 * admin-common.js — shared fetch helper + minimal nav for the admin portal (Phase 1).
 * Every page in web/admin_portal/ loads this before its own page script.
 */

// Role guard — admin-only, every page in this directory. The /api/admin/*
// JSON endpoints already enforce require_role("admin") server-side (a
// fetch() call carries the Authorization header fine), but the page SHELLS
// themselves (this HTML file, this JS file) were served completely
// unauthenticated — a patient or doctor, or a signed-out visitor, could
// load any admin_portal page and see its layout (though not its real data,
// which still 401s). A server-side Depends(require_role(...)) on the page
// ROUTE can't fix this: bearer-token auth has no header on a plain browser
// navigation (only fetch()/XHR calls carry one), so that would 401 every
// legitimate admin landing here too — same reasoning as every page guard
// elsewhere in this app (see diffdx.routers.pages's module docstring).
// This is the one mechanism that actually works: check the cached role,
// redirect before anything else on the page runs.
(function () {
  try {
    const authUser = typeof getAuthUser === "function" ? getAuthUser() : null;
    if (!authUser) {
      window.location.replace("/login.html?next=" + encodeURIComponent(location.pathname));
    } else if (authUser.role !== "admin") {
      window.location.replace("/");
    }
  } catch (e) {
    window.location.replace("/login.html");
  }
})();

const AdminPortal = (() => {
  // Phase 2's /api/admin/* endpoints (health/usage/audit/subjects) are
  // behind require_role("admin") — reuses the exact same JWT the rest of
  // the app already issues via /login.html, read from the shared /auth.js
  // (window.getAuthToken / authHeaders), which every admin_portal page
  // loads before this file. Phase 1's endpoints (evidence/quality/config)
  // are unauthenticated, so sending this header there is harmless — they
  // just ignore it.
  function _authHeader() {
    try {
      if (typeof getAuthToken === "function") {
        const t = getAuthToken();
        if (t) return { Authorization: `Bearer ${t}` };
      }
    } catch (e) {
      // getAuthToken() itself threw — degrade to an unauthenticated fetch
      // rather than breaking the page. Every admin_portal page loads
      // /auth.js before this file (evidence.html/quality.html included,
      // now that their data is admin-gated too), so getAuthToken existing
      // is the expected case, not an exception to plan around.
    }
    return {};
  }

  async function fetchJSON(path) {
    try {
      const res = await fetch(path, { headers: { Accept: "application/json", ..._authHeader() } });
      if (res.status === 401 || res.status === 403) {
        return { status: "auth_required", httpStatus: res.status };
      }
      if (!res.ok) {
        return { status: "error", message: `${path} returned HTTP ${res.status}` };
      }
      return await res.json();
    } catch (err) {
      return { status: "error", message: `Failed to reach ${path}: ${err.message}` };
    }
  }

  function isNotGenerated(data) {
    return !!data && data.status === "not_generated";
  }

  function isError(data) {
    return !!data && data.status === "error";
  }

  function isAuthRequired(data) {
    return !!data && data.status === "auth_required";
  }

  // Consistent "not signed in" state for any Phase 2 panel — deliberately
  // NOT the same as the mock-data fallback: showing sample numbers when the
  // real reason is "you aren't logged in as an admin" would be misleading,
  // not just incomplete.
  function renderAuthRequired(container, { what = "this panel" } = {}) {
    container.innerHTML = "";
    container.appendChild(
      el("div", { class: "auth-required" }, [
        el("p", {}, `Sign in as an admin to view ${what}.`),
        el("a", { href: "/login.html", class: "auth-required-link" }, "Sign in"),
      ])
    );
  }

  const NAV_LINKS = [
    { href: "index.html", label: "Overview" },
    { href: "evidence.html", label: "Evidence" },
    { href: "quality.html", label: "AI Quality" },
    { href: "architecture-validation.html", label: "Architecture Validation" },
    { href: "audit.html", label: "Audit" },
    { href: "dsr.html", label: "DSR" },
    { href: "reports.html", label: "Reports" },
    { href: "sop.html", label: "SOP" },
  ];

  function mountNav(activeHref) {
    const root = document.getElementById("admin-nav-root");
    if (!root) return;
    const nav = document.createElement("nav");
    nav.className = "admin-nav";
    nav.innerHTML = NAV_LINKS.map(
      (l) =>
        `<a href="${l.href}" class="${l.href === activeHref ? "active" : ""}">${l.label}</a>`
    ).join("");
    root.appendChild(nav);
  }

  function fmtPct(fraction, digits = 1) {
    if (fraction === null || fraction === undefined) return "—";
    return `${(fraction * 100).toFixed(digits)}%`;
  }

  function fmtNum(n, digits = 0) {
    if (n === null || n === undefined) return "—";
    return Number(n).toFixed(digits);
  }

  // Currency toggle — USD (native, every backend figure is computed in
  // USD) or INR display-only conversion at a fixed rate. Persisted across
  // pages/reloads via localStorage; every page's mountCurrencyToggle()
  // reads the same key, so switching on one page holds on every other.
  // Conversion is display-only: nothing is recomputed or re-fetched, only
  // reformatted, so there's no risk of a stale/mismatched rate baked into
  // stored data.
  const _CURRENCY_KEY = "adminPortalCurrency";
  const _USD_TO_INR = 95.86;

  function getCurrency() {
    try {
      const v = localStorage.getItem(_CURRENCY_KEY);
      return v === "INR" ? "INR" : "USD";
    } catch (e) {
      return "USD";
    }
  }

  function setCurrency(cur) {
    try {
      localStorage.setItem(_CURRENCY_KEY, cur === "INR" ? "INR" : "USD");
    } catch (e) {
      // localStorage unavailable (private mode etc.) — currency just won't
      // persist across a reload; not fatal.
    }
  }

  function currencySymbol() {
    return getCurrency() === "INR" ? "₹" : "$";
  }

  // Converts a raw USD number to the currently selected display currency.
  // Every dollar figure this app computes (cost_per_session_usd_estimated,
  // pricing.json rates, etc.) is USD at the source — this never touches
  // that, only what's shown on screen.
  function convertAmount(n) {
    if (n === null || n === undefined) return null;
    return getCurrency() === "INR" ? Number(n) * _USD_TO_INR : Number(n);
  }

  function fmtUsd(n, digits = 5) {
    if (n === null || n === undefined) return "—";
    return `${currencySymbol()}${convertAmount(n).toFixed(digits)}`;
  }

  // Injected into every admin_portal page's topbar (next to the theme
  // toggle) rather than duplicated as static markup in seven HTML files —
  // one place to change, and every page picks it up the same way mountNav()
  // and the admin role guard already do.
  function mountCurrencyToggle() {
    const actions = document.querySelector(".topbar-actions");
    if (!actions || document.getElementById("currency-toggle")) return;
    const btn = document.createElement("button");
    btn.id = "currency-toggle";
    btn.type = "button";
    btn.className = "topbar-btn currency-toggle-btn";
    btn.title = "Convert displayed cost figures to INR (1 USD = 95.86 INR)";
    const render = () => {
      const cur = getCurrency();
      btn.textContent = cur === "INR" ? "₹ INR" : "$ USD";
      btn.setAttribute("aria-label", `Currency: ${cur}. Click to switch to ${cur === "INR" ? "USD" : "INR"}.`);
    };
    render();
    btn.addEventListener("click", () => {
      setCurrency(getCurrency() === "INR" ? "USD" : "INR");
      // Every cost figure on the page came from a render pass keyed off
      // fmtUsd()/convertAmount() at fetch/poll time, scattered across each
      // page's own script — a full reload re-runs those passes uniformly
      // instead of needing a change-listener wired into every one of them.
      location.reload();
    });
    const themeToggle = document.getElementById("theme-toggle");
    if (themeToggle && themeToggle.parentNode === actions) {
      actions.insertBefore(btn, themeToggle);
    } else {
      actions.insertBefore(btn, actions.firstChild);
    }
  }

  function fmtFraction(block) {
    // {correct, total, pct} -> "13/20 (65.0%)"
    if (!block || block.total === undefined) return "—";
    const pct = block.pct === null || block.pct === undefined ? "—" : fmtPct(block.pct);
    return `${block.correct}/${block.total} (${pct})`;
  }

  // Collapsed-by-default "Read more" disclosure for a long explanatory note
  // (coverage notes, methodology caveats) — keeps the card scannable while
  // still making the full explanation one click away, instead of always
  // rendering the whole paragraph inline. `extraNotes` (ephemerality/p95/
  // model-coverage caveats etc.) render inside the SAME collapsed block —
  // rendering them as separate always-visible paragraphs right next to a
  // collapsed summary defeats the point, since there'd still be explanatory
  // text visible without clicking anything.
  function renderCoverageNote(container, text, { summary = "Read more — how this is calculated", extraNotes = [] } = {}) {
    if (!text && !(extraNotes && extraNotes.length)) return;
    const children = [el("summary", {}, summary)];
    if (text) children.push(el("p", { class: "usage-coverage-note" }, text));
    for (const note of extraNotes || []) {
      if (note) children.push(el("p", { class: "usage-coverage-note" }, note));
    }
    const details = el("details", { class: "coverage-details" }, children);
    container.appendChild(details);
  }

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else node.setAttribute(k, v);
    }
    for (const c of [].concat(children)) {
      if (c === null || c === undefined) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  }

  // The graceful "not real data" fallback, built in once here rather than
  // duplicated per page: falls back to mock data whenever a real, generated
  // ("ok") response isn't available — whether that's a running backend
  // reporting "not_generated"/"no_data", or no backend at all (these pages
  // must also render standalone, with no server reachable).
  //
  // "auth_required" is deliberately NOT folded into the mock fallback —
  // showing sample numbers when the real reason is "you aren't signed in
  // as an admin" would be misleading, not just incomplete. Callers must
  // check result.authRequired first and render renderAuthRequired() before
  // falling through to the usingMock branch.
  async function fetchWithMockFallback(path, mockData) {
    const data = await fetchJSON(path);
    if (isAuthRequired(data)) {
      return { authRequired: true, data: null, usingMock: false };
    }
    if (!data || data.status !== "ok") {
      return { authRequired: false, data: mockData, usingMock: true };
    }
    return { authRequired: false, data, usingMock: false };
  }

  return {
    fetchJSON, fetchWithMockFallback, isNotGenerated, isError, isAuthRequired,
    renderAuthRequired, renderCoverageNote, mountNav, mountCurrencyToggle,
    getCurrency, setCurrency, currencySymbol, convertAmount,
    fmtPct, fmtNum, fmtUsd, fmtFraction, el,
  };
})();

AdminPortal.mountCurrencyToggle();
