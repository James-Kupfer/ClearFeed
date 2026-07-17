-- Ad-hoc cleanup (PostgreSQL). Run against the clearfeed database:
--     psql -U clearfeed -d clearfeed -f "scripts/Delete processing data.sql"
DELETE FROM RecordTerms
WHERE id IN (
    SELECT id FROM RecordTerms WHERE kind = 'label' AND value IN ('Personal', 'Professional')
);


SELECT id, record_id, external_id, status, created_at
FROM ActionRuns
WHERE profile_name = 'task_connection'
ORDER BY created_at DESC;

DELETE FROM ActionRuns
WHERE profile_name = 'task_connection';
