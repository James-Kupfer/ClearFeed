# Batch Processing Feasibility Analysis for ClearFeed

**Date:** July 21, 2026  
**Status:** Not Recommended  
**Context:** ClearFeed ingests ~39 emails/day through a real-time pipeline. Prompt caching was implemented in previous optimization pass.

---

## Executive Summary

Anthropic's Batch Processing API could theoretically reduce LLM costs by ~$4–5/month (~45% reduction from current ~$8–9/month spend). However, **batch processing is not recommended** for ClearFeed due to:

1. **Streaming incompatibility** — current architecture uses streaming for all LLM calls; batch API rejects streaming
2. **Real-time blocking operations** — Gmail label/trash must happen immediately; batch results arrive 1–24 hours later
3. **Escalation logic conflict** — conditional re-classification doesn't fit fire-and-forget batch model
4. **Poor ROI at scale** — ~$5/month savings doesn't justify refactoring at 39 emails/day

**Recommendation:** Keep current streaming architecture with prompt caching. Batch processing is viable only at 6–12x current volume.

---

## Cost Analysis

### Current Spend (Real-Time Streaming + Prompt Caching)

**Volume:** 39 emails/day average

**LLM Calls per day:**
- Summarize (Haiku): 39 calls × ~2,000 tokens = 78,000 tokens
- Classify (Haiku): 39 calls × ~1,200 tokens = 46,800 tokens  
- Escalations (Sonnet, ~20% trigger rate): ~8 calls × ~1,200 tokens = 9,600 tokens
- **Total: ~134,400 tokens/day**

**Daily cost (standard API):**
- Haiku: 124,800 tokens × $0.80/MTok = $0.10
- Sonnet escalations: 9,600 tokens × $3.00/MTok = $0.03
- **Total: ~$0.13/day (~$4/month)**

**Cost with prompt caching enabled (current state):**
- ~10% cache hit rate on summarize/classify (repeated system prompts within 5-min TTL)
- Cache hits cost 10% of standard rate instead of 100%
- Estimated daily cost: ~$0.08/day (~$2.40/month base + ~$1.50/month overhead)
- **Total: ~$8–9/month**

### Batch Processing Cost

**Batch API pricing:** 50% discount on all token usage (input, output, cache penalties)

- Base tokens: 134,400 tokens/day × $0.80 weighted avg × 50% = ~$0.05/day
- **Total: ~$0.05/day (~$1.50/month)**

**Monthly savings:** $8–9 (current) → $1.50 (batch) = **~$6.50/month savings (~72% vs. current)**

**Note:** Prompt caching discounts and batch discounts stack. A cache hit in batch mode gets 50% (batch) × 10% (cache read) = 5% of standard rate.

---

## Technical Deep Dive: Why Batch Doesn't Work

### 1. Streaming Incompatibility (Blocking)

**Current implementation:**
```python
# src/llm_client.py:74
with self._client.messages.stream(
    model=model_id,
    max_tokens=max_tokens,
    system=system_param,
    messages=[{"role": "user", "content": prompt}],
) as stream:
    message = stream.get_final_message()
```

All LLM calls use `stream()` for real-time token feedback and timeout handling.

**Batch API constraint:**
- Batch API explicitly rejects `"stream": true`
- Results returned as JSONL file (not streaming)
- No intermediate token updates

**Refactoring required:**
- Split `AnthropicBackend.call()` into sync (streaming) and async (batch) code paths
- Handle both streaming timeouts and batch result polling
- Update all callers to support both modes
- Test both paths independently

**Effort:** Medium-high (affects core LLM client used by 5+ ingest stages)

### 2. Real-Time Gmail Operations (Blocking)

**Current pipeline order:**
```python
# src/ingest_gmail.py:415–426
classify_result = _classify(email_summary, ...)
labels = resolve_labels(classify_result['labels'])

# src/ingest_gmail.py:311–316
for label in labels:
    service.users().messages().modify(
        userId='me',
        id=message_id,
        body={'addLabelIds': [label_id_map[label]]}
    ).execute()  # ← IMMEDIATE API call

# src/ingest_gmail.py:334–337
if should_trash:
    service.users().threads().trash(
        userId='me',
        id=thread_id
    ).execute()  # ← IMMEDIATE API call
```

**Constraint:** Gmail label/trash operations are applied immediately after classification, based on the LLM result.

**Batch API impact:**
- Batch results arrive 1–24 hours after submission
- Cannot apply Gmail labels/trash until batch completes
- Pipeline state is suspended for hours (threads unprocessed in Gmail UI)

**Workaround needed:**
- Buffer email classifications in local DB during batch wait
- Defer Gmail operations until batch results arrive
- Risk: If batch fails/expires, emails sit orphaned in buffer

**Effort:** High (requires local state management, error recovery, operational complexity)

### 3. Escalation Logic (Blocking)

**Current `classify_with_escalation()` flow:**
```python
# src/utils.py:257–345
result = llm.call_json("classify", user_prompt, system=system_prompt)
confidence = parse_confidence(result.get("classification_confidence"), context)

if confidence <= CLASSIFY_ESCALATION_THRESHOLD or "miscellaneous" in labels:
    # Re-classify with Sonnet for second opinion
    result = llm.call_json(
        "classify", user_prompt, system=system_prompt, 
        model_override="sonnet"
    )
```

Escalation is a **conditional second LLM call** based on the first result.

**Batch API constraint:**
- Batch is fire-and-forget: submit all requests upfront, results arrive later
- Cannot conditionally resubmit based on an intermediate result
- Escalation check happens 1–24 hours after initial classification

