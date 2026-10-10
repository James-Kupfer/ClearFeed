# Change Log

## 2026-10-10 - Auto-reply to and trash declined connection requests

**Type**: feature
**Files**: src/action_dispatch.py, src/gmail_client.py, profiles/task_connection.yaml, tests/test_action_dispatch.py, tests/test_gmail_reply.py, README.md, system_architecture.md

`task_connection` no longer creates a Todoist task when the prompt's `action` is `Decline`. Instead ClearFeed emails the sender a configurable message (`auto_reply.body` in the profile) as a threaded reply, then moves the original Gmail message to Trash. Other actions (Accept, Review, Respond, Meeting) are unchanged.

New generic `auto_reply:` profile block (per_record mode only) routes matching records to `_handle_auto_reply` in place of the target handler. `ActionRuns.target` is `gmail_reply` for these rows. The trash is message-level (`GmailClient.trash_message`), not thread-level, so the sent reply is not trashed with it. `GmailClient.send_reply` is deliberately not retried. If the send succeeds but the trash fails, the record stays `created` with the error recorded, so it is never re-sent.

Header values are collapsed to one line before use to block header injection from a hostile subject or Message-ID. The Decline Message block inside the `task_connection` LLM prompt is now unused by this path and was left as is.

## 2026-10-07 - Retry transient Todoist errors; strip NUL bytes before insert

**Type**: fix
**Files**: src/todoist_client.py, src/db.py, tests/test_todoist_retry.py, tests/test_ingest.py, system_architecture.md

Todoist POST/GET responses with HTTP 429 or 5xx were raised as `TodoistError`, which the `@retry` decorator (catching only `requests.RequestException`) never retried. One 503 therefore wrote a permanent `failed` ActionRun (record 4956, `task_connection`). New `TodoistTransientError` (a `TodoistError` subclass) is raised for 429/5xx and retried with the existing backoff; 4xx still fails immediately. Retrying a POST on 503 can in theory duplicate a task the server created before erroring.

`db.insert_content_record` now strips NUL (0x00) bytes from every text field, the label/tag lists, and the raw source before inserting. PostgreSQL text columns reject NUL, so one email (thread 1a116ac78a06f331) failed the insert on every ingest cycle and was never stored.

## 2026-09-26 - Pin xhtml2pdf in requirements

**Type**: fix
**Files**: requirements.txt

`dispatch._render_digest_pdf` imports `xhtml2pdf`, but it was not listed in requirements. On a fresh install, every digest's PDF render failed and the email was sent without its attachment. Pinned to 0.2.20, the version used to verify the 7pt tag rendering. `pip check` reports no conflicts with the pinned Pillow 12.2.0.

## 2026-09-26 - Digest tag lines rendered at 7pt gray

**Type**: feature
**Files**: src/dispatch.py, src/config.py, tests/test_dispatch.py, README.md

Tag lines rendered at body size, which is 12pt in the PDF and the mail client's default in email. `_style_tag_lines` now inlines `font-size` and `color` on each `<p><em>Tags:</em>` paragraph when both the email body and the PDF are rendered. The values are `config.DIGEST_TAG_FONT_PT` and `config.DIGEST_TAG_COLOR`. The stored `DigestRuns.summary_text` is not changed.

Rejected: a CSS class in a `<style>` block, because many mail clients strip head styles. Rejected: asking the LLM to emit the style, because it is non-deterministic across seven profiles.

## 2026-09-26 - Admit AI jailbreak and AI-attack stories; keep prompt injection excluded

**Type**: fix
**Files**: profiles/digest_technology.yaml

Moved `jailbreaking`, `ai_enabled_attacks`, and `shadow_ai` from the unconditional exclusion to the conditional one. They now exclude a record only when it has no AI-security tag, the same rule as `cybersecurity`. At the user's direction, `prompt_injection` stays unconditionally excluded.

## 2026-09-26 - Keep AI security stories while excluding cyber patching content

**Type**: fix
**Files**: profiles/digest_technology.yaml, profiles/digest_science.yaml, profiles/digest_miscellaneous.yaml, prompts/classify.md

Technology's security-tag exclusion also blocked AI security stories. For example, OpenAI agents breaching Australian government servers carried the broad `cybersecurity` tag, was excluded from Technology, and then was dropped by Miscellaneous under the ownership map, so it appeared in no digest. The exclusion was meant for patching and vulnerability content such as CVEs.

- Split the exclusion. Patching, vulnerability, and attack-technique tags (`cve`, `vulnerability`, `rce`, `zero_day`, and similar) still exclude a record unconditionally, even when it involves an AI product. `cybersecurity` now excludes a record only if it has no AI-security tag (`ai_security`, `ai_safety`, `ai_regulation`, `ai_governance`, `alignment`).
- Added `ai_security` to Technology's tag routing and to the classifier's Technology tag registry. The classifier was already emitting it.
- The ownership map now names AI security explicitly and puts vulnerability disclosures, CVEs, and patch advisories out of scope for every digest.

Rejected: removing `cybersecurity` from the exclusion. Generic security news without an AI angle would return.

## 2026-09-26 - Cross-digest topic ownership and tag routing

**Type**: fix
**Files**: profiles/digest_technology.yaml, profiles/digest_science.yaml, profiles/digest_miscellaneous.yaml, prompts/classify.md, tests/test_dispatch.py, system_architecture.md

AI stories were split across digests by theme. Each email gets a single subject label, so AI facets inside Politics newsletters reached Miscellaneous, and AI-driven discoveries appeared in both Technology and Science. The 2026-09-26 run showed UN AI governance, AI-lab breaches, and US AI policy in both Technology and Miscellaneous, and Claude's phage-enzyme finding in both Technology and Science.

- Added an identical `TOPIC OWNERSHIP` block to the Technology, Science, and Miscellaneous prompts. Technology owns AI systems, safety, governance, and policy. Science owns scientific findings, including those produced by AI. Miscellaneous owns politics and culture where AI is incidental.
- Technology's SQL also pulls records labelled Science, Miscellaneous, Politics, Culture, or Health that carry `ai_safety`, `ai_regulation`, `ai_governance`, or `alignment`. Miscellaneous and Science drop those facets, and Technology receives the record.
- Science's SQL also pulls Technology-labelled records that carry both a science-domain tag and an AI-research tag.
- Added `ai_governance` to the classifier's Technology tag registry. The classifier was already emitting it.

Rejected: cross-digest story dedup, meaning feeding earlier digests' output into later ones. Most of the overlap was the same theme across different events, so story-level dedup would keep both halves. Rejected: ownership rules without routing, because the non-owning digest would drop facets the owner never received. Deferred: splitting emails into stories at ingest. It fixes the root cause but needs a schema change and a backfill.
