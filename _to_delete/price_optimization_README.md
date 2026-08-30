# ClearFeed LLM Cost Optimization — Options Evaluated

**Last reviewed:** 2026-08-01
**Bottom line:** No further meaningful cost-reduction opportunity identified at current scale (~53 classify events/day, ~100 action-dispatch calls/month, 7 digest profiles running 1-4x/week). Prompt caching is already deployed and measurably working. Everything else evaluated below is either a wash, negative ROI, or blocked on an architecture change with a payoff too small to justify it.

Re-review if: ingest volume grows materially (see `BATCH_PROCESSING_ANALYSIS.md` breakeven table), Anthropic/Google pricing shifts, or digest/action-dispatch call volume increases significantly.

---

## 1. Prompt caching — implemented, verified working

`llm_client.py` marks the system prompt `cache_control: ephemeral` on every call (5-minute TTL). Measured from a real ~2.5-hour ingest cycle log (2026-07-31):

| Operation | Calls | Total tokens | Cache reads | Cache creation |
|---|---|---|---|---|
| `summarize` | 14 | 135,066 | 86,671 | 6,667 |
| `classify` | 26 | 47,264 | 67,512 | 6,126 |

Cache reads bill at 10% of input price — roughly a 13:1 read:write ratio, meaning the vast majority of calls in a cycle hit the cache instead of paying full price for the repeated system prompt. This is the single highest-leverage optimization available and it's already live.

**Status: done. No further action.**

## 2. Batch Processing API (50% discount)

Full analysis already exists at [`../BATCH_PROCESSING_ANALYSIS.md`](../BATCH_PROCESSING_ANALYSIS.md) (2026-07-21). Summary: not viable for `summarize`/`classify` at current volume (~39-53 emails/day) because it requires restructuring the ingest pipeline from synchronous per-record processing to submit-batch/await-results, and the pipeline's real-time Gmail label/trash operations and conditional escalation logic don't fit a fire-and-forget batch model. Breakeven is ~500-1000 requests/day (12-25x current volume).

Extended this session to the other two stages, both worse fits than ingest:
- **`digest`/`synthesis`** — each profile makes exactly one LLM call per run. Nothing to batch against; batching would only add queue latency for zero savings.
- **`action` (Todoist dispatch)** — same one-call-per-run shape for most profiles, plus these are meant to land in Todoist promptly; batch latency (up to 24h) works against the point.

**Status: not viable anywhere in ClearFeed today. Revisit only if ingest volume grows per the breakeven table in `BATCH_PROCESSING_ANALYSIS.md`.**

## 3. Prompt caching for `action_dispatch.py` — evaluated, not worth building

`action_dispatch.py` currently sends the entire per-record prompt as one uncached blob (no `system=` argument at all). Investigated whether adding caching here would help, the way it helps `summarize`/`classify`. It would not, for four compounding reasons:

1. **Low volume** — ~100 action LLM calls/month total across all 5 profiles.
2. **Most runs find exactly 1 matching record** — the overwhelming majority of non-zero runs (99 of ~185 events for `task_investment`, 44 for `task_professional`, 39 for `task_connection`, 8 for `task_personal`) matched a single record, meaning a single LLM call with nothing else in that run to share a cache with.
3. **Cache TTL (5 min) doesn't span the cron gap (~2h)** — caching only helps calls made back-to-back in the same run; it can't help across scheduled runs hours apart.
4. **`task_investment`, the highest-volume profile, runs in `mode=aggregate`** — one call per run covering all matched records in a single prompt. There's structurally nothing to cache against.
5. Three of five profiles' static prompt text (`task_personal` ~553 tokens, `task_professional` ~572, `task_rights_offering` ~514) sit below Anthropic's 1024-token cache-write minimum and wouldn't activate caching even if wired up.

Best-case realistic saving (a rare multi-record backfill burst on `task_connection`, the one profile with both per-record mode and a static prompt above the cache minimum): on the order of $0.05-0.10 per occurrence, and it's happened twice in the logs, ever.

**Status: not worth the template-restructuring effort required. No action.**

## 4. Alternate model backend (Gemini 3 Pro) for digests

Pricing is close to a wash: Gemini 3 Pro Preview $2/M input, $12/M output vs. Sonnet 5's $2/M input, $10/M output (both introductory rates, as of August 2026). Not a cost play on its own.

`llm_client.py` already defines a `Backend` Protocol with a registry (`_BACKEND_REGISTRY`) specifically designed so a second backend can be added — the switching cost is low if ever wanted. The unresolved variable is quality/behavior, not engineering effort or price: Gemini's safety classifier is tuned differently than Claude's, so it could handle exploit-heavy newsletter batches (the kind that triggered the `digest_technology` refusal fixed this session) better, worse, or just differently — no way to know without testing against real content.

**Status: cost-neutral, quality unknown. Not pursued; would only be worth building to compare quality, not to save money.**

## 5. Classify-stage cost estimate (baseline, for reference)

Estimated from the same 14-day log window: ~740 classify events (~53/day), 97% resolved on Haiku alone, ~3% escalate to Sonnet.

| | Cost (14 days) |
|---|---|
| Haiku fresh input/output | ~$1.61 |
| Haiku cache read | ~$0.33 |
| Haiku cache write | ~$0.83 |
| Escalated calls (upper-bound, priced at Sonnet rates) | ~$0.55 |
| **Total** | **~$3.30** |

Extrapolated: **~$7/month, ~$85/year** for classify specifically. This is a small slice of total spend — `digest` (Sonnet, up to 64K max_tokens, full-batch synthesis, 7 profiles) is the likely dominant cost driver, not ingest.

## 6. Other levers considered and rejected

- **1-hour cache TTL instead of 5-minute** — Anthropic prices 1-hour cache writes at 2x input rate vs. 1.25x for 5-minute. Given the already-high ~13:1 read:write ratio measured in §1, cache misses are already rare; paying more per write for a hit-rate improvement on an already-small miss population isn't worth it. **Rejected** — revisit only if the ingest polling cadence changes in a way that spreads calls further apart than 5 minutes.
- **Downgrading the digest model from Sonnet to Haiku** — would cut the highest per-call cost stage, but digest synthesis (consolidating dozens of items, judgment calls on merging/scope/prioritization) is exactly the kind of task where model quality is load-bearing. Not evaluated in depth because the quality risk outweighs the likely saving without a side-by-side quality test — same caveat as the Gemini option above.
- **Shrinking SQL lookback windows / `LIMIT` in digest profiles** (currently 240h lookback, `LIMIT 200`, plus a 720h/limit-5 Prior Digests band) — would reduce input tokens directly, but trades off digest coverage/completeness for savings that, given digest calls are infrequent (7 profiles, 1-4x/week), are likely small in absolute dollars. Not quantified — flagging as an available lever, not a recommendation.
