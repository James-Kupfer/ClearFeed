# ClearFeed Prompt Caching — Final Verdict

**Date:** 2026-08-21
**Status:** Closed. No further caching analysis warranted.
**Supersedes and replaces:** `BATCH_PROCESSING_ANALYSIS.md` (2026-07-21), `price_optimization/README.md` (2026-08-01), and `batch_processing_analysis_2026_08_20.md`. All three have been retired; their non-caching content — the 2026-08-17 outage postmortem, the KEDM sender investigation, and the applied code changes — is consolidated below so nothing is lost.
**Evidence window:** 2026-07-21 → 2026-08-21 (32 days), 3,183 LLM calls with cache telemetry, 309 ingest cycles, 1,541 emails.

---

## Verdict

**Prompt caching cannot be meaningfully improved.**

Over 32 days the ingest pipeline wrote **276 `summarize` cache entries across 309 cycles — 0.89 writes per cycle**. One write per cycle is the hard floor: `INGEST_POLL_INTERVAL_MINUTES = 120` and the longest TTL Anthropic sells is 60 minutes, so every cycle must pay one cold write. The measured rate is *below* the floor because a few cycles start soon enough to inherit a live entry.

The 82% hit rate is not an 18% failure. It is `1 − (1 write ÷ 5.6 calls per cycle)`. It moves only if more emails land in a cycle, and the money it moves is **~$0.13/month**.

Caching is currently **saving ~$13/month — 28% of ingest spend**. What the prior documents got wrong is the **cost base**, not the caching conclusion: measured ingest is **~$33/month**, not $8–9/month.

---

## Measured data

Extracted from `logs/*.txt`. 2026-07-21 is the first run emitting `cache_read`/`cache_creation`; earlier logs cannot support any cache claim.

| Call site | Model | Calls | Hit rate | Cache read tok | Cache write tok | Uncached tok |
|---|---|---:|---:|---:|---:|---:|
| `summarize` | Haiku 4.5 | 1,553 | 82.2% | 8,515,999 | 1,840,252 | 14,825,406 |
| `classify` | Haiku 4.5 | 1,526 | 83.6% | 7,721,035 | 1,504,892 | 2,202,438 |
| `escalate` | Sonnet 5 | 104 | 28.8% | 284,442 | 657,775 | 218,744 |
| `digest` | Sonnet 5 | — | — | — | — | — |
| `action` | Sonnet 5 | — | — | — | — | — |

The two dashes are a finding. `dispatch.py` and `action_dispatch.py` log no usage, and `llm_client.py`'s `log.debug("LLM usage: …")` never reaches a file because `setup_logging()` pins the root logger to `INFO`. **Every audit of ClearFeed to date has been looking at 60% of the pipeline.**

### The cache is at its structural ceiling

| Measure | Value | Interpretation |
|---|---:|---|
| Cycles with ≥1 call | 309 | ~9.7/day of 12 scheduled |
| Cache writes | 276 | Fewer than one per cycle |
| Writes per cycle | **0.89** | Below the one-write-per-cycle floor |
| Cycles that wrote twice | 8 (2.6%) | The entire addressable miss population |
| Max writes in any cycle | 2 | Including the 61-call outage backlog cycle |
| Calls ÷ writes | 5.6 | Hit rate is fixed by emails per cycle |

Arithmetic check: 1,553 calls − 276 writes = 1,277 = the observed hit count exactly. Every miss is a cold cycle start; there is no third failure mode in the data.

TTL is refreshed on every read at no cost, so cycle length is irrelevant: the 2026-08-18 backlog cycle ran 61 calls over 68 minutes on one write; 2026-08-21 ran 47 calls over 53 minutes on one.

### Cost model

Haiku 4.5 $1/$5 per MTok in/out, 5-min write $1.25, read $0.10. Sonnet 5 $2/$10, write $2.50, read $0.20 (verified 2026-08-21 against platform.claude.com). Output split for `summarize` estimated from logged `len(summary)`; treat output column as ±20%.

