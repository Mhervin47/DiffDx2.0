/*
 * evidence.js — renders /api/admin/evidence (or mock.js sample data) into
 * the page.
 *
 * /api/admin/evidence now requires require_role("admin") (project owner's
 * explicit decision — every /api/admin/* route is admin-gated consistently;
 * this page was originally public by design, see the route's own docstring
 * for the history). Unauthenticated visitors get the same "sign in as
 * admin" prompt every other admin_portal page already uses, not a crash on
 * a null report.
 *
 * Design-pass rewrite (evidence_quality_redesign_prompt.md): the page used
 * to render every stat at the same visual weight — a grid of same-size
 * number cards. This version groups by the question a viewer actually has
 * ("Is it more accurate? Cost-effective? Safer?"), with one hero stat per
 * section and everything else demoted to supporting detail. Every existing
 * caveat/note still renders — see the "Methodology & statistical notes"
 * collapsed section — none were removed, only relocated.
 */
(async function () {
  const { fetchWithMockFallback, renderAuthRequired, fmtPct, fmtUsd, fmtFraction, el } = AdminPortal;

  const SYSTEM_LABELS = {
    actor_critic: "Actor-Critic",
    baseline_mode_a: "Baseline — Mode A (matched interaction)",
    baseline_mode_b: "Baseline — Mode B (matched information)",
  };
  const SYSTEM_LABELS_SHORT = {
    actor_critic: "Actor-Critic",
    baseline_mode_a: "Mode A",
    baseline_mode_b: "Mode B",
  };
  const SYSTEM_COLORS = { actor_critic: "var(--teal-primary)", baseline_mode_a: "var(--blue-secondary)", baseline_mode_b: "var(--purple-accent)" };
  const BASELINE_KEYS = ["baseline_mode_a", "baseline_mode_b"];

  const result = await fetchWithMockFallback(
    "/api/admin/evidence",
    AdminPortalMock.evidenceReport
  );

  if (result.authRequired) {
    renderAuthRequired(document.getElementById("content"), { what: "the evidence report" });
    return;
  }

  const { data: report } = result;

  document.getElementById("eval-set-size").textContent = report.eval_set_size;
  document.getElementById("generated-at").textContent = report.generated_at;

  renderStaticBadge(report);
  renderAccuracySection(report);
  renderCostSection(report);
  renderSafetySection(report);
  renderMethodology(report);

  // Item 8 — unmistakable that this is a point-in-time offline benchmark,
  // not something that updates on its own (pre-empts "why isn't this
  // changing" — see index.html/audit.html for what DOES update live).
  function renderStaticBadge(report) {
    const root = document.getElementById("static-badge-root");
    if (!root) return;
    root.appendChild(
      el("span", { class: "static-badge" }, `Offline benchmark — n=${report.eval_set_size} — not live`)
    );
  }

  // ---------------------------------------------------------------------
  // Shared hero bar chart — one metric, all three systems, fixed 0-100%
  // y-axis (never a truncated/non-zero baseline). `advanced: true` gets the
  // gradient fill + soft drop-shadow filter (reserved for one hero chart
  // per page); the safety chart uses the lighter variant for visual family
  // consistency without stacking the same depth cue on every chart.
  // ---------------------------------------------------------------------
  function renderPctBarChart(root, idPrefix, entries, { advanced = false } = {}) {
    const svgNS = "http://www.w3.org/2000/svg";
    // Wide aspect ratio (~3.8:1) — at the CSS max-height cap this fills a
    // ~1100px-wide desktop container without letterboxing, and still holds
    // up narrower (viewBox scales down as one unit, bars stay proportional).
    const width = 760, height = 200, pad = 34, barW = 100, gap = 60;
    const totalBarsW = entries.length * barW + (entries.length - 1) * gap;
    const startX = (width - totalBarsW) / 2;

    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("class", "hero-chart-svg");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", entries.map((e) => `${e.label}: ${e.value == null ? "no data" : Math.round(e.value * 100) + "%"}`).join(", "));

    const defs = document.createElementNS(svgNS, "defs");
    const gradId = `${idPrefix}-grad`;
    const grad = document.createElementNS(svgNS, "linearGradient");
    grad.setAttribute("id", gradId);
    grad.setAttribute("x1", "0"); grad.setAttribute("y1", "0"); grad.setAttribute("x2", "0"); grad.setAttribute("y2", "1");
    const stop1 = document.createElementNS(svgNS, "stop");
    stop1.setAttribute("offset", "0%"); stop1.setAttribute("stop-color", "var(--teal-primary)"); stop1.setAttribute("stop-opacity", "1");
    const stop2 = document.createElementNS(svgNS, "stop");
    stop2.setAttribute("offset", "100%"); stop2.setAttribute("stop-color", "var(--teal-primary)"); stop2.setAttribute("stop-opacity", "0.55");
    grad.appendChild(stop1); grad.appendChild(stop2);
    defs.appendChild(grad);

    let filterId = null;
    if (advanced) {
      filterId = `${idPrefix}-shadow`;
      const filter = document.createElementNS(svgNS, "filter");
      filter.setAttribute("id", filterId);
      filter.setAttribute("x", "-40%"); filter.setAttribute("y", "-40%");
      filter.setAttribute("width", "180%"); filter.setAttribute("height", "180%");
      const feDrop = document.createElementNS(svgNS, "feDropShadow");
      feDrop.setAttribute("dx", "0"); feDrop.setAttribute("dy", "3"); feDrop.setAttribute("stdDeviation", "5");
      feDrop.setAttribute("flood-color", "#0D9488"); feDrop.setAttribute("flood-opacity", "0.35");
      filter.appendChild(feDrop);
      defs.appendChild(filter);
    }
    svg.appendChild(defs);

    // Zero-baseline axis line — the chart's floor is always real zero.
    const axisY = height - pad;
    const axis = document.createElementNS(svgNS, "line");
    axis.setAttribute("x1", startX - 8); axis.setAttribute("x2", startX + totalBarsW + 8);
    axis.setAttribute("y1", axisY); axis.setAttribute("y2", axisY);
    axis.setAttribute("class", "hero-chart-baseline");
    svg.appendChild(axis);

    const bars = [];
    entries.forEach((entry, i) => {
      const x = startX + i * (barW + gap);
      const maxH = axisY - 14;
      const frac = entry.value == null ? 0 : Math.max(0, Math.min(1, entry.value));
      const barH = Math.max(entry.value == null ? 0 : 4, frac * maxH);
      const y = axisY - barH;

      const rect = document.createElementNS(svgNS, "rect");
      rect.setAttribute("x", x); rect.setAttribute("y", y);
      rect.setAttribute("width", barW); rect.setAttribute("height", Math.max(barH, 0.1));
      rect.setAttribute("rx", 6);
      rect.setAttribute("class", "hero-bar");
      rect.style.fill = entry.isHero ? `url(#${gradId})` : (entry.color || "var(--text-dim)");
      rect.setAttribute("opacity", entry.value == null ? 0.2 : 1);
      if (advanced && entry.isHero && filterId) rect.setAttribute("filter", `url(#${filterId})`);
      const title = document.createElementNS(svgNS, "title");
      title.textContent = `${entry.label}: ${entry.value == null ? "no data" : fmtPct(entry.value, 1)}`;
      rect.appendChild(title);
      svg.appendChild(rect);
      bars.push(rect);

      if (entry.value != null) {
        const label = document.createElementNS(svgNS, "text");
        label.setAttribute("x", x + barW / 2);
        label.setAttribute("y", y - 10);
        label.setAttribute("text-anchor", "middle");
        label.setAttribute("class", "hero-bar-value");
        label.textContent = fmtPct(entry.value, 0);
        svg.appendChild(label);
      }

      const axisLabel = document.createElementNS(svgNS, "text");
      axisLabel.setAttribute("x", x + barW / 2);
      axisLabel.setAttribute("y", axisY + 20);
      axisLabel.setAttribute("text-anchor", "middle");
      axisLabel.setAttribute("class", "hero-bar-axis-label");
      axisLabel.textContent = entry.shortLabel;
      svg.appendChild(axisLabel);
    });

    root.appendChild(svg);

    // Animated entrance: bars start at scaleY(0) via CSS, revealed as one
    // orchestrated moment (double rAF so the browser paints the 0-state
    // first, then the transition to 1 actually animates instead of
    // snapping straight to the end state).
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        bars.forEach((b) => b.classList.add("revealed"));
      });
    });
  }

  function deltaChip(deltaPp) {
    if (deltaPp == null || Number.isNaN(deltaPp)) return el("span", { class: "ref-delta neutral" }, "—");
    const cls = deltaPp > 0.05 ? "positive" : deltaPp < -0.05 ? "negative" : "neutral";
    const sign = deltaPp >= 0 ? "+" : "";
    return el("span", { class: `ref-delta ${cls}` }, `${sign}${deltaPp.toFixed(1)}pp`);
  }

  function refChip(key, pct, deltaPp) {
    return el("div", { class: "ref-chip" }, [
      el("div", { class: "ref-value" }, pct == null ? "—" : fmtPct(pct, 0)),
      el("div", { class: "ref-label" }, SYSTEM_LABELS_SHORT[key]),
      deltaChip(deltaPp),
    ]);
  }

  // ---------------------------------------------------------------------
  // "Is it more accurate?"
  // ---------------------------------------------------------------------
  function renderAccuracySection(report) {
    const ac = report.systems.actor_critic;
    const acPct = ac ? ac.top1_accuracy.pct : null;

    document.getElementById("accuracy-hero-value").textContent = acPct == null ? "—" : fmtPct(acPct, 0);

    // Takeaway — real numbers, not a caption of the chart.
    const takeaway = document.getElementById("accuracy-takeaway");
    if (ac && acPct != null) {
      let sentence = `Correctly diagnosed ${ac.top1_accuracy.correct} of ${ac.top1_accuracy.total} test cases (${fmtPct(acPct, 0)}) on the first guess.`;
      const deltas = BASELINE_KEYS
        .map((k) => {
          const sig = report.significance[`actor_critic_vs_${k}`];
          return sig ? { key: k, pp: sig.top1_delta_pp } : null;
        })
        .filter(Boolean);
      if (deltas.length) {
        const best = deltas.reduce((a, b) => (Math.abs(b.pp) > Math.abs(a.pp) ? b : a));
        const dir = best.pp >= 0 ? "more" : "fewer";
        sentence += ` That's ${Math.abs(best.pp).toFixed(1)} percentage points ${dir} correct than the ${SYSTEM_LABELS_SHORT[best.key]} baseline.`;
      }
      takeaway.textContent = sentence;
    } else {
      takeaway.textContent = "No actor-critic evaluation data available.";
    }

    // Reference chips + confidence badges, one per existing baseline.
    const refsRoot = document.getElementById("accuracy-hero-refs");
    const badgeRoot = document.getElementById("accuracy-confidence-row");
    for (const key of BASELINE_KEYS) {
      const block = report.systems[key];
      const sig = report.significance[`actor_critic_vs_${key}`];
      refsRoot.appendChild(refChip(key, block ? block.top1_accuracy.pct : null, sig ? sig.top1_delta_pp : null));
      if (sig) {
        badgeRoot.appendChild(
          el("span", { class: "confidence-chip", title: sig.note }, `${SYSTEM_LABELS_SHORT[key]}: directional signal, n=${sig.n_discordant}`)
        );
      }
    }

    // Hero chart — the one chart on this page with the full advanced
    // treatment (gradient + drop-shadow), per the "one hero chart" rule.
    const chartRoot = document.getElementById("accuracy-chart-root");
    const entries = ["actor_critic", ...BASELINE_KEYS].map((key) => {
      const block = report.systems[key];
      return {
        label: SYSTEM_LABELS[key],
        shortLabel: SYSTEM_LABELS_SHORT[key],
        value: block ? block.top1_accuracy.pct : null,
        color: SYSTEM_COLORS[key],
        isHero: key === "actor_critic",
      };
    });
    renderPctBarChart(chartRoot, "acc", entries, { advanced: true });

    // Supporting detail — visually secondary to the hero.
    const subStats = document.getElementById("accuracy-sub-stats");
    if (ac) {
      subStats.appendChild(
        el("div", { class: "sub-stat" }, [
          el("div", { class: "sub-stat-value" }, fmtFraction(ac.top3_accuracy)),
          el("div", { class: "sub-stat-label" }, "Top-3 accuracy — Actor-Critic"),
        ])
      );
      subStats.appendChild(
        el("div", { class: "sub-stat" }, [
          el("div", { class: "sub-stat-value" }, ac.mean_differential_overlap == null ? "—" : ac.mean_differential_overlap.toFixed(2)),
          el("div", { class: "sub-stat-label" }, "Mean differential overlap (Jaccard)"),
        ])
      );
    }

    renderStratumTable(report);
  }

  function renderStratumTable(report) {
    const table = document.getElementById("stratum-table");
    const strata = ["in_pool", "out_of_pool", "ambiguous"];
    const SYSTEM_KEYS = ["actor_critic", ...BASELINE_KEYS];
    const thead = el("thead", {}, [
      el("tr", {}, [
        el("th", {}, "Stratum"),
        ...SYSTEM_KEYS.map((k) => el("th", {}, SYSTEM_LABELS_SHORT[k])),
      ]),
    ]);
    const tbody = el("tbody");
    for (const stratum of strata) {
      const tr = el("tr");
      tr.appendChild(el("td", { class: "metric-label" }, stratum.replace(/_/g, " ")));
      for (const sysKey of SYSTEM_KEYS) {
        const block = report.systems[sysKey];
        const td = el("td");
        if (!block) {
          td.textContent = "no data";
          td.className = "no-data";
        } else {
          const s = block.by_stratum[stratum];
          td.textContent = `top1 ${fmtFraction(s.top1_accuracy)} · top3 ${fmtFraction(s.top3_accuracy)}`;
        }
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(thead);
    table.appendChild(tbody);
  }

  // ---------------------------------------------------------------------
  // "Is it cost-effective?" — deliberately neutral, not teal-glorified.
  // The baseline is genuinely cheaper here (fewer turns, fewer tokens);
  // pretending otherwise with a "win" color would misrepresent a real,
  // explained tradeoff as a caveat-worthy problem or a false victory.
  // ---------------------------------------------------------------------
  function renderCostSection(report) {
    const ac = report.systems.actor_critic;
    const acCost = ac ? ac.mean_cost_per_session_usd_estimated : null;

    const takeaway = document.getElementById("cost-takeaway");
    if (acCost != null) {
      const baselineCosts = BASELINE_KEYS
        .map((k) => report.systems[k] && report.systems[k].mean_cost_per_session_usd_estimated)
        .filter((v) => v != null && v > 0);
      let sentence = `Actor-critic costs an estimated ${fmtUsd(acCost)} per session.`;
      if (baselineCosts.length) {
        const minBaseline = Math.min(...baselineCosts);
        const ratio = acCost / minBaseline;
        sentence += ` That's roughly ${ratio.toFixed(1)}× the cheapest single-shot baseline — expected, since actor-critic runs multiple reasoning turns per session rather than one.`;
      }
      takeaway.textContent = sentence;
    } else {
      takeaway.textContent = "No cost data available for actor-critic.";
    }

    const cardRow = document.getElementById("cost-card-row");
    for (const key of ["actor_critic", ...BASELINE_KEYS]) {
      const block = report.systems[key];
      const cost = block ? block.mean_cost_per_session_usd_estimated : null;
      const isRef = key !== "actor_critic";
      cardRow.appendChild(
        el("div", { class: isRef ? "cost-card is-reference" : "cost-card" }, [
          el("div", { class: "cost-card-value" }, cost == null ? "no data" : fmtUsd(cost)),
          el("div", { class: "cost-card-label" }, SYSTEM_LABELS[key]),
        ])
      );
    }

    const subStats = document.getElementById("cost-sub-stats");
    for (const key of ["actor_critic", ...BASELINE_KEYS]) {
      const block = report.systems[key];
      if (!block) continue;
      const cpc = block.cost_per_correct_diagnosis_usd;
      const stat = el("div", { class: "sub-stat" }, [
        el("div", { class: "sub-stat-value" }, cpc.value == null ? "—" : fmtUsd(cpc.value)),
        el("div", { class: "sub-stat-label" }, `Cost per correct diagnosis — ${SYSTEM_LABELS_SHORT[key]}`),
      ]);
      if (cpc.note) {
        stat.querySelector(".sub-stat-value").appendChild(el("span", { class: "caveat", title: cpc.note }, " ⚠"));
      }
      subStats.appendChild(stat);
    }
    if (ac) {
      subStats.appendChild(
        el("div", { class: "sub-stat" }, [
          el("div", { class: "sub-stat-value" }, ac.mean_prompt_tokens_per_session == null ? "—" : Math.round(ac.mean_prompt_tokens_per_session).toLocaleString()),
          el("div", { class: "sub-stat-label" }, "Mean prompt tokens / session"),
        ])
      );
      const compStat = el("div", { class: "sub-stat" }, [
        el("div", { class: "sub-stat-value" }, ac.mean_completion_tokens_estimated_per_session == null ? "—" : Math.round(ac.mean_completion_tokens_estimated_per_session).toLocaleString()),
        el("div", { class: "sub-stat-label" }, "Mean completion tokens / session (estimated)"),
      ]);
      subStats.appendChild(compStat);
    }
  }

  // ---------------------------------------------------------------------
  // "Is it safer?"
  // ---------------------------------------------------------------------
  function renderSafetySection(report) {
    const s = report.safety;
    if (!s) {
      document.getElementById("safety-fallback").appendChild(
        el("p", { class: "muted" }, "Safety recall not computed — no red_flag_labels.json found.")
      );
      document.querySelectorAll("#safety-section-block .hero-row, #safety-section-block #safety-chart-root, #safety-section-block .section-sub-stats").forEach((elm) => (elm.style.display = "none"));
      document.getElementById("safety-takeaway").textContent = "Safety recall has not been computed for this eval set.";
      return;
    }

    const recall = s.red_flag_top3_recall;
    const ac = recall.actor_critic;

    document.getElementById("safety-hero-value").textContent = ac && ac.pct != null ? fmtPct(ac.pct, 0) : "—";

    // Same plain-English framing as quality.js's safety summary (that page
    // fetches this same endpoint purely to render this sentence — kept
    // consistent rather than diverging in wording).
    const takeaway = document.getElementById("safety-takeaway");
    const baseline = recall.baseline_mode_a || recall.baseline_mode_b;
    if (baseline && ac && ac.total != null) {
      const caughtMore = ac.correct - baseline.correct;
      takeaway.textContent =
        caughtMore > 0
          ? `Caught ${caughtMore} more high-acuity case${caughtMore === 1 ? "" : "s"} than the baseline, out of ${ac.total} test patients with a red-flag feature.`
          : caughtMore < 0
          ? `Caught ${Math.abs(caughtMore)} fewer high-acuity case${Math.abs(caughtMore) === 1 ? "" : "s"} than the baseline, out of ${ac.total} test patients with a red-flag feature.`
          : `Caught the same number of high-acuity cases as the baseline (${ac.correct} of ${ac.total} test patients with a red-flag feature).`;
    } else {
      takeaway.textContent = `${ac ? ac.correct : 0} of ${ac ? ac.total : 0} high-acuity (red-flag) test patients caught in the top-3 differential.`;
    }

    const refsRoot = document.getElementById("safety-hero-refs");
    for (const key of BASELINE_KEYS) {
      const r = recall[key];
      const deltaPp = ac && r && ac.pct != null && r.pct != null ? (ac.pct - r.pct) * 100 : null;
      refsRoot.appendChild(refChip(key, r ? r.pct : null, deltaPp));
    }

    const chartRoot = document.getElementById("safety-chart-root");
    const entries = ["actor_critic", ...BASELINE_KEYS].map((key) => ({
      label: SYSTEM_LABELS[key],
      shortLabel: SYSTEM_LABELS_SHORT[key],
      value: recall[key] ? recall[key].pct : null,
      color: SYSTEM_COLORS[key],
      isHero: key === "actor_critic",
    }));
    renderPctBarChart(chartRoot, "safety", entries, { advanced: false });

    const subStats = document.getElementById("safety-sub-stats");
    if (s.critic_missed_red_flag_rate) {
      const cr = s.critic_missed_red_flag_rate;
      subStats.appendChild(
        el("div", { class: "sub-stat" }, [
          el("div", { class: "sub-stat-value" }, `${fmtPct(cr.value)} (of ${cr.total})`),
          el("div", { class: "sub-stat-label" }, "Critic-flagged missed-red-flag rate — available for actor-critic only"),
        ])
      );
    }

    // Trust-critical, kept visible (not collapsed into Methodology) — the
    // README/module docstring both stress checking this before trusting
    // any safety-recall number above.
    document.getElementById("safety-provenance").textContent =
      `Label provenance: ${s.label_provenance.automatic} automatic (${s.label_provenance.automatic_source}), ` +
      `${s.label_provenance.override} real clinician override(s). Safety-recall credibility depends entirely on this.`;
  }

  // ---------------------------------------------------------------------
  // Methodology & statistical notes — progressive disclosure. Nothing
  // here is new; it's every caveat the old flat layout already showed,
  // relocated and reachable via one click instead of competing with the
  // headline numbers for attention.
  // ---------------------------------------------------------------------
  function renderMethodology(report) {
    const SYSTEM_KEYS = ["actor_critic", ...BASELINE_KEYS];
    const body = document.getElementById("methodology-body");

    body.appendChild(el("div", { class: "methodology-subhead" }, "Report notes"));
    const notesList = el("ul", { id: "notes-list" });
    for (const note of report.notes) notesList.appendChild(el("li", {}, note));
    body.appendChild(notesList);

    body.appendChild(el("div", { class: "methodology-subhead" }, "Significance (exact McNemar)"));
    const sigTable = el("table", { class: "metrics-table" });
    sigTable.appendChild(el("thead", {}, el("tr", {}, [
      el("th", {}, "Comparison"), el("th", {}, "p-value"), el("th", {}, "Δ top-1"), el("th", {}, "n"), el("th", {}, "n discordant"),
    ])));
    const sigBody = el("tbody");
    for (const key of BASELINE_KEYS) {
      const sig = report.significance[`actor_critic_vs_${key}`];
      const tr = el("tr");
      tr.appendChild(el("td", { class: "metric-label" }, `Actor-Critic vs. ${SYSTEM_LABELS_SHORT[key]}`));
      if (!sig) {
        tr.appendChild(el("td", { class: "no-data", colspan: "4" }, "no data"));
      } else {
        tr.appendChild(el("td", {}, sig.p_value.toFixed(4)));
        tr.appendChild(el("td", {}, `${sig.top1_delta_pp >= 0 ? "+" : ""}${sig.top1_delta_pp.toFixed(1)}pp`));
        tr.appendChild(el("td", {}, String(sig.n)));
        const ndTd = el("td", {}, String(sig.n_discordant));
        ndTd.appendChild(el("span", { class: "caveat", title: sig.note }, " ⚠"));
        tr.appendChild(ndTd);
      }
      sigBody.appendChild(tr);
    }
    sigTable.appendChild(sigBody);
    body.appendChild(sigTable);

    body.appendChild(el("div", { class: "methodology-subhead" }, "Per-system detail"));
    const detailTable = el("table", { class: "metrics-table" });
    detailTable.appendChild(el("thead", {}, el("tr", {}, [
      el("th", {}, "Metric"),
      ...SYSTEM_KEYS.map((k) => el("th", {}, SYSTEM_LABELS_SHORT[k])),
    ])));
    const detailBody = el("tbody");

    function metricRow(label, getFn, fmtFn) {
      const tr = el("tr");
      tr.appendChild(el("td", { class: "metric-label" }, label));
      for (const key of SYSTEM_KEYS) {
        const block = report.systems[key];
        const td = el("td");
        if (!block) { td.textContent = "no data"; td.className = "no-data"; }
        else td.textContent = fmtFn(getFn(block));
        tr.appendChild(td);
      }
      return tr;
    }
    function noteRow(label, key, fmtValue) {
      const tr = el("tr");
      tr.appendChild(el("td", { class: "metric-label" }, label));
      for (const sysKey of SYSTEM_KEYS) {
        const block = report.systems[sysKey];
        const td = el("td");
        if (!block) { td.textContent = "no data"; td.className = "no-data"; }
        else {
          const field = block[key];
          td.appendChild(document.createTextNode(fmtValue(field.value)));
          if (field.note) td.appendChild(el("span", { class: "caveat", title: field.note }, " ⚠"));
        }
        tr.appendChild(td);
      }
      return tr;
    }

    detailBody.appendChild(noteRow("Structured output rate", "structured_output_rate", (v) => fmtPct(v, 0)));
    detailBody.appendChild(noteRow("Mean latency / session", "mean_latency_ms_per_session", (v) => (v === null ? "—" : v.toFixed(0) + " ms")));
    detailBody.appendChild(metricRow("Model", (b) => b.model, (v) => v));
    detailTable.appendChild(detailBody);
    body.appendChild(detailTable);
  }
})();
