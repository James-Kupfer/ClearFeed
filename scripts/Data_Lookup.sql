-- Ad-hoc record dump (PostgreSQL). Run against the clearfeed database:
--     psql -U clearfeed -d clearfeed -f scripts/Data_Lookup.sql
SELECT
    cr.id,
    REPLACE(REPLACE(cr.subject,   CHR(13), ' '), CHR(10), ' ') AS subject,
    cr.sender,
    cr.processed_at,
    rt.labels,
    rt.tags,
    cr.executive_summary,
    cr.classification_confidence,
    cr.classification_rationale,
    cr.escalated,
    cr.classify_model,
    cr.classify_tokens,
    cr.summary_confidence,
    cr.summary_rationale,
    REPLACE(REPLACE(cr.summary,   CHR(13), ' '), CHR(10), ' ') AS summary,
    REPLACE(REPLACE(cr.body_text, CHR(13), ' '), CHR(10), ' ') AS body_text
FROM ContentRecords cr
LEFT JOIN (
    SELECT
        id,
        STRING_AGG(CASE WHEN kind = 'label' THEN value END, ', ') AS labels,
        STRING_AGG(CASE WHEN kind = 'tag'   THEN value END, ', ') AS tags
    FROM RecordTerms
    GROUP BY id
) rt ON rt.id = cr.id
ORDER BY cr.processed_at DESC
LIMIT 500;
