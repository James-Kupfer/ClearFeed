# ClearFeed — System Architecture

Audience: whoever (human or agent) is about to change ClearFeed. This is the
map that lets you find the one place to edit without re-reading the whole
codebase. For user-facing usage see [`README.md`](README.md).

---

## 1. Pipeline overview

Four stages, each independently invokable via `clearfeed.bat <stage>` and
schedulable. PostgreSQL (`clearfeed`, local) is the shared source of truth.

```
                 ┌──────────────┐
 Gmail inbox ───►│ ingest_gmail │── summarize (Haiku) ─► classify (Haiku, may escalate)
                 └──────┬───────┘
                        ▼  writes ContentRecords (+ RecordTerms), trashes thread
                 PostgreSQL: clearfeed
                        │
   ┌────────────────────┼─────────────────────┬───────────────────────┐
   ▼                     ▼                     ▼                       │
dispatch.py        action_dispatch.py       export.py                 │
kind: digest        kind: action            kind: export              │
reads records/      scans pending records,  queries records →         │
digests → 1 email   per-record prompt →     .xlsx                     │
writes DigestRuns   target handler;                                   │
                    writes ActionRuns                                  │
                        ▲                                              │
                        └── ingest_orchestrator.py runs ingest, then ──┘
                            post_ingest_actions every cycle regardless
                            of whether new records were ingested
```

| Stage | Entry | Reads | Writes |
|-------|-------|-------|--------|
| Ingest | `ingest_gmail.run_ingest` | Gmail | `ContentRecords`, `RecordTerms`; trashes thread |
| Digest | `dispatch.run_dispatch` | `ContentRecords`, `DigestRuns` | email; `DigestRuns` |
| Action | `action_dispatch.run_action` | `ContentRecords`, `ActionRuns` | external task; `ActionRuns` |
| Export | `export.run_export` | `ContentRecords` | `.xlsx` |
| Orchestrate | `ingest_orchestrator.run_cycle` | `orchestration/ingest.yaml` | (delegates) |

### Database tables (`schema_postgres.sql`)

| Table | Role |
|-------|------|
| `ContentRecords` | One row per ingested item: sender/subject/body, `summary`, `executive_summary`, confidence/rationale, `enrichment_status`, timestamps |
| `RecordTerms` | Label/tag terms per record (`kind` ∈ {label, tag}); joined for filtering |
| `DigestRuns` | One row per digest send: profile, period, summary HTML, content/prior-digest ids |
| `ActionRuns` | One row per action attempt: `(profile_name, record_id)` UNIQUE → idempotency; `target`, `external_id`, `status`, `action_content` (aggregate mode: stores the action JSON for prior-action context) |
| `SourceDocuments` | Full original source, untruncated: `raw_html` / `raw_text`, shared PK with `ContentRecords` (1:1). Captured at ingest before stripping/truncation |

---

## 2. Module map (`src/`)

| Module | Responsibility | Key public surface | Called by |
|--------|----------------|--------------------|-----------|
| `config.py` | All tunables, paths, `LLM_ROUTING` | constants | everything |
| `db.py` | PostgreSQL access (psycopg) | `get_connection`, **`query_sql`** (trusted profile SQL), `query_digests_for_band`, `insert_digest_run`, `query_prior_actions` | dispatch, action, export, reprocess |
| `gmail_client.py` | Gmail API + SMTP | `GmailClient`, `send_email` | ingest, dispatch |
| `llm_client.py` | Anthropic wrapper + model routing | `LLMClient.call`, `.call_json` (routes via `LLM_ROUTING`) | ingest, dispatch, action |
| `utils.py` | Cross-cutting helpers | **`read_yaml_profile`** (single YAML-parse site), `setup_logging`, `retry`, `normalize_tag`, `now_cst`, summarize/classify helpers | everything |
| `ingest_gmail.py` | Stage 1 | `run_ingest` | orchestrator, CLI |
| `ingest_orchestrator.py` | ingest + post-ingest actions | `run_cycle` | `run_ingestion_service.bat` |
| `dispatch.py` | `kind: digest` | `run_dispatch`, `_load_profile`, `_resolve_bands`, `_build_prompt` | CLI / launcher |
| `action_dispatch.py` | `kind: action` | `run_action`, `_load_profile`, `_TARGET_HANDLERS/_VALIDATORS/_CLIENTS`; `_run_aggregate` (aggregate mode) | CLI / launcher / orchestrator |
| `todoist_client.py` | Todoist REST v1 | `TodoistClient` (`resolve_project_id`, `create_task`; `_unwrap_list`) | action_dispatch |
| `export.py` | `kind: export` | `run_export`, `_load_profile`; columns derived from SQL `cursor.description` | CLI |
| `reprocess.py` / `reprocess_summary.py` | Re-run classify / summarize on stored records | CLI | — |

