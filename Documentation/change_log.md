# Change Log

## 2026-10-12 - Shorter task titles for Review and Meeting connection tasks

**Type**: change
**Files**: profiles/task_connection.yaml

`task_connection` task titles: Review → "Connection request - <name>" (was "Review connection request from <name>"); Meeting → "Meeting request - <name>" (was "Propose meeting time with <name>"). Accept, Decline and Respond titles are unchanged. Nothing in `src/` parses these titles.

## 2026-10-12 - Daily heartbeat for task_rights_offering; action profiles run when Gmail ingest fails

**Type**: fix
**Files**: src/action_dispatch.py, src/ingest_orchestrator.py, src/db.py, schema_postgres.sql, profiles/task_rights_offering.yaml, tests/test_action_dispatch.py, README.md, system_architecture.md

Rights-offering task creation stopped with no signal: a run that found no records only logged "Found 0 record(s)", so a broken run and a quiet day looked identical. New optional `heartbeat:` profile block creates one Todoist task per US-Central day on the profile's first run of the day, whether it found nothing, failed to query, or raised. It lists records found/created/failed, any error, and the output of an optional `stats_sql` (for `task_rights_offering`: feed record count, how many never reached `enrichment_status = 'complete'`, records ingested in the last 24 h, latest `received_at`/`ingested_at`). Quiet days are P4; any problem is P1 with `NEEDS ATTENTION` in the title. The day is claimed in the new `HeartbeatRuns` table (auto-created on first use) before the task is made; a failed creation releases the claim so the next cycle retries. If the claim itself fails (DB error) no heartbeat is sent that cycle.

Optional `after_hour` (US-Central, 0-23) delays the heartbeat until the first cycle at or after that hour; `task_rights_offering` uses 20 so the task is sent after the 19:00 weekday SEC Updates job (SEC_Monitor + Rights_Offering_Monitor) rather than at ~1am. Diagnosis on 2026-10-12: that feed is healthy — the last offering (Rent the Runway, 2026-10-07) was ingested and tasked; the Oct 8-9 ROM runs correctly skipped every filing as not a rights offering. The 'stopped' symptom was silence on quiet days.

Also fixed: `ingest_orchestrator.run_cycle` returned as soon as Gmail ingest raised, so no action profile ran while ingest was failing. Action profiles read records already in the database (the rights-offering records are written by another system, not Gmail), so they now run regardless, and the heartbeat reports the ingest failure.

Not established: why no rights-offering tasks were created. The trigger needs records with `source_type = 'SEC_Edgar_rights_offering'` and `enrichment_status = 'complete'`, written by a process outside this repository. The first heartbeat shows whether that feed is writing records and whether they reach `complete`.

## 2026-10-11 - Trash LinkedIn job alerts after export

**Type**: feature
**Files**: src/config.py, src/ingest_gmail.py, src/db.py, src/gmail_client.py, src/export.py, profiles/job_export.yaml, tests/test_ingest.py, tests/test_export.py, tests/test_gmail_trash.py, README.md

Emails from `jobalerts-noreply@linkedin.com` used to be trashed at ingest, before `job_export` ran. New `config.TRASH_DEFERRED_SENDERS` makes ingest label and store them but leave them in the inbox (status `kept`). New optional export-profile block `trash_after_export: {sender}` (set in `job_export.yaml`) trashes the Gmail message of each exported row from that sender after the `.xlsx` is saved, via `db.query_gmail_refs` + `GmailClient.trash_message`. The export also sweeps `from:<sender> in:inbox label:ProcessedClearFeed` (`GmailClient.list_message_ids`), including when no rows matched. Reason: Gmail appends repeat alerts to an existing thread, ingest reads only the thread's first message and treats the thread as a duplicate, so it labels the new messages processed but never trashed them (16 such threads found in the inbox). Failures are logged and retried by the next export. 

**Ingest now stores every message of a thread** (follow-up, same day). `_ingest_thread` loops over all messages; each becomes its own record (source_ref = message id) and already-stored messages are skipped, so alerts Gmail appends to an old thread are classified and exported. Stored messages are trashed per message (`trash_message`, not `trash_thread`), so an older kept message in the same thread is untouched; messages that are trash-excluded or from a deferred sender stay in the inbox. Spam and security/promo messages are also trashed per message. If any message fails (e.g. no body), the others are still stored, labeled and trashed, but the thread is not marked ProcessedClearFeed so the failed one is retried. `IngestResult.record_id` is the first newly stored record of the thread.

**One-off backfill**: the 15 inbox threads already holding appended alerts are labeled ProcessedClearFeed, so normal runs skip them. Run `clearfeed.bat ingest --include-processed` once (`fetch_ingest_threads(include_processed=True)` drops the label filter; stored messages are skipped by message id). Do not dry-run it first: a dry run stores the records, and the real run would then see those messages as duplicates and not trash them.

## 2026-10-10 - Random delay before clicking Ignore

**Type**: feature
**Files**: src/linkedin_client.py, src/action_dispatch.py, profiles/task_connection.yaml, tests/test_linkedin_client.py, README.md

