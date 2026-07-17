-- ClearFeed schema (SQL Server). Run once against the ClearFeed database.
-- Idempotent: guarded with IF NOT EXISTS so re-running is safe.
USE [ClearFeed];

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'ContentRecords')
BEGIN
    CREATE TABLE ContentRecords (
        id                  INT IDENTITY    NOT NULL PRIMARY KEY,
        source_type         VARCHAR(20)     NOT NULL,        -- gmail | file | manual
        source_ref          VARCHAR(500)    NOT NULL,        -- Gmail message_id or file path
        received_at         DATETIME2       NULL,            -- original receive / file-modified time
        sender              VARCHAR(500)    NULL,            -- From address; NULL for file sources
        subject             VARCHAR(1000)   NULL,            -- email subject or filename
        body_text           NVARCHAR(MAX)   NULL,            -- stripped plain text (retained permanently)
        linked_article_url  VARCHAR(2000)   NULL,            -- primary URL extracted from body
        image_local_paths   NVARCHAR(MAX)   NULL,            -- JSON array of stored image paths
        image_descriptions  NVARCHAR(MAX)   NULL,            -- JSON array of vision output (v2; NULL in v1)
        summary             NVARCHAR(MAX)   NULL,            -- LLM summary (set at ingest)
        enrichment_status   VARCHAR(20)     NOT NULL DEFAULT 'pending',  -- pending | complete | failed
        enrich_attempt_count INT            NOT NULL DEFAULT 0,
        processed_at        DATETIME2       NULL,            -- when LLM fields were last computed (drives reprocess)
        ingested_at         DATETIME2       NOT NULL DEFAULT CONVERT(datetime2, SYSDATETIMEOFFSET() AT TIME ZONE 'Central Standard Time'),
        CONSTRAINT uq_content_source UNIQUE (source_type, source_ref)
    );

    CREATE INDEX ix_content_received   ON ContentRecords(received_at DESC);
    CREATE INDEX ix_content_enrichment ON ContentRecords(enrichment_status)
        WHERE enrichment_status IN ('pending', 'failed');
END;

-- RecordTerms: generic queryable attributes on a record.
-- kind='label' terms are also pushed to the Gmail thread for inbox organization.
-- kind='tag' terms are LLM-derived topics. Future: 'ticker', 'entity', etc.
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'RecordTerms')
BEGIN
    CREATE TABLE RecordTerms (
        record_id   INT          NOT NULL,
        kind        VARCHAR(20)  NOT NULL,
        value       VARCHAR(100) NOT NULL,
        CONSTRAINT pk_record_terms PRIMARY KEY (record_id, kind, value),
        CONSTRAINT fk_record_terms_record FOREIGN KEY (record_id)
            REFERENCES ContentRecords(id) ON DELETE CASCADE
    );

    CREATE INDEX ix_record_terms_kind_value ON RecordTerms(kind, value);
END;

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'DigestRuns')
BEGIN
    CREATE TABLE DigestRuns (
        id               INT IDENTITY    NOT NULL PRIMARY KEY,
        profile_name     VARCHAR(100)    NOT NULL,
        period_start     DATETIME2       NOT NULL,
        period_end       DATETIME2       NOT NULL,
        summary_text     NVARCHAR(MAX)   NOT NULL,
        content_ids      NVARCHAR(MAX)   NULL,            -- JSON array of ContentRecords.id
        prior_digest_ids NVARCHAR(MAX)   NULL,            -- JSON array of DigestRuns.id used as context
        record_count     INT             NOT NULL DEFAULT 0,
        created_at       DATETIME2       NOT NULL DEFAULT CONVERT(datetime2, SYSDATETIMEOFFSET() AT TIME ZONE 'Central Standard Time')
    );

    CREATE INDEX ix_digest_profile_end ON DigestRuns(profile_name, period_end DESC);
END;

-- classification_confidence: rename legacy 'confidence' if present, else add fresh.
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('ContentRecords') AND name = 'confidence')
   AND NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('ContentRecords') AND name = 'classification_confidence')
BEGIN
    EXEC sp_rename 'ContentRecords.confidence', 'classification_confidence', 'COLUMN';
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('ContentRecords') AND name = 'classification_confidence')
BEGIN
    ALTER TABLE ContentRecords ADD classification_confidence TINYINT NULL;
END;

-- classification_rationale: rename legacy 'rationale' if present, else add fresh.
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('ContentRecords') AND name = 'rationale')
   AND NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('ContentRecords') AND name = 'classification_rationale')