| Call site | Cache read | Cache write | Uncached in | Output | 32-day | /month |
|---|---:|---:|---:|---:|---:|---:|
| `summarize` | $0.85 | $2.30 | $12.83 | $10.00 | $25.98 | $24.35 |
| `classify` | $0.77 | $1.88 | $1.65 | $2.75 | $7.06 | $6.61 |
| `escalate` | $0.06 | $1.64 | $0.35 | $0.45 | $2.50 | $2.34 |
| **Ingest total** | **$1.68** | **$5.82** | **$14.83** | **$13.20** | **$35.53** | **$33.31** |

Same traffic with `cache_control` removed: **$49.49/32d (~$46/month)**. Caching saves **~$13/month**.

Ceiling if every call hit cache (physically impossible on a 2-hour cron): a further **$5.02/month**. Attainable slice (the 8 double-writing cycles): **$0.13/month**.

---

## Audit of prior claims

| Claim | Source | Verdict |
|---|---|---|
| "Prompt caching is deployed and measurably working" | README §1 | **Confirmed.** ~$13/mo saved, 82–84% hit rate. Both system prompts exceed Haiku 4.5's 4,096-token minimum (`summarize.md` 6,667 tok, `classify.md` 6,769 tok) and are byte-identical between calls. |
| "Roughly a 13:1 read:write ratio" | README §1 | **Misleading.** Real data, but one 14-email cycle. Full-window ratio is 4.6:1 (summarize) and 5.1:1 (classify). The ratio is just *emails per cycle minus one* — not a quality metric. Quoting the best cycle is why later readers think something regressed. |
| "~10% cache hit rate on summarize/classify" | BATCH_PROCESSING_ANALYSIS.md | **Wrong.** Real: 82–84%. The same file also says "55% hit rate" elsewhere — two contradictory unmeasured figures, written the same day telemetry first appeared. **Most likely source of the recurring "caching isn't working" emails.** |
| "Monthly cost: ~$8–9" | BATCH_PROCESSING_ANALYSIS.md → README | **Wrong.** Ingest alone is ~$33/mo. Three compounding errors: flat "$0.80/MTok" (Haiku is $1 in / **$5 out**); ~1,200–2,000 tok/call assumed vs. `summarize`'s actual **9,546**; output priced as input. |
| "1-hour TTL rejected" | README §6 | **Confirmed**, and now provable: 120-min cron > 60-min TTL, so the 2× write premium buys zero reads. Close permanently. |
| "`dispatch.py` should pass `cacheable=False`" | 2026-08-20 fix #2 | **Confirmed.** 7 profiles, 7 distinct system prompts, one call each per weekly cycle. Verified against the 2026-08-15 run. Pure premium removal. |
| "`action_dispatch.py` caching fix" | 2026-08-20 fix #1 | **Overstated — measured value is zero.** Action routes to Sonnet (1,024-token minimum). Three of five new `system:` blocks fall below it: `task_personal` 522 tok, `task_professional` 542, `task_rights_offering` 490 — `cache_control` is silently ignored, no error. `task_investment` is aggregate and correctly `cacheable=False`. Only `task_connection` (2,450 tok) can ever cache, on the ~0.2% of runs with ≥2 records. Keep the change; stop calling it a win. |
| Sonnet escalation's 28.8% hit rate looks broken | *new* | **Leave alone.** Disabling caching there would cost money: $1.70 with caching (writes $1.64 + reads $0.06) vs. $1.88 without, over 32 days. The 30 hits pay for the 74 misses with $0.18 to spare. |
| Escalated records under-report token cost | *new bug* | **Real, but costs $0 in API spend.** See [Escalation token accounting](#escalation-token-accounting) below. |
| "Batch API: ~$7/mo savings doesn't justify refactoring" | BATCH break-even table | **Re-argue.** The architectural objections (immediate Gmail label/trash, conditional escalation) stand on their own. The ROI half used the wrong denominator: at $33/mo for ingest, a 50% batch discount is worth **~$16/mo (~$190/yr)** against the same document's $100–200 refactoring estimate. The "12–25× current volume" break-even is not right. Decline batch on architecture, not economics. |

---

## Escalation token accounting

**This is a measurement bug, not a spending bug. It costs $0 in API spend.** Both the Haiku classify call and the Sonnet re-classify are made and billed by Anthropic regardless of what the code records. Only the record is wrong.

`classify_with_escalation()` calls `consume_usage()` once, at `utils.py:326`, *after* the Sonnet re-classify. `consume_usage()` reads the backend's `last_*` attributes, which the Sonnet call has already overwritten, so the initial Haiku call's tokens are never captured. The docstring and the `ContentRecords.classify_tokens` column both claim "total input+output tokens across all classify calls"; on escalated records they hold Sonnet-only figures.

**Confirmed empirically, not just from the code.** Across the 104 escalate lines, `cache_creation` only ever takes the values 4,976 / 8,954 / 9,875 — all Sonnet-side prompt tokenizations. It is never a Haiku value (4,975 / 6,126 / 6,769) and never a sum of the two. If both calls were being accumulated, sums would appear. They never do.

What it actually costs:

| Effect | Magnitude |
|---|---|
| Direct API spend | **$0** — both calls billed either way |
| `classify_tokens` understatement | ~150,000 tokens / 32 days (~1,443 tok × 104 escalated records) |
| Real spend absent from the record | **~$0.28/month** |
| Per-model attribution | Escalated records report 100% `claude-sonnet-5`; Haiku undercounted on ~6.4% of classify volume |
| Effect on this document | The $33.31/month figure above draws on the `no escalation` log lines, so those 104 Haiku calls are missing from it too. **True ingest is ~$33.6/month.** The caching conclusions are untouched — per-cycle write counting uses `summarize`, which has no escalation path. |

Today nothing reads `classify_tokens` programmatically, so this is inert data corruption. It would bite the moment a cost report is built on that column — which is exactly the failure mode that produced the $8–9/month figure in the first place.

**Related, same class:** `call_json()` retries on JSON parse failure, and every retry is a real billed API call, but only the last is captured by the same mechanism. Lower frequency, identical undercount.

---

## Where the money actually is

Ranked by measured monthly cost. Every one exceeds the entire remaining caching opportunity.

**1. Email body sent to `summarize` — $12.83/mo.**
`BODY_TEXT_CAP = 30_000` chars plus up to `ARTICLE_SCRAPE_MAX_CHARS = 15_000` per scraped article. Actual average is **8,260 uncached input tokens per call** — most emails hit or approach the cap. Halving it is a direct cut; the quality cost (losing the tail of long newsletters) is testable against existing records.

**2. Summary length coming back out — $10.00/mo.**
Average logged `len(summary)` is **3,743 characters**, plus an executive summary and a rationale, all at Haiku's $5/MTok output rate. Output is the second-largest line item in the pipeline and has never been examined. A prompt edit to `summarize.md`, not an architecture change.

**3. The digest stage, entirely unmeasured — ~$8/mo (estimated).**
7 Sonnet profiles weekly, `DIGEST_MAX_TOKENS = 64000`, 291 records plus 28 prior digests re-sent as context in the 2026-08-15 run, ~28 min of generation. `dispatch.py` logs nothing and `DigestRuns` has no token columns. The only place where an unknown could still be large.

**4. Summarizing mail you then throw away — $4.13/mo.**
The pipeline summarizes first and classifies second, so every email that comes back labelled `Spam` has already paid for the expensive call. **243 in 32 days** at ~$0.0152 (summarize) + ~$0.0029 (classify). A subject/sender pre-filter, or classifying before summarizing, recovers most of it. Note the deterministic `_should_trash()` check at step 3 already runs pre-LLM and costs nothing — this is only the LLM-`Spam` path.

**5. Everything remaining in prompt caching — $0.13/mo.**
The 8 cycles out of 309 that wrote a second entry. No mechanism prevents them and no configuration would have.

---

## Three things to change, then stop

None are caching changes. Two are instrumentation; one is a correctness fix.

1. **Make the pipeline observable end to end.** Add `INFO`-level cache/token logging to `dispatch.py` and `action_dispatch.py` mirroring `utils.py`, and token columns to `DigestRuns`. Raising `llm_client.py`'s existing `log.debug("LLM usage: …")` to `INFO` covers all five call sites in one line. Until this exists, every audit will keep rediscovering the same "unverifiable" gaps and writing them up as problems.
2. **Fix the escalation token accounting.** Call `consume_usage()` after the first classify too, or accumulate inside `call_json()`, so `classify_tokens` means what its docstring says.
3. **Retire the stale documents.** `BATCH_PROCESSING_ANALYSIS.md` carries a 10% hit rate, a contradictory 55% hit rate, and an $8–9/mo cost base, all wrong and all cited forward. `README.md` §1 quotes a single unrepresentative cycle. Mark both superseded by this file.

---

## Closure test

ClearFeed has a prompt-caching problem **only** if one of these is true. Nothing else counts:

- **Writes per cycle > 1.1** for `summarize` or `classify` — one grep. Currently 0.89.
- **A call site above the model's cache minimum is missing `cache_control`** — 4,096 tok on Haiku, 1,024 on Sonnet, 512 on Opus 5. All five are currently correct.
- **A cached prefix changed between calls** — a placeholder interpolated into a `system:` block, a mid-cycle edit to `classify.md`, a model-routing change. The `_load_profile` placeholder guard added 2026-08-20 covers the profile case.
- **The cron gap fell below 60 minutes**, which would make the 1-hour TTL worth re-pricing. It is 120.

A hit rate below 100% is **not** on this list. On a 120-minute schedule the hit rate is arithmetic: one unavoidable write, divided by however many emails arrived.

---

# Consolidated from the retired documents

The rest of this file preserves material from `batch_processing_analysis_2026_08_20.md` that is unrelated to caching and has no other home.

## Code changes already applied (2026-08-20)

All merged and tested — **145/145 passing**. This is the change log for the caching work; the retired document was its only record.

**`src/llm_client.py`** — added `cacheable: bool = True` to the `Backend` protocol, `AnthropicBackend.call()`, `LLMClient.call()` and `call_json()`. `cache_control` is attached only when `cacheable=True`, so existing callers keep their behaviour by default.

**`src/dispatch.py`** — `run_dispatch()` passes `cacheable=False`. Correct: one call per profile per schedule cycle, no read can follow the write.

**`src/action_dispatch.py`** — `_action_one()` passes `system=profile.get("system", "")` at the default `cacheable=True`; `_run_aggregate()` passes it with `cacheable=False`. `_load_profile()` gained a guard that raises `ValueError` if a `system:` block contains a `{word}`-shaped placeholder, since `system` is sent verbatim and a stray placeholder would break cache stability silently.

**Profile YAML** — all five `task_*.yaml` split the single `prompt:` key into a static `system:` block and a dynamic tail. `task_rights_offering.yaml` also had its `## Input` section moved from the top to the bottom so the static instructions form an uninterrupted prefix. `task_investment.yaml` moved `Today is {today_dow}.` out of the static section. `task_connection.yaml` changed literal `{name}` example text to `<name>` to avoid tripping the new guard.

**Tests** — `test_llm_client.py` (+3), `test_action_dispatch.py` (+5), `test_dispatch.py` (+1).

Per the audit above, keep all of this. Just don't score the `action_dispatch` half as a cost win — three of the five `system:` blocks are below Sonnet's 1,024-token cache minimum and never activate.

## Production incident: 24-hour ingestion outage

**Root cause: an Anthropic org-level API spend cap. Not a code or infrastructure defect.**

| Event | Timestamp |
|---|---|
| Last successful ingest | 2026-08-17 18:03:04 (`ingested=2, failed=0`) |
| Outage begins | 2026-08-17 20:03:33 — `400 invalid_request_error`: *"You have reached your specified API usage limits. You will regain access on 2026-09-01 at 00:00 UTC."* |
| Duration | ~24h — 10 consecutive `ingest_orchestrator` cycles, every LLM call rejected (232 failures in the last broken cycle as the backlog accumulated) |
| Cap raised | Manually, in the Anthropic Console |
| Verified | 2026-08-18 ~20:29 CDT, live call to `claude-haiku-4-5-20251001` |
| Backlog cleared | 2026-08-18 20:36–21:45 cycle: `ingested=53, trashed=6, failed=0`; `task_investment` created 4 Todoist tasks |
| Normal cadence | Confirmed through the 2026-08-19 05:52 cycle |

`utils.retry()`'s 3-attempt logic behaved correctly — it cannot route around a hard cap, only transient errors. Gmail fetch was unaffected throughout; only the first LLM call per item was rejected, so nothing reached `ContentRecords` and every action profile correctly logged `Found 0 record(s)`.

**Still open:** add alerting for N consecutive ingest cycles failing entirely on LLM errors, modelled on the existing Gmail-OAuth-expiry pattern (`config.TOKEN_EXPIRATION_NOTIFICATION_EMAIL`, `config.py:61`). This class of outage is currently silent until someone reads the logs — it ran undetected for a full day.

## KEDM sender investigation

**The outage caused zero KEDM-specific data loss.** The most recent KEDM email in the mailbox is 2026-08-13 ("Thematic Review"); none arrived during the outage window. That message and the ones from 08-11 and 08-06 were all processed correctly — `Business` label applied, `ContentRecords` row written, `ProcessedClearFeed` label applied. Confirmed by direct DB query and cross-checked against a live read-only Gmail API search.

**A separate, pre-existing gap does exist.** 5 of 8 historical KEDM messages (2026-07-23 → 07-31: two "Fund Letters", "Free KEDM Lite!!!", "Want More Free Insights From KEDM?", "Up in the air") were never ingested at all — no `Business` label, no `ProcessedClearFeed` label, sitting in Gmail Trash with Gmail's own `CATEGORY_UPDATES` tag. All 9 configured Gmail filters were checked via the API; none reference KEDM or match these messages. `_should_trash()`'s deterministic patterns don't match the subject lines either.

**Most likely explanation:** Gmail's own spam/promotion heuristics routed them out of the Inbox before `fetch_ingest_threads()`'s query (`in:inbox -label:ProcessedClearFeed after:<90 days>`) could see them. `in:inbox` cannot match a thread Gmail already filed elsewhere. This predates the outage by weeks and is unrelated to it.

**Latent risk, flagged but not an observed bug:** `GmailClient.fetch_ingest_threads()` calls `threads().list(..., maxResults=config.EMAIL_MAX_THREADS)` (100) with no pagination — `nextPageToken` is never followed. If more than 100 threads are ever simultaneously eligible, only the first 100 per Gmail's default ordering are fetched per cycle. The 2026-08-18 catch-up cleared cleanly at 53 threads, well under the cap, so this caused no observed loss — but it is a starvation risk on a longer outage or higher volume.

## Open items

| Item | Status |
|---|---|
| `action_dispatch.py` missing `system=` / caching | Fixed — but worth $0, see audit |
| `dispatch.py` wasted `cache_control` on one-shot calls | Fixed |
| Profile YAML static/dynamic split (5 profiles) | Fixed |
| Validation guard against stray placeholders in `system:` | Added |
| Test coverage for the above | Added — 145/145 passing |
| `INFO`-level cache/token logging for `dispatch.py` and `action_dispatch.py` | **Not implemented — recommendation #1** |
| Escalation token-accounting fix (`utils.py:326`) | **Not implemented — recommendation #2** |
| Alerting for persistent ingest-LLM failures | Not implemented — recommended |
| `cache-diagnosis-2026-04-07` beta wiring | Not implemented, and not needed. The one useful application would be watching `system_changed` / `model_changed` for silent drift; the `_load_profile` placeholder guard plus the closure test below cover that more cheaply. ClearFeed's independent single-turn calls would report `messages_changed` on nearly every request — pure noise. **Close this.** |
| Check other senders for the same Gmail-filtering gap as KEDM | Not started |
| Review `Business`-labelled backlog for `actionable`-tagging accuracy | Declined |
| `fetch_ingest_threads()` pagination (100-thread cap) | Flagged, not investigated |

---

## Method & confidence

All counts extracted from `logs/*.txt` over 2026-07-21 → 2026-08-21, the full period for which cache telemetry exists. Source reviewed: `llm_client.py`, `utils.py`, `ingest_gmail.py`, `dispatch.py`, `action_dispatch.py`, `config.py`, `schema_postgres.sql`, all five `task_*.yaml` profiles. The Postgres instance was not reachable from this session; nothing in the conclusions depends on it.

Call counts, hit rates, and cache read/write token totals are exact. Dollar figures split uncached tokens into input and output using logged `len(summary)` as the output proxy — the split carries ~±20%, the totals less. The digest estimate is the only figure with no telemetry behind it, which is why instrumentation is recommendation #1.

Pricing verified 2026-08-21 against <https://platform.claude.com/docs/en/about-claude/pricing> and <https://platform.claude.com/docs/en/build-with-claude/prompt-caching>.
