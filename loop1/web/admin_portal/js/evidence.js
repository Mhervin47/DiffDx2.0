/* evidence.js — renders /api/admin/evidence (or mock.js sample data) into the page. */
(async function () {
  const { fetchWithMockFallback, fmtPct, fmtUsd, fmtFraction, el } = AdminPortal;

  const SYSTEM_LABELS = {
    actor_critic: "Actor-Critic",
    baseline_mode_a: "Baseline — Mode A (matched interaction)",
    baseline_mode_b: "Baseline — Mode B (matched information)",
  };
  const SYSTEM_KEYS = ["actor_critic", "baseline_mode_a", "baseline_mode_b"];

  const { data: report, usingMock } = await fetchWithMockFallback(
    "/api/admin/evidence",
    AdminPortalMock.evidenceReport
  );

  document.getElementById("sample-banner").hidden = !usingMock;
  document.getElementById("eval-set-size").textContent = report.eval_set_size;
  document.getElementById("generated-at").textContent = report.generated_at;

  renderMainTable(report);
  renderStratumTable(report);
  renderSafety(report);
  renderNotes(report);

  function metricRow(label, getFn, fmtFn) {
    const tr = el("tr");
    tr.appendChild(el("td", { class: "metric-label" }, label));
    for (const key of SYSTEM_KEYS) {
      const block = report.systems[key];
      const td = el("td");
      if (!block) {
        td.textContent = "no data";
        td.className = "no-data";
      } else {
        td.textContent = fmtFn(getFn(block));
      }
      tr.appendChild(td);
    }
    return tr;
  }

  function noteRow(label, key) {
    // For fields shaped {value, note} — shows the value plus an inline caveat icon with title.
    const tr = el("tr");
    tr.appendChild(el("td", { class: "metric-label" }, label));
    for (const sysKey of SYSTEM_KEYS) {
      const block = report.systems[sysKey];
      const td = el("td");
      if (!block) {
        td.textContent = "no data";
        td.className = "no-data";
      } else {
        const field = block[key];
        const valueText = key === "mean_latency_ms_per_session" ? `${field.value === null ? "—" : field.value.toFixed(0) + " ms"}` : (field.value === null ? "—" : fmtUsd(field.value));
        td.appendChild(document.createTextNode(valueText));
        if (field.note) {
          const sup = el("span", { class: "caveat", title: field.note }, " ⚠");
          td.appendChild(sup);
        }
      }
      tr.appendChild(td);
    }
    return tr;
  }

  function renderMainTable(report) {
    const table = document.getElementById("main-table");
    const thead = el("thead", {}, [
      el("tr", {}, [
        el("th", {}, "Metric"),
        ...SYSTEM_KEYS.map((k) => el("th", {}, SYSTEM_LABELS[k])),
      ]),
    ]);
    const tbody = el("tbody", {}, [
      metricRow("Top-1 accuracy", (b) => b.top1_accuracy, fmtFraction),
      significanceRow(),
      metricRow("Top-3 accuracy", (b) => b.top3_accuracy, fmtFraction),
      metricRow("Mean differential overlap (Jaccard)", (b) => b.mean_differential_overlap, (v) => (v === null ? "—" : v.toFixed(3))),
      metricRow("Structured output rate", (b) => b.structured_output_rate.value, (v) => fmtPct(v, 0)),
      metricRow("Mean prompt tokens / session", (b) => b.mean_prompt_tokens_per_session, (v) => (v === null ? "—" : v.toFixed(0))),
      metricRow("Mean completion tokens / session (estimated)", (b) => b.mean_completion_tokens_estimated_per_session, (v) => (v === null ? "—" : v.toFixed(0))),
      metricRow("Mean cost / session (estimated)", (b) => b.mean_cost_per_session_usd_estimated, fmtUsd),
      noteRow("Mean latency / session", "mean_latency_ms_per_session"),
      noteRow("Cost per correct diagnosis (estimated)", "cost_per_correct_diagnosis_usd"),
      metricRow("Model", (b) => b.model, (v) => v),
    ]);
    table.appendChild(thead);
    table.appendChild(tbody);
  }

  function significanceRow() {
    const tr = el("tr", { class: "significance-row" });
    tr.appendChild(el("td", { class: "metric-label sub" }, "  vs. actor-critic (exact McNemar)"));
    tr.appendChild(el("td", { class: "no-data" }, "—"));
    for (const key of ["baseline_mode_a", "baseline_mode_b"]) {
      const sig = report.significance[`actor_critic_vs_${key}`];
      const td = el("td");
      if (!sig) {
        td.textContent = "no data";
        td.className = "no-data";
      } else {
        td.appendChild(document.createTextNode(
          `p=${sig.p_value.toFixed(4)}, Δ=${sig.top1_delta_pp >= 0 ? "+" : ""}${sig.top1_delta_pp.toFixed(1)}pp (n=${sig.n})`
        ));
        td.appendChild(el("span", { class: "caveat", title: sig.note }, " ⚠"));
      }
      tr.appendChild(td);
    }
    return tr;
  }

  function renderStratumTable(report) {
    const table = document.getElementById("stratum-table");
    const strata = ["in_pool", "out_of_pool", "ambiguous"];
    const thead = el("thead", {}, [
      el("tr", {}, [
        el("th", {}, "Stratum"),
        ...SYSTEM_KEYS.map((k) => el("th", {}, SYSTEM_LABELS[k])),
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

  function renderSafety(report) {
    const container = document.getElementById("safety-section");
    if (!report.safety) {
      container.appendChild(el("p", { class: "muted" }, "Safety recall not computed — no red_flag_labels.json found."));
      return;
    }
    const s = report.safety;
    const table = el("table", { class: "metrics-table" });
    table.appendChild(el("thead", {}, el("tr", {}, [
      el("th", {}, "Metric"),
      ...SYSTEM_KEYS.map((k) => el("th", {}, SYSTEM_LABELS[k])),
    ])));
    const tbody = el("tbody");
    const recallRow = el("tr");
    recallRow.appendChild(el("td", { class: "metric-label" }, `Red-flag top-3 recall (n=${s.red_flag_patient_ids.length} red-flag patients)`));
    for (const key of SYSTEM_KEYS) {
      const td = el("td");
      const r = s.red_flag_top3_recall[key];
      td.textContent = r ? fmtFraction(r) : "no data";
      if (!r) td.className = "no-data";
      recallRow.appendChild(td);
    }
    tbody.appendChild(recallRow);

    if (s.critic_missed_red_flag_rate) {
      const cr = s.critic_missed_red_flag_rate;
      const row = el("tr");
      row.appendChild(el("td", { class: "metric-label" }, "Critic-flagged missed-red-flag rate"));
      row.appendChild(el("td", {}, `${fmtPct(cr.value)} (of ${cr.total})`));
      row.appendChild(el("td", { class: "no-data" }, "available for actor_critic only"));
      row.appendChild(el("td", { class: "no-data" }, "available for actor_critic only"));
      tbody.appendChild(row);
    }
    table.appendChild(tbody);
    container.appendChild(table);

    const prov = s.label_provenance;
    container.appendChild(el("p", { class: "muted" },
      `Label provenance: ${prov.automatic} automatic (${prov.automatic_source}), ${prov.override} real clinician override(s). ` +
      "Safety-recall credibility depends entirely on this — check it before trusting the numbers above."
    ));
  }

  function renderNotes(report) {
    const list = document.getElementById("notes-list");
    for (const note of report.notes) {
      list.appendChild(el("li", {}, note));
    }
  }
})();
