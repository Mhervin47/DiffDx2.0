/*
 * mock.js — realistic sample payloads, matching the REAL /api/admin/* response
 * shapes exactly. Used ONLY when a real endpoint reports "not_generated".
 * Every place this renders MUST show a visible "sample data" label — never
 * let it pass as a real measurement.
 */
const AdminPortalMock = {
  // Phase 2 — matches /api/admin/usage's real shape exactly.
  usage: {
    status: "ok",
    window_days: 14,
    cost_per_session_usd_estimated: 0.00061,
    mean_tokens_per_session: 4200,
    p95_llm_latency_ms: 2100,
    sessions_today: 3,
    daily: Array.from({ length: 14 }, (_, i) => ({
      date: new Date(Date.now() - (13 - i) * 86400000).toISOString().slice(0, 10),
      sessions: [2, 4, 3, 5, 1, 0, 3, 4, 2, 3, 5, 4, 2, 3][i],
      turns: [8, 15, 11, 19, 4, 0, 12, 16, 7, 10, 18, 14, 8, 11][i],
      cost_usd_estimated: 0.0006 * [8, 15, 11, 19, 4, 0, 12, 16, 7, 10, 18, 14, 8, 11][i],
    })),
    counts: { usage_records: 168, sessions_in_window: 41, records_excluded_by_source_filter: 0, model_actual_unknown: 0, malformed_lines_skipped: 0 },
    coverage_note:
      "Usage figures cover the doctor model's own call, the compressor (runs once history exceeds the keep-recent window), and the critic (runs on every turn in a background thread) — all three are logged separately (see call_site on each record) and summed into these totals. The profile updater is disabled by design in the live web session (a CLI/offline-only path), so its cost is not, and cannot be, represented here. Completion tokens and the exact model that served each call (which can differ from the configured model after an HTTP 429 fallback) are real measured values from the provider's own response where available; a record falls back to an output-length estimate only when the provider didn't return one — see each record's completion_tokens_is_estimate flag, and model_actual_breakdown below for the live model mix. Cost is computed per record using that record's own actual (or configured, if unmeasured) model against pricing.json's rate table, not a single blended rate.",
    model_actual_breakdown: [
      { model: "openrouter/meta-llama/llama-3.1-8b-instruct", count: 168, pct: 61.5 },
      { model: "openrouter/meta-llama/llama-3.3-70b-instruct", count: 84, pct: 30.8 },
      { model: "openrouter/nvidia/nemotron-3-super-120b-a12b:free", count: 21, pct: 7.7 },
    ],
    notes: [],
  },

  // Phase 2 — matches /api/admin/audit's real shape exactly.
  audit: {
    status: "ok",
    total: 4,
    items: [
      { id: "00000000-0000-0000-0000-000000000004", ts: "2026-01-01T12:03:00", actor_user_id: "a1", actor_name: "Dr. Admin", actor_role: "admin", action: "subject_erasure", resource_type: "user", resource_id: "u-4821", ip: "127.0.0.1" },
      { id: "00000000-0000-0000-0000-000000000003", ts: "2026-01-01T11:58:00", actor_user_id: "a1", actor_name: "Dr. Admin", actor_role: "admin", action: "GET /api/admin/subjects/u-4821/export", resource_type: "user", resource_id: "u-4821", ip: "127.0.0.1" },
      { id: "00000000-0000-0000-0000-000000000002", ts: "2026-01-01T09:20:00", actor_user_id: null, actor_name: "unknown", actor_role: "unknown", action: "login_failed", resource_type: null, resource_id: "unknown@nowhere.com", ip: "10.0.0.4" },
      { id: "00000000-0000-0000-0000-000000000001", ts: "2026-01-01T08:00:00", actor_user_id: "d1", actor_name: "Dr. Rao", actor_role: "doctor", action: "login", resource_type: null, resource_id: null, ip: "10.0.0.1" },
    ],
  },

  // Phase 2 — matches /api/admin/subjects and inventory shapes exactly.
  subjectsSearch: {
    status: "ok",
    items: [
      { user_id: "u-4821", name: "Test Patient", email: "patient@example.com", created_at: "2025-11-01T00:00:00Z", session_count: 3 },
    ],
  },
  dsrRequestsList: {
    status: "ok",
    items: [
      { id: "req-9012", user_id: "u-4821", name: "Test Patient", email: "patient@example.com", reason: "No longer using the service", requested_at: "2025-11-05T00:00:00Z" },
    ],
  },
  subjectInventory: {
    status: "ok",
    user_id: "u-4821",
    name: "Test Patient",
    email: "patient@example.com",
    categories: [
      { label: "Identity", tables: ["users", "patients"], record_count: 1, detail: null, retained: false },
      { label: "Consultations", tables: ["diagnostic_sessions", "session_turns"], record_count: 14, detail: "3 sessions, 11 turns", retained: false },
      { label: "Appointments", tables: ["appointments"], record_count: 2, detail: null, retained: false },
      { label: "Messages", tables: ["message_threads", "messages"], record_count: 5, detail: "1 thread, 5 messages", retained: false },
      { label: "Files", tables: ["uploaded_files"], record_count: 1, detail: null, retained: false },
      { label: "Session logs on disk", tables: ["logs/sessions", "logs/final_records"], record_count: 3, detail: "outside the database", retained: false },
      { label: "Audit trail", tables: ["audit_log_entries"], record_count: 12, detail: null, retained: true },
    ],
  },

  // Phase 2 — matches /api/admin/health/detail's real shape exactly.
  healthDetail: {
    status: "ok",
    checked_at: "2026-01-01T00:00:00+00:00",
    api: "ok",
    postgres: { status: "ok", latency_ms: 4.2 },
    redis: { status: "not_configured", latency_ms: null, detail: "REDIS_URL unset — sessions held in-memory." },
    groq: { status: "ok", latency_ms: null, detail: "API key present. Connectivity not tested — health checks do not make billable model calls." },
  },

  evidenceReport: {
    status: "ok",
    generated_at: "2026-01-01T00:00:00+00:00",
    eval_set_size: 20,
    systems: {
      actor_critic: {
        n: 20,
        top1_accuracy: { correct: 13, total: 20, pct: 0.65 },
        top3_accuracy: { correct: 17, total: 20, pct: 0.85 },
        mean_differential_overlap: 0.52,
        by_stratum: {
          in_pool: { n: 5, top1_accuracy: { correct: 4, total: 5, pct: 0.8 }, top3_accuracy: { correct: 5, total: 5, pct: 1.0 }, mean_differential_overlap: 0.61 },
          out_of_pool: { n: 10, top1_accuracy: { correct: 6, total: 10, pct: 0.6 }, top3_accuracy: { correct: 8, total: 10, pct: 0.8 }, mean_differential_overlap: 0.48 },
          ambiguous: { n: 5, top1_accuracy: { correct: 3, total: 5, pct: 0.6 }, top3_accuracy: { correct: 4, total: 5, pct: 0.8 }, mean_differential_overlap: 0.45 },
        },
        structured_output_rate: {
          value: 1.0,
          note: "schema-enforced by construction — the actor's doctor-turn output is a validated pydantic model, it cannot produce unstructured output.",
        },
        mean_prompt_tokens_per_session: 4200,
        mean_completion_tokens_estimated_per_session: 380,
        mean_cost_per_session_usd_estimated: 0.00061,
        mean_latency_ms_per_session: { value: null, note: "not measurable until Phase 2 adds timing instrumentation to session.py." },
        cost_per_correct_diagnosis_usd: { value: 0.00094, note: null },
        model: "gemini/gemini-2.5-flash-lite",
      },
      baseline_mode_a: {
        n: 20,
        top1_accuracy: { correct: 8, total: 20, pct: 0.4 },
        top3_accuracy: { correct: 12, total: 20, pct: 0.6 },
        mean_differential_overlap: 0.31,
        by_stratum: {
          in_pool: { n: 5, top1_accuracy: { correct: 2, total: 5, pct: 0.4 }, top3_accuracy: { correct: 3, total: 5, pct: 0.6 }, mean_differential_overlap: 0.3 },
          out_of_pool: { n: 10, top1_accuracy: { correct: 4, total: 10, pct: 0.4 }, top3_accuracy: { correct: 6, total: 10, pct: 0.6 }, mean_differential_overlap: 0.3 },
          ambiguous: { n: 5, top1_accuracy: { correct: 2, total: 5, pct: 0.4 }, top3_accuracy: { correct: 3, total: 5, pct: 0.6 }, mean_differential_overlap: 0.32 },
        },
        structured_output_rate: { value: 0.9, note: null },
        mean_prompt_tokens_per_session: 310,
        mean_completion_tokens_estimated_per_session: 140,
        mean_cost_per_session_usd_estimated: 0.00008,
        mean_latency_ms_per_session: { value: 980, note: null },
        cost_per_correct_diagnosis_usd: { value: 0.0002, note: null },
        model: "gemini/gemini-2.5-flash-lite",
      },
      baseline_mode_b: {
        n: 20,
        top1_accuracy: { correct: 9, total: 20, pct: 0.45 },
        top3_accuracy: { correct: 13, total: 20, pct: 0.65 },
        mean_differential_overlap: 0.34,
        by_stratum: {
          in_pool: { n: 5, top1_accuracy: { correct: 3, total: 5, pct: 0.6 }, top3_accuracy: { correct: 4, total: 5, pct: 0.8 }, mean_differential_overlap: 0.35 },
          out_of_pool: { n: 10, top1_accuracy: { correct: 4, total: 10, pct: 0.4 }, top3_accuracy: { correct: 6, total: 10, pct: 0.6 }, mean_differential_overlap: 0.32 },
          ambiguous: { n: 5, top1_accuracy: { correct: 2, total: 5, pct: 0.4 }, top3_accuracy: { correct: 3, total: 5, pct: 0.6 }, mean_differential_overlap: 0.35 },
        },
        structured_output_rate: { value: 0.95, note: null },
        mean_prompt_tokens_per_session: 520,
        mean_completion_tokens_estimated_per_session: 150,
        mean_cost_per_session_usd_estimated: 0.00011,
        mean_latency_ms_per_session: { value: 1040, note: null },
        cost_per_correct_diagnosis_usd: { value: 0.00024, note: null },
        model: "gemini/gemini-2.5-flash-lite",
      },
    },
    significance: {
      actor_critic_vs_baseline_mode_a: {
        n: 20, b: 6, c: 1, n_discordant: 7, p_value: 0.125, top1_delta_pp: 25.0,
        note: "This eval set has 20 patients. Treat any significance result here as directional, not confirmatory — a larger eval set would be needed for a strong statistical claim. This caveat applies regardless of whether the p-value looks significant or not.",
      },
      actor_critic_vs_baseline_mode_b: {
        n: 20, b: 5, c: 1, n_discordant: 6, p_value: 0.219, top1_delta_pp: 20.0,
        note: "This eval set has 20 patients. Treat any significance result here as directional, not confirmatory — a larger eval set would be needed for a strong statistical claim. This caveat applies regardless of whether the p-value looks significant or not.",
      },
    },
    safety: {
      red_flag_patient_ids: ["test_090884", "test_023321", "test_043085", "test_119104", "test_040841", "test_048599", "test_001649", "test_076772"],
      red_flag_top3_recall: {
        actor_critic: { correct: 6, total: 8, pct: 0.75 },
        baseline_mode_a: { correct: 3, total: 8, pct: 0.375 },
        baseline_mode_b: { correct: 4, total: 8, pct: 0.5 },
      },
      critic_missed_red_flag_rate: { value: 0.125, total: 8, available_for: ["actor_critic"] },
      label_provenance: { automatic: 20, override: 0, automatic_source: "ddxplus_severity_v1" },
    },
    reasoning_quality: {
      total_turns: 84,
      total_sessions: 20,
      mean_question_quality: 0.71,
      median_question_quality: 0.75,
      mean_differential_quality: 0.68,
      median_differential_quality: 0.7,
      mean_reasoning_quality: 0.66,
      median_reasoning_quality: 0.68,
      confidence_calibration_distribution: { "well-calibrated": 52, overconfident: 21, underconfident: 11 },
      weakness_category_counts: { redundant_question: 9, missed_red_flag: 4, poor_differential: 6, premature_confidence: 3, other: 2 },
      low_question_quality_sessions: ["test_092939", "test_070029"],
      top_weakness_texts: [
        { weakness: "Asked about a symptom the patient already disclosed", count: 5 },
        { weakness: "Did not screen for a relevant red-flag feature", count: 3 },
      ],
    },
    cross_reference: [
      {
        session_id: "sample-session-1", patient_id: "test_092939", mean_question_quality: 0.42,
        low_quality_turns: [2, 3], leading_diagnosis_correct: false, top3_contains_truth: true,
        in_exemplar_pool: false, total_turns: 5, weakness_categories: ["poor_differential", "redundant_question"],
      },
      {
        session_id: "sample-session-2", patient_id: "test_119104", mean_question_quality: 0.51,
        low_quality_turns: [1], leading_diagnosis_correct: false, top3_contains_truth: false,
        in_exemplar_pool: false, total_turns: 4, weakness_categories: ["missed_red_flag"],
      },
    ],
    notes: [
      "mean_cost_per_session_usd_estimated for actor_critic excludes critic-call cost — the critic does not run during a real deployed patient session today, only in this offline evaluation harness, so excluding it is the correct scope for 'cost per deployed patient session,' not an oversight. It ALSO excludes profile_updater and compressor calls, which DO run in every real session but whose token usage is not currently logged anywhere in this codebase (profile_updater.py/compressor.py both discard usage via call_llm() rather than call_llm_with_usage()). True per-session cost for actor_critic is higher than the number shown here. Closing this gap requires editing files outside this system's scope and is Phase 2 work, not Phase 1.",
      "actor_critic's mean_latency_ms_per_session is null because session.py has no wall-clock instrumentation at all — not measurable until Phase 2 adds timing instrumentation to session.py. Baseline latency figures are real wall-clock measurements and are not directly comparable to a missing number.",
      "This eval set has 20 patients. Treat any significance result here as directional, not confirmatory — a larger eval set would be needed for a strong statistical claim. This caveat applies regardless of whether the p-value looks significant or not.",
      "All completion-token figures in this report are estimates (len(json.dumps(doctor_output)) // 4), not measured — loop1.llm never returns a completion-token count for any model call in this codebase, only prompt_tokens. Do not treat these as billed-token-accurate.",
      "The doctor model is held constant between actor_critic and both baseline modes (config['models']['doctor'], overridable via baseline_eval.py --model), so this is an architecture comparison, not a model comparison. The full per-turn model PIPELINE is NOT held constant, though: profile_updater and compressor models run on every turn of a real actor_critic session and never run in the baseline at all.",
    ],
  },
};
