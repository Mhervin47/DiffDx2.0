/*
 * admin-common.js — shared fetch helper + minimal nav for the admin portal (Phase 1).
 * Every page in web/admin_portal/ loads this before its own page script.
 */
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
    { href: "audit.html", label: "Audit" },
    { href: "dsr.html", label: "DSR" },
    { href: "reports.html", label: "Reports" },
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

  function fmtUsd(n, digits = 5) {
    if (n === null || n === undefined) return "—";
    return `$${Number(n).toFixed(digits)}`;
  }

  function fmtFraction(block) {
    // {correct, total, pct} -> "13/20 (65.0%)"
    if (!block || block.total === undefined) return "—";
    const pct = block.pct === null || block.pct === undefined ? "—" : fmtPct(block.pct);
    return `${block.correct}/${block.total} (${pct})`;
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
    renderAuthRequired, mountNav, fmtPct, fmtNum, fmtUsd, fmtFraction, el,
  };
})();
