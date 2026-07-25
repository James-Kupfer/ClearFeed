SELECT
    cr.id,
    cr.source_type,
    cr.source_ref,
    cr.sender,
    cr.subject,
    cr.received_at,
    cr.ingested_at,
    cr.enrichment_status,
    cr.summary,
    cr.executive_summary,
    COALESCE(sd.raw_text, sd.raw_html) AS original_content
FROM ContentRecords cr
LEFT JOIN SourceDocuments sd ON sd.id = cr.id
WHERE EXISTS (
    SELECT 1 FROM RecordTerms rt
    WHERE rt.id = cr.id AND rt.kind = 'tag' AND rt.value = 'actionable'
)
ORDER BY cr.ingested_at DESC
LIMIT 10;