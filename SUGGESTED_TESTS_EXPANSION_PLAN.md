# Suggested Tests: Persistence, Additional Generation, Multi-Page Access

## Problem

The LLM already recommends basic pre-diagnosis tests from a session's differential
(`GET /api/session/{id}/suggested-tests`, `session_booking.py`), and upload is already
optional (a patient can check a test "done" or upload a result file, or ignore it — nothing
blocks the flow). But the feature has three gaps:

1. **Not persisted.** The generated list lives only in an in-process dict
   (`_suggested_tests_cache` in `sessions.py`) — lost on every server restart, and never
   written anywhere durable. There's no record of what was ever suggested.
2. **One-shot.** The list is generated exactly once per session (unless `?refresh=1`
   *replaces* it). There's no way to ask for more tests later if the picture changes.
3. **Single surface.** The only place a patient ever sees or acts on this list is
   `report.html`, right after a session ends. `health-history.html` (their permanent record
   of past visits) and `pre-visit-intake.html` (prep before an upcoming visit) both know
   nothing about it.

## Design

### 1. Persist the suggested-tests store (backend)

Replace the in-memory `_suggested_tests_cache` with a DB-backed store, same pattern already
used for `_session_test_uploads` (`legacy_store.py`'s `_db_load`/`_db_save` against the
`store` table, collection key `"suggested_tests"`):

```python
# legacy_store.py
def _load_suggested_tests() -> dict:
    return _db_load("suggested_tests", {})

def _save_suggested_tests(data: dict) -> None:
    _db_save("suggested_tests", data)

_suggested_tests_store: dict = _load_suggested_tests()   # session_id -> {"batches": [...]}
```

Per-session shape:
```python
{
  "batches": [
    {
      "generated_at": "2026-09-13T12:00:00Z",
      "necessary": true,
      "rationale": "...",
      "tests": [ {"id": "t1", "name": "CBC", ...}, ... ]
    },
    # a later "generate more" call appends a second batch here
  ]
}
```

`GET /api/session/{id}/suggested-tests` keeps its exact current response shape
(`{necessary, rationale, tests}`) for backward compatibility with `report.html`'s existing
rendering code — it now flattens all batches' `tests` into one list (later batches appended
after earlier ones, ids stay unique since generation always assigns `t{n}` against the
existing count) and uses the *latest* batch's `rationale`/`necessary`. First call for a
session with no stored batches generates batch 1 exactly as today. `?refresh=1` is repointed
to mean "regenerate batch 1" (unchanged behavior, just now against the persisted store
instead of the dict) rather than being removed — no existing caller's contract changes.

### 2. "Generate additional tests" (backend + report.html)

New endpoint:

```python
@router.post("/api/session/{session_id}/suggested-tests/more")
```

Same LLM prompt as today's generator, with one addition: the prompt is told which tests were
already suggested (by name) and instructed not to repeat them, and is allowed to say "no
additional tests needed." Appends a new batch to the persisted store, returns the same
merged `{necessary, rationale, tests}` shape as the GET route, plus a `new_ids` list (the ids
introduced by this call) so the frontend can visually mark only the new cards.

**Automatic, not user-triggered.** There's no button — generation stays automatic throughout;
only uploading a result stays the optional, patient-driven step. The endpoint is idempotent
per session (a persisted `auto_more_done` flag on the store entry), so it only ever calls the
LLM once per session; every later call — including every subsequent page load — just replays
the stored result at no extra cost. `report.html` fires this silently right after the initial
list renders (`_autoCheckMoreTests()`, called from `loadSuggestedTests()`), with no loading
state and no "nothing found" message — only a small "New" badge appears on any cards it
actually adds. Existing checked/uploaded state (`_sugDone`/`_sugUploads`) is untouched — new
tests just extend the set.

### 3. Shared frontend widget (new file: `suggested-tests-widget.js`)

`report.html`'s suggested-tests rendering (`_buildSuggestedTestsHtml`, `toggleSugDone`,
`uploadSugResult`, progress bar, upload badges — ~250 lines) is exactly what
`health-history.html` and `pre-visit-intake.html` both need too. Rather than copy-pasting it
three times, factor it into a shared script, following the same shared-include convention
the codebase already uses for `auth.js` and `reloader.js`:

- `SugTestsWidget.render(containerEl, sessionId, opts)` — fetches `/suggested-tests` and
  `/suggested-test-files`, renders cards + progress bar + upload buttons into `containerEl`,
  then silently fires the automatic additional-tests check. `opts.autoCheckMore` (default
  true) turns that check off — off on pre-visit-intake (prep context, not the review context;
  regeneration mid-checklist doesn't make sense there).
- `toggleSugDone`/`uploadSugResult`/localStorage key logic moves in verbatim — the existing
  `sug_tests_${sessionId}` key is already correctly scoped per session, so reuse across pages
  needs no change.

`report.html` keeps its own long-standing implementation (not switched to the shared widget,
to avoid touching an already-polished page) — it grew the same automatic-check behavior
directly, via `_autoCheckMoreTests()`.

### 4. `health-history.html` — recommended tests per past visit

Each `.tl-card` with a truthy `session_id` gets a collapsed-by-default "Recommended Tests"
sub-section (expand on click, lazy-loads via `SugTestsWidget.render` only when opened — avoids
firing N requests for every visible history entry on page load). Uses the shared widget with
its default `autoCheckMore: true`, so opening an old session still silently checks for
additional tests if the picture has changed, exactly like on `report.html` — still no button.

### 5. `pre-visit-intake.html` — recommended tests before the visit

Add one fetch at page load: `GET /api/appointments`, find the entry matching the `appt`
query-param id, read its `session_id` (field already present on every appointment record —
no new backend route needed). If present, render a "Recommended Tests" panel (shared widget,
`autoCheckMore: false` — regeneration belongs on the report page, not mid-intake) above
the existing static `tests_done` self-report checklist. The two stay conceptually distinct
and both remain: `tests_done` is "tell us what you've already had done"; the new panel is
"here's what the AI recommends, upload if you have results." No change to the intake submit
payload — `tests_done` keeps working exactly as today.

## Out of scope

- No new relational tables — everything uses the existing blob/`store`-table JSON pattern,
  consistent with how `suggested_test_uploads` already works.
- No change to the doctor-side view of uploaded results (already works via the existing
  `patient_files`/`suggested_test_uploads` pipeline, untouched).
- Not touching the static `TESTS_CATALOG` self-report checklist on pre-visit-intake — it's a
  different concept (patient-reported history vs. AI recommendation) and stays as-is.

## Verification

- `py_compile` on all touched backend files; `node --check` on the new/changed JS.
- Direct Python exercise of the persistence round-trip (`_save_suggested_tests` /
  `_load_suggested_tests`) and the batch-merge logic in `get_suggested_tests`.
- Playwright, mocked APIs: report.html's automatic additional-tests check appends new cards
  with the "New" badge and preserves existing checked/uploaded state; health-history.html's
  collapsed section lazy-loads on expand; pre-visit-intake.html renders the panel only when
  the matched appointment has a `session_id`, and never sends a `/suggested-tests` request
  when it doesn't.
