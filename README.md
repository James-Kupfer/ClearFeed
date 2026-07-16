# ClearFeed

Personal email-to-action pipeline. Ingests Gmail, enriches each item with LLM
summarization + classification, then dispatches **digests** (topic briefs by
email), **actions** (per-record tasks in external systems like Todoist), and
**exports** (Excel) — all driven by self-contained YAML profiles.

For a code-and-integration map (what to change for a given task), see
[`system_architecture.md`](system_architecture.md).

## Architecture

```
Gmail inbox
   │
   ▼
ingest_gmail.py ──► PostgreSQL (clearfeed)
   summarize+classify   ContentRecords, RecordTerms, DigestRuns, ActionRuns, SourceDocuments
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
   dispatch.py          action_dispatch.py       export.py
   kind: digest         kind: action             kind: export
   → email brief        → Todoist task /…        → .xlsx
```

`ingest_orchestrator.py` runs ingest then any `post_ingest_actions` listed in
`orchestration/ingest.yaml`, but only when new records were ingested. The
continuous loop (`scripts/run_ingestion_service.bat`) calls the orchestrator.

## Profiles

Every profile is a **single self-contained YAML file** in `profiles/`. The
prompt (when present) is always the **last key**, inlined — no separate prompt
files. A top-level `kind:` selects the pipeline:

| `kind` | Pipeline | Module | Output |
|--------|----------|--------|--------|
| `digest` | aggregate many records → one brief | `dispatch.py` | email |
| `action` | per matching record → one task | `action_dispatch.py` | Todoist task (extensible — see `target`) |
| `export` | query records → spreadsheet | `export.py` | .xlsx |

No code changes are needed to add or modify a digest/export, or to add an
action profile for an already-supported `target`.

### Digest profile (`kind: digest`)

```yaml
kind: digest
name: investment_digest
model: haiku                    # haiku | sonnet
recipient: summary@kupfer.me    # optional; falls back to DEFAULT_RECIPIENT
subject_template: "[Investment] Digest — {period_label}"
output: [email]                 # optional; default [email]
inputs:
  - section: Investment
    source: sql                 # sql | digests
    format: markdown            # markdown (default) | json
    sql: |                      # trusted SQL — no user input; single-user tool
      SELECT cr.id, cr.subject, cr.sender, cr.received_at, cr.summary,
             cr.executive_summary, cr.linked_article_url,
             (SELECT STRING_AGG(rt.value, ', ')
                FROM RecordTerms rt WHERE rt.id = cr.id AND rt.kind = 'tag') AS tags
      FROM ContentRecords cr
      WHERE cr.enrichment_status = 'complete'
        AND cr.received_at >= (now() AT TIME ZONE 'America/Chicago') - interval '168 hours'
        AND EXISTS (SELECT 1 FROM RecordTerms rt WHERE rt.id = cr.id
                    AND rt.kind = 'label' AND rt.value = 'Investment')
      ORDER BY cr.received_at DESC LIMIT 200
  - section: Prior Digests     # optional — prior outputs for de-duplication
    source: digests
    from_profile: investment_digest
    window_hours: 74            # time-based limit on prior digests fetched
    limit: 5
prompt: |                       # LAST key — the system prompt for the compose call
  You are composing an Investment digest...
```

**SQL conventions:**
- Always alias the main table `cr` (required for `{{dedup}}` substitution in action profiles).
- Always select `cr.id` and `cr.received_at` (used for DigestRun lineage and period computation).
- Label and tag values are **mixed case** in the DB (`'Investment'`, `'AI'`; tags are lowercase `'actionable'`).
- Use `STRING_AGG` for tags; for a "now" cutoff use `(now() AT TIME ZONE 'America/Chicago')` and `interval` math (e.g. `- interval '240 hours'`), and cap rows with `LIMIT n`.

**Band (`inputs[]`) fields:**

