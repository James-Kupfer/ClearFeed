# Change Log

## 2026-09-26 - Cross-digest topic ownership and tag routing

**Type**: fix
**Files**: profiles/digest_technology.yaml, profiles/digest_science.yaml, profiles/digest_miscellaneous.yaml, prompts/classify.md, tests/test_dispatch.py, system_architecture.md

AI stories were split across digests by theme. Each email gets a single subject label, so AI facets inside Politics newsletters reached Miscellaneous, and AI-driven discoveries appeared in both Technology and Science. The 2026-09-26 run showed UN AI governance, AI-lab breaches, and US AI policy in both Technology and Miscellaneous, and Claude's phage-enzyme finding in both Technology and Science.

- Added an identical `TOPIC OWNERSHIP` block to the Technology, Science, and Miscellaneous prompts. Technology owns AI systems, safety, governance, and policy. Science owns scientific findings, including those produced by AI. Miscellaneous owns politics and culture where AI is incidental.
- Technology's SQL also pulls records labelled Science, Miscellaneous, Politics, Culture, or Health that carry `ai_safety`, `ai_regulation`, `ai_governance`, or `alignment`. Miscellaneous and Science drop those facets, and Technology receives the record.
- Science's SQL also pulls Technology-labelled records that carry both a science-domain tag and an AI-research tag.
- Added `ai_governance` to the classifier's Technology tag registry. The classifier was already emitting it.

Rejected: cross-digest story dedup, meaning feeding earlier digests' output into later ones. Most of the overlap was the same theme across different events, so story-level dedup would keep both halves. Rejected: ownership rules without routing, because the non-owning digest would drop facets the owner never received. Deferred: splitting emails into stories at ingest. It fixes the root cause but needs a schema change and a backfill.