**Shared seams** (edit once, affects many): `utils.read_yaml_profile` (all
profile parsing), `LLMClient` + `config.LLM_ROUTING` (all model selection),
`db.query_sql` (trusted execution seam — all profile SQL runs through here).

> **Trusted SQL note:** `query_sql` executes profile-supplied SQL directly with no sanitization. This is an intentional design exception — ClearFeed is a single-user local tool; all SQL originates from `profiles/` YAML files in the local repo, never from user input at runtime.

---

## 3. Profile system

All profiles are self-contained YAML in `profiles/`, parsed by the single
`utils.read_yaml_profile`. Two **orthogonal discriminators**:

1. **`kind:`** (top level) selects the *pipeline*: `digest` | `action` | `export`.
   Each module's `_load_profile` validates `kind` matches and does its own
   schema checks. There is intentionally **no central router** — the stage
   (`clearfeed.bat dispatch|action|export`, chosen by the launcher) already
   selects the module.
2. **`target:`** (inside `kind: action`) selects the per-record *handler*:
   `todoist` (implemented) | `playwright` (stub). The action pipeline (scan →
   prompt → dedup) is shared; only the final step varies by handler.

Flow of a profile:

```
profiles/X.yaml ─► read_yaml_profile ─► <module>._load_profile (validate)
   digest  ─► resolve bands (query_sql / query_digests_for_band)
              ─► build prompt (markdown or fenced JSON per band format)
              ─► LLM.call ─► email + DigestRuns
   action  ─► query_sql(trigger.sql with {{dedup}} substituted)
   per_record: ─► per record: LLM.call_json ─► _TARGET_HANDLERS[target](...) ─► ActionRuns
   aggregate:  ─► one LLM.call_json({records_json}, {prior_actions_json})
              ─► parse {"actions": [...]} ─► one task per action ─► ActionRuns (action_content stored)
   export  ─► query_sql(profile.sql, with_columns=True) ─► .xlsx
```

The prompt is always the **last key**, inlined. (Digests inline their full
prompt — the two action-digest profiles each carry their own copy.)

---

## 4. Extension recipes ("to change X, edit Y")