| Field | Where | Description |
|-------|-------|-------------|
| `section` | band | Section name used in the prompt |
| `source` | band | `sql` (profile SQL query) or `digests` (prior DigestRun outputs) |
| `format` | sql band | `markdown` (default, per-record bullets) or `json` (whole result set as fenced JSON blob) |
| `sql` | sql band | Raw SQL executed against the DB — required for `source: sql` |
| `emphasis_tags` | band | Tags to highlight for prioritization (doesn't restrict inclusion) |
| `from_profile` | digests band | Which profile's prior digest outputs to pull |
| `window_hours` | digests band | Time-based lookback cap on prior digests fetched |
| `limit` | digests band | Count cap on prior digests fetched |

### Action profile (`kind: action`)

```yaml
kind: action
name: task_connection
target: todoist                 # selects the per-record handler
mode: per_record                # per_record (default) | aggregate
model: sonnet
trigger:
  sql: |                        # MUST contain {{dedup}} token
    SELECT cr.id, cr.sender, cr.subject, cr.executive_summary,
           cr.body_text, cr.linked_article_url
    FROM ContentRecords cr
    WHERE cr.enrichment_status = 'complete'
      AND cr.processed_at >= (now() AT TIME ZONE 'America/Chicago') - interval '26 hours'
      AND {{dedup}}
      AND EXISTS (SELECT 1 FROM RecordTerms rt WHERE rt.id = cr.id
                  AND rt.kind = 'label' AND rt.value = 'Professional')
      AND EXISTS (SELECT 1 FROM RecordTerms rt2 WHERE rt2.id = cr.id
                  AND rt2.kind = 'tag' AND rt2.value = 'connection_request')
    ORDER BY cr.processed_at DESC LIMIT 50
  prior_actions_hours: 74       # aggregate only: lookback for prior ActionRuns context
todoist:                        # config block NAMED BY `target`
  project: "Professional"
  priority: 3                   # human scale: 1=most urgent, 4=normal; inverted to Todoist REST scale by todoist_client.py; prompt JSON may override
  labels: [clearfeed]
  content: "{action} connection request from {name}"
  description: |
    {description}
prompt: |                       # LAST key — interpolated with record column {placeholders};
  Return STRICT JSON ...        # returned JSON keys become {placeholders} in content/description
```

**`{{dedup}}` token:** at runtime, replaced with:
```sql
NOT EXISTS (SELECT 1 FROM ActionRuns ar WHERE ar.record_id = cr.id AND ar.profile_name = '<name>')
```
This must be present in `trigger.sql` — `_load_profile` rejects profiles without it.

**Modes:**

| Mode | Behavior |
|------|----------|
| `per_record` (default) | One prompt call + one task per record |
| `aggregate` | One prompt call over **all** records; prompt returns `{"actions": [...]}` with `record_ids`; one task per action item |

**Aggregate placeholders** in prompt:
- `{records_json}` — JSON array of all current records
- `{prior_actions_json}` — JSON array of `action_content` strings from recent `ActionRuns` (lookback = `trigger.prior_actions_hours`)

**`priority` in `todoist:` blocks** uses the **human scale (1 = most urgent, 4 = normal)**. `todoist_client.py` inverts it to the Todoist REST API scale (`api_priority = 5 − human_priority`) at the point the request is built. Profiles and prompts never need to express the REST scale.

**Record column names** in `content`/`description` come directly from the SQL `SELECT` column aliases — no separate `inputs:` list. `{placeholders}` also include prompt JSON keys (prompt wins on collision).

### Export profile (`kind: export`)

```yaml
kind: export
name: job_export
sql: |
  SELECT cr.id, cr.subject, cr.sender, cr.processed_at, cr.summary,
         cr.body_text, cr.linked_article_url
  FROM ContentRecords cr
  WHERE cr.enrichment_status = 'complete'
    AND cr.processed_at >= (now() AT TIME ZONE 'America/Chicago') - interval '48 hours'
    AND EXISTS (SELECT 1 FROM RecordTerms rt WHERE rt.id = cr.id
                AND rt.kind = 'label' AND rt.value = 'Professional')
    AND EXISTS (SELECT 1 FROM RecordTerms rt2 WHERE rt2.id = cr.id
                AND rt2.kind = 'tag' AND rt2.value IN ('job_search', 'recruiter'))
  ORDER BY cr.processed_at DESC LIMIT 500
output_path: "O:\\...\\Listings"
filename_template: "job_export_{timestamp}.xlsx"
```

Column headers in the Excel output are derived from `cursor.description` (the SQL `SELECT` column names/aliases) — no separate `columns:` list.

### Adding a new capability

- **New digest / export:** create one `profiles/<name>.yaml`; add a scheduler
  entry. No code.
- **New action for an existing target:** create one `profiles/<name>.yaml` with
  that `target`; add it to `orchestration/ingest.yaml` and/or a scheduler entry.
  No code.
- **New action target (e.g. Playwright):** add a handler + validator (+ client
  factory) to the registries in `action_dispatch.py` — see
  [`system_architecture.md`](system_architecture.md). The shared action pipeline
  is untouched.

## Quick Start

### 1. Create the database
Install PostgreSQL locally, then create the role and database:
```sql
CREATE ROLE clearfeed LOGIN PASSWORD 'clearfeed_local';
CREATE DATABASE clearfeed OWNER clearfeed;
```
Apply the schema:
```bat
psql -U clearfeed -d clearfeed -f schema_postgres.sql
```
Connection settings (host/port/name/user) are in `src/config.py`; the password
is a secret in `Secrets/db_keys.py` (`DB_PASSWORD`). To migrate existing
data from the legacy SQL Server instance, run
`python scripts/migrate_sqlserver_to_postgres.py`.

### 2. Secrets
API keys/tokens live in `Secrets/*.py`, imported by `security_config.py`
(gitignored). Populate `ANTHROPIC_API_KEY`, Gmail OAuth/app-password values,
`TODOIST_API_TOKEN`, and `DB_PASSWORD`.

### 3. Gmail API (one-time)
Enable the Gmail API in Google Cloud Console, create OAuth 2.0 Desktop
credentials, download `client_secret.json`; the token caches on first run.

### 4. Install dependencies
```bat
pip install -r requirements.txt
```

### 5. Run
```bat
:: Ingest new Gmail threads
clearfeed.bat ingest

:: Dispatch a digest
clearfeed.bat dispatch profiles\investment_digest.yaml

:: Run an action profile (per-record tasks)
clearfeed.bat action profiles\task_connection.yaml

:: Export to Excel
clearfeed.bat export profiles\job_export.yaml

:: Re-run classify / summarize on stored records (see Reprocess)
clearfeed.bat reprocess --label Investment
clearfeed.bat reprocess-summary --all

:: Full run (tests + ingest + all profiles)
master.bat
```

## Reprocess

`reprocess` re-runs **only** the Haiku classify call against each record's
stored `summary`, overwriting labels/tags/classification fields and bumping
`processed_at`. Use after changing `prompts/classify.md`.
`reprocess-summary` is the summarize-step counterpart (re-runs against stored
`body_text`); use after changing `prompts/summarize.md`.

| Flag | Description |
|------|-------------|
| `--label <name>` | Records carrying this label |
| `--tag <name>` | Records carrying this tag |
| `--days <n>` | Received in the last N days |
| `--since <YYYY-MM-DD>` | Received on or after this date |
| `--all` | All records, no filter |
| `--gaps` | Only records missing an output of this step (aborted-run recovery) |

Filters AND together; at least one is required. A typical prompt-change refresh
is `reprocess-summary` then `reprocess`. Convenience runners in `scripts/`
(`run_resummarization.bat`, `run_reclassification.bat`, `run_reprocess.bat`;
default `--days 4`).

## Scheduling (Windows Task Scheduler)

Task XMLs live in `scripts/tasks/`. Each scheduled task invokes a **hidden**
launcher with a **bare profile name** (no extension/stage) — the launcher
supplies the stage and `.yaml`, so converting profiles required **no task
edits**.

| Launcher | Used by | Invokes |
|----------|---------|---------|
| `launch_hidden.vbs` | Ingestion_Service | `run_ingestion_service.bat` (orchestrator loop) |
| `launch_digest.vbs <name>` | the digest tasks | `clearfeed.bat dispatch profiles\<name>.yaml` |
| `launch_action.vbs <name>` | action tasks | `clearfeed.bat action profiles\<name>.yaml` |

## Configuration

All tunables in [`src/config.py`](src/config.py):

| Setting | Purpose |
|---------|---------|
| `LLM_ROUTING` | Model per operation (`summarize`/`classify`/`digest`/`synthesis`/`action`) |
| `LLM_MAX_TOKENS` / `DIGEST_MAX_TOKENS` | Output token caps |
| `INGEST_POLL_INTERVAL_MINUTES` | Orchestrator loop interval |
| `EMAIL_INGEST_LOOKBACK_DAYS` | Inbox lookback window |
| `TODOIST_BASE_URL` | Todoist REST v1 base |
| `LOG_RETENTION_DAYS` | Auto-purge age for logs |

## Project Layout

```
ClearFeed/
├── src/
│   ├── config.py             # all settings
│   ├── db.py                 # PostgreSQL client (psycopg) + band queries
│   ├── gmail_client.py       # Gmail API + SMTP
│   ├── llm_client.py         # Anthropic API wrapper + routing
│   ├── utils.py              # logging, retry, tag norm, read_yaml_profile (shared YAML parse)
│   ├── ingest_gmail.py       # Stage 1 — ingest + summarize + classify
│   ├── ingest_orchestrator.py# ingest + post-ingest actions (loop entry)
│   ├── dispatch.py           # kind: digest — compose + email
│   ├── action_dispatch.py    # kind: action — per-record tasks; target registry
│   ├── todoist_client.py     # Todoist REST v1 wrapper
│   ├── export.py             # kind: export — query → .xlsx
│   ├── reprocess.py          # re-run classify on stored records
│   └── reprocess_summary.py  # re-run summarize on stored records
├── prompts/                  # ingest-stage prompts only: summarize.md, classify.md
├── profiles/                 # one self-contained .yaml per profile
├── orchestration/ingest.yaml # post_ingest_actions list
├── scripts/                  # launchers (.vbs), per-profile + helper .bat, tasks/*.xml
├── images/                   # downloaded images (gitignored)
├── logs/                     # timestamped run logs (gitignored)
├── tests/
├── schema_postgres.sql       # PostgreSQL DDL (incl. SourceDocuments — full original source)
├── security_config.py        # imports secrets from Secrets/ — gitignored
├── clearfeed.bat             # stage runner (ingest|dispatch|action|export|reprocess|…)
└── master.bat                # full pipeline (tests + ingest + dispatch all)
```

## v2 Roadmap

- **Playwright action target** — browser automation as a drop-in `target`
  (handler stub + extension point already in `action_dispatch.py`).
- File intake (`_intake/` watch for PDF/DOCX/EML/TXT).
- Article scraping; vision LLM on stored images; DOCX report renderer.