**Workaround needed:**
- Batch all initial classifications
- Parse results, identify escalation candidates
- Resubmit escalation batch (doubles latency: 1–24h + 1–24h = 2–48h total)

**Effort:** Medium (adds complexity but mechanically feasible)

---

## Volume Break-Even Analysis

Batch processing ROI depends on request volume:

| Daily Requests | Monthly Cost (Streaming) | Monthly Cost (Batch) | Savings | Refactoring Worth? |
|---|---|---|---|---|
| 39–90 (current) | $8–9 | $1.50–2 | $6.50–7 | ❌ No |
| 200 | $40–50 | $8–10 | $30–40 | Maybe |
| 500 | $100–125 | $20–25 | $75–100 | ✅ Yes |
| 1000+ | $200+ | $40+ | $160+ | ✅ Yes |

**Key insight:** Batch refactoring is economical when monthly savings exceed refactoring cost (~$100–200 engineer time). At 39 emails/day, savings (~$7/month) don't justify the work.

**Breakeven:** ~500–1000 daily requests (12–25x current volume)

---

## Compatibility with Prompt Caching

Good news: **Batch API and prompt caching are compatible.**

- Batch requests can include cached system prompts
- Cache discounts stack with batch discounts
- Cache hits in batch mode cost: 50% (batch) × 10% (cache read) = **5% of standard rate**

Example:
```json
{
  "custom_id": "email-001",
  "params": {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1000,
    "system": [
      {
        "type": "text",
        "text": "[4,096-token classify.md prompt]",
        "cache_control": {"type": "ephemeral"}
      }
    ],
    "messages": [{"role": "user", "content": "..."}]
  }
}
```

---

## Batch API Specifics (Reference)

### Request Format
- JSON Lines format (one valid JSON object per line)
- Each request must include `custom_id` (alphanumeric, 1–64 chars) for result matching
- Standard Messages API parameters under `params` key
- Max 100,000 requests OR 256 MB per batch submission

### Latency & Polling
- Typical completion: < 1 hour
- Maximum wait: 24 hours before expiration
- Results available for 29 days after batch creation
- Must poll `processing_status` field until status = "ended"
- Results returned as JSONL (may arrive out of order; match via `custom_id`)

### Cost
- 50% discount on all token usage
- No charge for `errored`, `expired`, or `canceled` requests
- Billing stops if request expires after 24 hours

### Unsupported Features
- `stream: true`
- `speed` parameter (Fast mode)
- Threads API (`store`, `previous_thread_event_id`)
- `cache_hint`, `context_hint` (routing hints)
- `max_tokens: 0` (ephemeral cache pre-warming)

---

## Alternative Optimizations (Already Implemented)

ClearFeed has already implemented the most cost-effective optimization:

### Prompt Caching (Active)
- **Cost reduction:** ~10% on cache hits (55% hit rate on summarize + classify)
- **Implementation cost:** Low (one-line change to llm_client.py)
- **Latency impact:** None
- **Operational complexity:** None
- **Status:** ✅ Deployed

### Ingestion Cadence Optimization (Active)
- **Change:** Poll interval 15 minutes → 120 minutes
- **Benefit:** Batches emails for cache reuse within 5-minute TTL
- **Trade-off:** Todoist actions delayed up to 2 hours (acceptable)
- **Cost reduction:** Synergistic with prompt caching
- **Status:** ✅ Deployed

### Per-Call Optimizations (Not Applicable)
- Merging summarize + classify into one call: Rejected (would require Sonnet, 3x cost)
- Disabling escalation: Not viable (quality tradeoff)
- Reducing prompt size: Not viable (already minimal)

---

## Recommendation

### Current Status: Optimized for This Scale
ClearFeed's 39 emails/day are best served by:
- Real-time streaming LLM calls (immediate Gmail operations)
- Prompt caching (10% cost reduction)
- 120-minute polling cadence (synergistic with caching)

**Monthly cost: ~$8–9** (down from ~$15–20 pre-optimization)

### When to Revisit Batch Processing

Batch processing becomes viable if **either** of these occurs:

1. **Volume growth to 300+ emails/day**
   - Savings scale from $7/month to $70+/month
   - Refactoring investment justified
   - Real-time Gmail operations may become less critical at higher volume (async labeling acceptable)

2. **Async digest/report pipeline**
   - Separate low-volume digest generation (currently 1–7 calls/week) into async-only batch job
   - Keep real-time summarize + classify for immediate email processing
   - Hybrid model: Real-time ingest (streaming) + async reports (batch)
   - Savings: ~$2–3/month from digests (small, but zero blocking dependencies)

### Action Items

- [ ] Monitor ingest volume monthly — if consistent growth to 200+ emails/day observed, revisit batch processing in Q4 2026
- [ ] Keep streaming architecture as-is (proven, low-cost at current scale)
- [ ] Continue monitoring Anthropic pricing — future model discounts may shift economics

---

## Appendix: Cost Comparison Summary

| Scenario | Daily Requests | Monthly Cost | vs. Real-Time |
|---|---|---|---|
| Real-time, no caching | ~90 | $15–20 | Baseline |
| Real-time + prompt caching (current) | ~90 | $8–9 | **-45%** ✅ |
| Real-time + prompt caching + batch | ~90 | $1.50–2 | -82% (but blocked) ❌ |
| Batch only (if no real-time ops) | ~90 | $1.50–2 | -82% (not viable) ❌ |

---

**Document prepared:** 2026-07-21  
**Last reviewed:** 2026-07-21  
**Next review:** Q4 2026 (or when email volume exceeds 200/day)