| Goal | Edit |
|------|------|
| **Add a digest** | New `profiles/<name>.yaml` (`kind: digest`, prompt last). Add a `scripts/tasks/*.xml` pointing `launch_digest.vbs <name>`. No code. |
| **Add an export** | New `profiles/<name>.yaml` (`kind: export`) with a `sql:` key. Column headers come from the SQL `SELECT` aliases. No code. |
| **Add an action (existing target)** | New `profiles/<name>.yaml` (`kind: action`, `target: todoist`). SQL in `trigger.sql` must contain `{{dedup}}`. Add to `orchestration/ingest.yaml` `post_ingest_actions` and/or a task XML. No code. |
| **Add an aggregate action** | Same as above but add `mode: aggregate`. Prompt receives `{records_json}` and `{prior_actions_json}`; must return `{"actions": [...]}` with `record_ids`. Set `trigger.prior_actions_hours` to control lookback for prior-action context. |
| **Add an action target (e.g. Playwright)** | In `action_dispatch.py`: write `_handle_playwright(record, profile, prompt_result, clients)` returning `ActionResult`; write `_validate_playwright(cfg)`; register both in `_TARGET_HANDLERS` / `_TARGET_VALIDATORS`; if it needs a client, add a factory to `_TARGET_CLIENTS`. Profiles then use `target: playwright` + a `playwright:` config block. **Pipeline, launchers, and tasks are untouched.** |
| **Expose new ContentRecord columns to action prompts** | Add the column to the `SELECT` list in the profile's `trigger.sql`. No code. |
| **Expose new ContentRecord columns to digest LLM** | Add the column to the `SELECT` in the profile's `sql:` band. Render it in `dispatch._build_prompt` only if it needs special formatting beyond the default row rendering. |
| **Change which model an operation uses** | `config.LLM_ROUTING` (per-profile override: the profile's `model:` field). |
| **Change ingest/orchestrator cadence** | `config.INGEST_POLL_INTERVAL_MINUTES`; the task XML triggers for digests/actions. |
| **Add a secret / integration key** | Add to `Secrets/<name>.py`; import it in `security_config.py`. Never inline secrets. |
| **Add a new pipeline kind** | New `src/<x>.py` with `_load_profile` (validate `kind`); a `clearfeed.bat` stage; a launcher if scheduled. |

---

## 5. Integration points

| System | Where | Auth / secret |
|--------|-------|---------------|
| Gmail | `gmail_client.py` (API read/label/trash + SMTP send) | OAuth client secret + token cache; app password — in `Secrets/` |
| Anthropic | `llm_client.py` | `ANTHROPIC_API_KEY` (`Secrets/`) |
| Todoist | `todoist_client.py` — REST **v1** (`config.TODOIST_BASE_URL`). Responses may be wrapped `{"results": [...]}`; `_unwrap_list` normalizes both bare lists and envelopes. Missing projects are auto-created. | `TODOIST_API_TOKEN` (`Secrets/todoist_keys.py`) |
| PostgreSQL | `db.py` (`localhost:5432`, DB `clearfeed`) | host/port/name/user in `config.py`; `DB_PASSWORD` in `security_config.py` |

Secrets pattern: real values live in `Secrets/*.py`; `security_config.py`
(gitignored) imports them; `config.py` reads from there.

---

## 6. Orchestration & scheduling

- **`orchestration/ingest.yaml`** — `post_ingest_actions:` lists action profiles
  to run each cycle. Add a profile path to wire it into ingestion.
- **`ingest_orchestrator.run_cycle`** — runs `run_ingest()`, then runs each
  listed action profile via `run_action` **unconditionally every cycle**. Actions
  handle their own deduplication via the `{{dedup}}` NOT EXISTS clause in `trigger.sql`
  and use a long lookback window in their SQL so missed runs catch up automatically.
- **`scripts/run_ingestion_service.bat`** — infinite loop calling the
  orchestrator every `INGEST_POLL_INTERVAL_MINUTES`; launched hidden by
  `launch_hidden.vbs`.
- **Scheduled tasks** (`scripts/tasks/*.xml`) pass a **bare profile name** to a
  hidden launcher (`launch_digest.vbs` / `launch_action.vbs`); the launcher
  supplies the stage and `.yaml` extension. Because the extension lives only in
  the launcher, profile format changes need **no task edits**.

---

## 7. Known gaps / TODO

- **ActionRuns dedup excludes failed records.** A transient failure (e.g. Todoist
  503) writes an `ActionRun` with `status='failed'`, which permanently excludes
  that record from future runs. To retry, delete the row from `ActionRuns` where
  `profile_name = '<profile>' AND record_id = <id>`. A retry-on-transient-failure
  mechanism is not yet implemented.