New optional `auto_decline.linkedin_ignore.delay_seconds: [min, max]`; `task_connection` sets `[5, 25]`. After the profile page has loaded, the browser waits a random `min..max` seconds (uniform) before clicking Ignore, and logs the wait. Interpreted as seconds. Each Decline therefore takes 5-25 s longer; declines are processed one after another within a cycle.

## 2026-10-10 - Decline = ignore on LinkedIn + trash; drop the reply

**Type**: feature (supersedes the same-day auto-reply entries below)
**Files**: src/action_dispatch.py, src/linkedin_client.py, src/gmail_client.py, profiles/task_connection.yaml, clearfeed.bat, tests/test_linkedin_client.py, tests/test_gmail_trash.py, tests/test_action_dispatch.py, README.md, system_architecture.md

LinkedIn's notification emails have no return address and messaging a pending inviter goes through an InMail-style subject+body composer, so replying is dropped. `task_connection` now handles `action: Decline` with a new `auto_decline:` block: open `review_url`, click the `Ignore` button, confirm it disappeared, then trash the Gmail message. No message is sent. If the ignore can't be done and confirmed, nothing is trashed and the normal Todoist task is created.

Removed: the `auto_reply:` block and its Gmail and LinkedIn reply channels, `GmailClient.send_reply` / `get_message_headers`, `linkedin_client.send_message` / `--try-reply`, the `linkedin-try-reply` stage, and `skip_addresses`. They remain in git history at 9ecd45b. Kept: `linkedin-login`, `linkedin-inspect`, `trash_message`. `ActionRuns.target` is `linkedin_ignore` for these rows. The Ignore label ("Ignore <name>'s request to connect") was confirmed by hand on real invitation pages; the automated click is verified only against a local stand-in page.

## 2026-10-10 - Ignore via the profile page's Ignore button

**Type**: fix
**Files**: src/linkedin_client.py, profiles/task_connection.yaml

On a real invitation profile page Ignore is a button, not a link, so there is no URL to extract. `task_connection` now opens `review_url` (the profile link from the email) and clicks `Ignore` (`linkedin_ignore.click_text`). After the click the step checks the button is gone and otherwise raises, so an unconfirmed ignore is recorded in `ActionRuns.error`. Exercised against a local stand-in page only.

## 2026-10-10 - Reply to declined connection requests on LinkedIn instead of by email

**Type**: feature
**Files**: src/linkedin_client.py, src/action_dispatch.py, profiles/task_connection.yaml, clearfeed.bat, tests/test_linkedin_client.py, README.md, system_architecture.md

LinkedIn's notification emails have no usable return address, so `auto_reply` gains `channel: linkedin`, now used by `task_connection`. On Decline: `linkedin_client.send_message` opens the invitation's `review_url` in the signed-in browser profile, types the configured body into the message box, sends it, and confirms the composer cleared and the text appears; then the `linkedin_ignore` link is opened and the Gmail message is trashed. If the reply can't be sent and confirmed, nothing else is done and the normal Todoist task is created. After a confirmed send, ignore/trash failures are recorded in `ActionRuns.error` and not retried; `ActionRuns.target` is `linkedin_reply`. `channel: gmail` remains the default.

New `clearfeed.bat linkedin-try-reply "<link>"` types the message in a visible browser without sending. The send flow was exercised only against a local stand-in page: LinkedIn's real page structure (message box name, whether a button must be clicked first, whether `review_url` lands on a replyable page) is unverified.

## 2026-10-10 - Never auto-reply to LinkedIn relay addresses

**Type**: fix
**Files**: src/action_dispatch.py, profiles/task_connection.yaml, tests/test_action_dispatch.py, README.md, system_architecture.md

Seven prior `task_connection` records were all sent from `invitations@linkedin.com` or `messages-noreply@linkedin.com`. With no Reply-To, the auto-reply would have gone to LinkedIn, never the person, while the email was still trashed and the request ignored. New `auto_reply.skip_addresses` (fnmatch globs) excludes such addresses; if no deliverable address remains, `_handle_auto_reply` returns None having done nothing and the normal Todoist task is created. A message with a Reply-To for a real person still gets the reply. An unusable address previously failed the record; it now falls back to the task too.

## 2026-10-10 - Ignore declined connection requests on LinkedIn

**Type**: feature
**Files**: src/linkedin_client.py, src/action_dispatch.py, src/config.py, profiles/task_connection.yaml, clearfeed.bat, requirements.txt, .gitignore, tests/test_linkedin_client.py, README.md, system_architecture.md

After the decline reply is sent and the email trashed, `task_connection` opens the request's `decline_url` in a persistent, signed-in Playwright Chromium profile so LinkedIn archives it. New optional `auto_reply.linkedin_ignore` block (`url_key`, `click_text`, `not_url_key`). Sign in once with `clearfeed.bat linkedin-login`.

Only https `linkedin.com` URLs are opened (the URL is LLM-extracted from an email body), and the step is refused if `decline_url` equals `accept_url`. A signed-out session is detected and reported. Failures are recorded in `ActionRuns.error`, not retried, and leave the status `created` because the reply already went out. Verified against a local stand-in page only, not LinkedIn; LinkedIn's terms prohibit automation.

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
