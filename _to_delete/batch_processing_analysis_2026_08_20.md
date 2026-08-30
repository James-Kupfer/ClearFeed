# Prompt Caching Audit & Production Incident Findings

**Date:** August 20, 2026
**Status:** Fixes implemented and tested; two investigations closed, several follow-ups open
**Context:** Follow-up session covering (1) an audit of prompt-cache usage across `llm_client.py`, `dispatch.py`, and `action_dispatch.py`, (2) a production incident that stopped all ingestion for ~24 hours, and (3) a sender-specific ingest gap (KEDM) reported by the user. Builds on the caching work referenced in [BATCH_PROCESSING_ANALYSIS.md](BATCH_PROCESSING_ANALYSIS.md) (July 21, 2026).

---

## Executive Summary

1. **Caching gaps found and fixed:** `action_dispatch.py` never passed a `system=` parameter to the LLM client, so none of the five action profiles (`task_connection`, `task_investment`, `task_personal`, `task_professional`, `task_rights_offering`) benefited from prompt caching, despite four of them being structured with static instructions first and dynamic per-record data last. Separately, `dispatch.py` unconditionally applied `cache_control` to one-shot digest/synthesis calls that can never be re-read within the cache TTL, paying the ~1.25x write premium for nothing.
2. **Both are fixed, tested, and merged into working tree** — see [Code Changes](#code-changes) below. All 145 tests pass.
3. **Real production data corrected an initial overstatement:** the `action_dispatch.py` fix was originally framed as a "textbook caching win" for `task_connection`. Pulling actual log data showed matching records are rare enough (2+ records in a single run: 0.0–0.2% of ~21,900 runs sampled) that the real-world benefit is minimal, though the fix remains correct and harmless. The digest fix's benefit is unambiguous — `dedup_schedule` guarantees no reuse within any cache TTL, so removing `cache_control` there is pure cost reduction with no tradeoff.
4. **The already-existing classify/summarize caching (not touched this session) is the real, confirmed win** — 81–83% cache hit rate over 2,662 sampled calls.
5. **Production incident (unrelated to caching):** all ingestion was blocked for ~24 hours (2026-08-17 20:03 to 2026-08-18 20:29) by an Anthropic org-level API usage/spend cap, not a code defect. User raised the cap; verified working; backlog cleared automatically on the next scheduled cycle.
6. **KEDM sender investigation:** the incident caused zero KEDM-specific data loss (no new KEDM email arrived during the outage window), but a separate, pre-existing gap was found — 5 of 8 historical KEDM messages were never ingested at all, apparently filtered out by Gmail before ClearFeed's ingest query ever saw them.
7. **cache-diagnosis-2026-04-07 (Anthropic's beta cache-diagnostics endpoint) is NOT implemented.** Explicitly discussed and left undone — see [Cache Diagnostics Beta](#cache-diagnostics-beta-not-implemented).

---

## Part 1 — Prompt Caching Audit

### Findings

| # | Location | Issue | Severity |
|---|---|---|---|
| 1 | `action_dispatch.py` — `_action_one`, `_run_aggregate` | Never passed `system=` to `llm.call_json(...)`; entire prompt (static instructions + dynamic record data) sent as one user-turn string with no `cache_control`. | Structural bug — zero caching on 5 profiles |
| 2 | `dispatch.py` — `run_dispatch` | `llm.call(...)` always received a non-empty `system`, and `llm_client.py` unconditionally attached `cache_control` whenever `system` was non-empty — including one-shot digest/synthesis calls that run once per `dedup_schedule` cycle (daily/weekly), far outside any cache TTL. | Real, unconditional cost waste (1.25x write premium, never read) |
| 3 | `profiles/task_investment.yaml` | `Today is {today_dow}.` was interpolated mid-block (inside `## PRIORITY SCALE`), ahead of the otherwise-static classification taxonomy/rules/schema. Harmless once combined with fix #1's `cacheable=False` for aggregate mode, but would have forced a daily cache rewrite if aggregate mode were ever cached. | Minor / latent |
| 4 | `profiles/task_rights_offering.yaml` | Dynamic `## Input` section (per-record fields) was placed **before** the static instructions, defeating any future caching regardless of fix #1 (a cacheable prefix must be stable and first). | Structural, now fixed |
| 5 | `profiles/task_connection.yaml` | Contained literal `{name}` example text inside instructional prose (e.g. `"Accept connection from {name}"`), which is indistinguishable from a real template placeholder — would have false-positived the new validation guard (see below) and is inherently ambiguous. | Correctness/clarity, now fixed |

Confirmed **not** an issue: `ingest_gmail.py`'s classify/summarize path (`classify.md`, `summarize.md`) was already correctly wired — static, byte-identical system prompts (~28KB each, well above Haiku 4.5's 4096-token cache-write floor), read fresh from disk each call but content-identical, passed via `system=` with `cache_control` on every call. This is the dominant real caching win in the codebase (see [Part 2](#part-2--real-usage-pattern-validation)).

### Root Cause

`AnthropicBackend.call()` in `llm_client.py` always wrapped `system` in `cache_control: {"type": "ephemeral"}` whenever `system` was non-empty, with no way for a caller to opt out. `action_dispatch.py` profiles mixed static instructions and dynamic per-record/per-batch data into a single `prompt:` YAML key, which was always sent as the user turn — `system` was simply never populated for these calls.

### Code Changes

**`src/llm_client.py`**
- Added `cacheable: bool = True` parameter to the `Backend` protocol, `AnthropicBackend.call()`, `LLMClient.call()`, and `LLMClient.call_json()`.
- `cache_control` is now only attached to the system block when `cacheable=True` (the default — preserves existing classify/summarize/action behavior without callers needing to change).

**`src/dispatch.py`**
- `run_dispatch()`'s `llm.call(...)` now passes `cacheable=False` — digest/synthesis prompts run once per schedule cycle and can never be re-read within the TTL.

**`src/action_dispatch.py`**
- `_action_one()` (per-record mode) now passes `system=profile.get("system", "")` — default `cacheable=True` is correct here since per-record calls can repeat within a single run.
- `_run_aggregate()` now passes `system=profile.get("system", "")` and `cacheable=False` — aggregate mode makes exactly one call per profile run.
- `_load_profile()` gained a validation guard: raises `ValueError` if `profile["system"]` contains any `{word}`-shaped placeholder, since `system` is sent verbatim (never interpolated) — a stray placeholder would either leak literal braces into the model's system prompt or silently break the cache-stability assumption.
- Module docstring's profile-schema example updated to document the new optional `system:` key.

**Profile YAML restructuring** (all five `profiles/task_*.yaml`) — split the single `prompt:` key into a static `system:` block (sent once, cacheable) and a `prompt:` block containing only the per-record/per-batch dynamic tail:
- `task_connection.yaml` — split; `{name}` example text changed to `<name>` to avoid colliding with the new placeholder-validation guard.
- `task_personal.yaml`, `task_professional.yaml` — split (dynamic fields were already at the end).
- `task_rights_offering.yaml` — split **and reordered**: the dynamic `## Input` section was moved from the top of the prompt to the bottom, so the static instructions form an uninterrupted prefix.
- `task_investment.yaml` — split; `Today is {today_dow}.` moved out of the static `## PRIORITY SCALE` section into the dynamic `prompt:` block, next to `{records_json}`/`{prior_actions_json}`.

**Tests** — `tests/test_llm_client.py` (+3), `tests/test_action_dispatch.py` (+5), `tests/test_dispatch.py` (+1). Full suite: **145 passed**. Coverage added:
- `cacheable=True` (default) attaches `cache_control`; `cacheable=False` omits it; empty `system` produces `[]` either way.
- `_load_profile` rejects a `system` block containing a placeholder, accepts a placeholder-free one.
- `_action_one` forwards `profile['system']` and leaves `cacheable` at its default (True).
- `_run_aggregate` forwards `profile['system']` and explicitly sets `cacheable=False`.
- `run_dispatch` passes `cacheable=False` end-to-end.

---

## Part 2 — Real Usage-Pattern Validation

The initial recommendation for fix #1 was framed as a "textbook caching win," reasoning from prompt *shape* (static-first, dynamic-last, per-record loop) rather than measured call volume. Challenged on this, the following was pulled from `logs/*.txt` (June 1 – August 18, 2026, ~5,190 log files):

### classify/summarize cache hit rate (already-correct path — confirms it's working)

| Path | Calls | Cache hit rate |
|---|---|---|
| `[summarize]` (Haiku) | 1,332 | 81.0% |
| `[classify]` no-escalation (Haiku) | 1,330 | 82.7% |
| `[escalate]` (Sonnet) | 79 | 25.3% |

Escalation rate: 5.6% of classify calls (79/1,409). Escalating to Sonnet does **not** negate the Haiku-side cache — cache is scoped per-model (confirmed both by Anthropic's documented invalidation hierarchy and by the data above), so the two paths are independent lineages. The escalation path's lower hit rate simply reflects that it fires less often, not that it's broken; `classify_with_escalation()` was not modified this session because its caching was already correctly configured (`cacheable=True` default).

### Action-profile record volume (corrects the original overstatement)

| Profile | Runs w/ ≥1 record (of ~21,900 total runs) | Avg gap between them | Gap ≤5min | Gap ≤1h |
|---|---|---|---|---|
| task_connection | 59 | 27.2h | 5.2% | 6.9% |
| task_investment | 172 | 9.2h | 3.5% | 16.4% |
| task_personal | 13 | 117.9h | 8.3% | 8.3% |
| task_professional | 70 | 22.6h | 1.4% | 2.9% |
| task_rights_offering | 2 (total) | 3.4h | 0% | 0% |

Within a single run, **2+ records landing together is 0.0–0.2% of all runs** — essentially never.

**Conclusion:** the `action_dispatch.py` fix is still correct to keep (never harmful; captures the small real benefit on the few-percent of gaps that land inside the TTL), but its magnitude was overstated — action-profile call volume (a few hundred calls total across all 5 profiles over 2.5 months) is a rounding error next to the ~1,400 classify/summarize calls in the same window. The digest fix needed no such correction: `dedup_schedule: weekly_saturday` makes reuse within any TTL mathematically impossible regardless of measured run frequency, so `cacheable=False` there is unconditionally correct.

---

## Cache Diagnostics Beta — NOT Implemented

Anthropic's `cache-diagnosis-2026-04-07` beta (https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics) was investigated at the user's request but deliberately not wired in. Reasons:

- It requires switching `llm_client.py` from `client.messages.stream(...)` to `client.beta.messages.stream(...)`, adding the beta header, and threading a `diagnostics={"previous_message_id": ...}` param — none of which exists in the codebase today.
- ClearFeed's call pattern is independent single-turn requests (one fresh user message per record/digest), not a growing multi-turn conversation. Chaining `previous_message_id` across calls that share a system prompt but have entirely different user content would report `messages_changed` on nearly every call — expected, not a bug, but mostly noise for this use case.
- The one genuinely useful application would be narrow: watch specifically for `system_changed` / `model_changed` (catches silent drift — an accidental edit to `classify.md`, an unintended model-routing change, a stray placeholder reintroduced into a `system:` block) while ignoring `messages_changed` entirely.
- The user was asked whether to implement it on that narrower basis; no decision has been made as of this document.

**Current state:** the Anthropic Console shows no cache-diagnostics tracking for this project, correctly, because no request sends the beta header. Existing visibility comes from `cache_read`/`cache_creation` fields already logged by `utils.py`'s `summarize_content()`/`classify_with_escalation()` (INFO level, in the log files) — this is what was used to produce all the real numbers in Part 2. Note: `llm_client.py`'s own per-call debug log line (`"LLM usage: ..."`) is **not** actually visible in production log files, because `setup_logging()` sets the root logger to `INFO` and that line is logged at `DEBUG`. Only the `utils.py` call sites that explicitly log cache stats at `INFO` are captured. `action_dispatch.py` and `dispatch.py` do not log cache stats at all currently, so there is no way to verify from logs whether the new `system=`/`cacheable=` wiring is actually hitting the cache in production without either raising `dispatch`/`action_dispatch`'s logging level or adding equivalent `INFO`-level cache-stat logging to those paths (not done).

---

## Part 3 — Production Incident: 24-Hour Ingestion Outage

### Root Cause

An Anthropic org-level API usage/spend cap, not a code or infrastructure defect.

| Event | Timestamp |
|---|---|
| Last successful ingest before outage | 2026-08-17 18:03:04 (`ingested=2, failed=0`) |
| Outage begins | 2026-08-17 20:03:33 — first `400 invalid_request_error`: *"You have reached your specified API usage limits. You will regain access on 2026-09-01 at 00:00 UTC."* |
| Outage duration | ~24 hours — 10 consecutive `ingest_orchestrator` cycles, every single LLM call failed (232 failures in the last broken cycle alone; count grew each cycle as the unprocessed backlog accumulated) |
| User raised the cap | Manually, in the Anthropic Console (outside this session) |
| Fix verified | 2026-08-18 ~20:29 CDT — live test call to `claude-haiku-4-5-20251001` succeeded |
| Backlog catch-up | 2026-08-18 20:36–21:45 cycle: `ingested=53, trashed=6, failed=0`; `task_investment` aggregate run created 4 Todoist tasks |
| Normal cadence resumed | Confirmed through 2026-08-19 05:52 cycle (`ingested=0/1/2/0/5` across subsequent runs — consistent with typical low-volume steady state) |

The 3-attempt retry logic in `utils.retry()` behaved correctly throughout — it cannot route around a hard monthly cap, only transient errors. Gmail fetch was unaffected the whole time; only the first LLM call per item was rejected, so nothing reached `ContentRecords` during the outage, which is also why every action profile logged `Found 0 record(s)` — there was nothing new in the DB for them to act on.

### Recommendation (not implemented)

Add alerting for N consecutive ingest cycles failing entirely on LLM errors, mirroring the existing pattern for Gmail OAuth expiry (`config.TOKEN_EXPIRATION_NOTIFICATION_EMAIL`, `config.py:61`). Currently this class of outage is silent until someone reads the logs — it ran undetected for a full day.

---

## Part 4 — KEDM Sender Investigation

Triggered by the user's report that "nothing from KEDM was processed correctly" after the outage catch-up.

### Finding 1: The outage caused zero KEDM-specific data loss

The most recent KEDM email in the entire mailbox is from **2026-08-13** ("Thematic Review") — no new KEDM email arrived in the 6 days since, outage or not. That email, plus ones from 2026-08-11 and 2026-08-06, were all processed correctly (`Business` label applied, `ContentRecords` row written, `ProcessedClearFeed` label applied). Confirmed via direct DB query (initially returned zero results due to an overly narrow date filter — corrected by removing the date bound entirely) and cross-checked against a live, read-only Gmail API search.

### Finding 2: A separate, pre-existing ingest gap (unrelated to the outage)

5 of 8 historical KEDM messages (2026-07-23 through 2026-07-31: two "Fund Letters," "Free KEDM Lite!!!," "Want More Free Insights From KEDM?," "Up in the air") were **never ingested at all** — no `Business` label, no `ProcessedClearFeed` label, sitting only in Gmail Trash with Gmail's own `CATEGORY_UPDATES` tag. Checked all 9 configured Gmail filters directly via the API — none reference KEDM or match these messages. `_should_trash()` in `ingest_gmail.py` (deterministic pre-LLM trash patterns: security-alert / promo keyword matching) was also checked against these subject lines and does not match any of them.

**Most likely explanation:** Gmail's own spam/promotion heuristics routed these messages out of the Inbox before ClearFeed's `fetch_ingest_threads()` query (`in:inbox -label:ProcessedClearFeed after:<90 days>`) ever had a chance to see them — `in:inbox` cannot match a thread Gmail already filtered elsewhere. This predates the outage by weeks and is a distinct issue from it.

**Not investigated further (deferred, no decision from user):**
- Whether other senders exhibit the same "never reaches Inbox" pattern.
- Whether `task_investment`'s post-catch-up result of only 4 qualifying records (`label=Business AND tag=actionable`, 14-day lookback) reflects correct classifier behavior or under-tagging — user explicitly declined a review of existing records.

### Secondary structural note (not confirmed as an active bug, flagged for awareness)

`GmailClient.fetch_ingest_threads()` calls `threads().list(..., maxResults=config.EMAIL_MAX_THREADS)` (100) with **no pagination** — only `response.get("threads", [])` is read; `nextPageToken` is never followed. During a backlog condition (e.g. the 24-hour outage), if more than 100 threads are simultaneously eligible (`in:inbox -label:ProcessedClearFeed`), only the top 100 per Gmail's default ordering are fetched per cycle. In practice the 2026-08-18 catch-up cleared cleanly (53 ingested in one cycle, well under the cap), so this was not the cause of any observed data loss — but it is a latent starvation risk if inbound volume or outage duration were larger than what was observed here.

---

## Summary of Open Items

| Item | Status |
|---|---|
| `action_dispatch.py` missing `system=` / caching | **Fixed** |
| `dispatch.py` wasted `cache_control` on one-shot calls | **Fixed** |
| Profile YAML static/dynamic split (5 profiles) | **Fixed** |
| `task_rights_offering.yaml` dynamic-content-first ordering | **Fixed** |
| `task_connection.yaml` ambiguous `{name}` literal | **Fixed** |
| Validation guard against stray placeholders in `system:` | **Added** |
| Test coverage for all of the above | **Added — 145/145 passing** |
| `cache-diagnosis-2026-04-07` beta wiring | **Not implemented** — awaiting decision |
| `INFO`-level cache-stat logging for `action_dispatch.py`/`dispatch.py` | **Not implemented** |
| Alerting for persistent ingest-LLM-call failures | **Not implemented** — recommended, modeled on existing Gmail-OAuth-expiry pattern |
| Check other senders for the same Gmail-filtering gap as KEDM | **Not started** |
| Review Business-labeled backlog for `actionable`-tagging accuracy | **Declined by user** |
| `fetch_ingest_threads()` pagination (100-thread cap, no `nextPageToken`) | **Flagged, not investigated further** |