BEGIN
    EXEC sp_rename 'ContentRecords.rationale', 'classification_rationale', 'COLUMN';
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('ContentRecords') AND name = 'classification_rationale')
BEGIN
    ALTER TABLE ContentRecords ADD classification_rationale NVARCHAR(1000) NULL;
END;

-- Widen classification_rationale to 1000 chars (was 500 in earlier schema versions).
ALTER TABLE ContentRecords ALTER COLUMN classification_rationale NVARCHAR(1000) NULL;

-- executive_summary + summary_confidence + summary_rationale: produced by the summarize step (Haiku).
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('ContentRecords') AND name = 'executive_summary')
BEGIN
    ALTER TABLE ContentRecords ADD executive_summary NVARCHAR(MAX) NULL;
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('ContentRecords') AND name = 'summary_confidence')
BEGIN
    ALTER TABLE ContentRecords ADD summary_confidence TINYINT NULL;
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('ContentRecords') AND name = 'summary_rationale')
BEGIN
    ALTER TABLE ContentRecords ADD summary_rationale NVARCHAR(1000) NULL;
END;

-- Widen summary_rationale to 1000 chars (was 250 in earlier schema versions).
ALTER TABLE ContentRecords ALTER COLUMN summary_rationale NVARCHAR(1000) NULL;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('ContentRecords') AND name = 'classify_model')
BEGIN
    ALTER TABLE ContentRecords ADD classify_model VARCHAR(50) NULL;
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('ContentRecords') AND name = 'classify_tokens')
BEGIN
    ALTER TABLE ContentRecords ADD classify_tokens INT NULL;
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('ContentRecords') AND name = 'escalated')
BEGIN
    ALTER TABLE ContentRecords ADD escalated BIT NULL;
END;

-- Rename RecordTerms.record_id to id for consistency with ContentRecords.id and DigestRuns.id
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('RecordTerms') AND name = 'record_id')
BEGIN
    ALTER TABLE RecordTerms DROP CONSTRAINT fk_record_terms_record;
    ALTER TABLE RecordTerms DROP CONSTRAINT pk_record_terms;
    EXEC sp_rename 'RecordTerms.record_id', 'id', 'COLUMN';
    ALTER TABLE RecordTerms ADD CONSTRAINT pk_record_terms PRIMARY KEY (id, kind, value);
    ALTER TABLE RecordTerms ADD CONSTRAINT fk_record_terms_record
        FOREIGN KEY (id) REFERENCES ContentRecords(id) ON DELETE CASCADE;
END;

-- ActionRuns: deduplication log for action-dispatch (Todoist, etc.).
-- One row per (profile_name, record_id) — the UNIQUE constraint prevents
-- re-creating a task for the same record under the same profile on a re-run.
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'ActionRuns')
BEGIN
    CREATE TABLE ActionRuns (
        id            INT IDENTITY        NOT NULL PRIMARY KEY,
        profile_name  VARCHAR(100)        NOT NULL,
        record_id     INT                 NOT NULL,
        target        VARCHAR(40)         NOT NULL,   -- 'todoist'
        external_id   VARCHAR(100)        NULL,       -- provider task id
        status        VARCHAR(20)         NOT NULL,   -- created | failed
        error         NVARCHAR(1000)      NULL,
        created_at    DATETIME2           NOT NULL DEFAULT CONVERT(datetime2, SYSDATETIMEOFFSET() AT TIME ZONE 'Central Standard Time'),
        CONSTRAINT uq_action_run UNIQUE (profile_name, record_id),
        CONSTRAINT fk_action_run_record FOREIGN KEY (record_id)
            REFERENCES ContentRecords(id) ON DELETE CASCADE
    );

    CREATE INDEX ix_action_run_profile ON ActionRuns(profile_name, created_at DESC);
END;

-- ActionRuns.action_content: stores the LLM-generated action JSON for aggregate-mode runs.
-- Provides prior-action context so subsequent runs can suppress redundant tasks.
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('ActionRuns') AND name = 'action_content')
BEGIN
    ALTER TABLE ActionRuns ADD action_content NVARCHAR(MAX) NULL;
END;

-- ContentRecords.source_type: widened from VARCHAR(20) to VARCHAR(50) to accommodate
-- extensible source types beyond the original short built-in names (gmail, file, manual).
IF EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'ContentRecords'
      AND COLUMN_NAME = 'source_type'
      AND CHARACTER_MAXIMUM_LENGTH < 50
)
BEGIN
    ALTER TABLE ContentRecords ALTER COLUMN source_type VARCHAR(50) NOT NULL;
END;
