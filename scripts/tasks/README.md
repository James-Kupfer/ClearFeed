# scripts/tasks/

Disaster-recovery exports of the live Windows Scheduled Tasks under `\ClearFeed\`
(`schtasks /query /tn "\ClearFeed\<name>" /xml`). These are **not** the source
of truth — the registered tasks in Task Scheduler are. If a task is edited live,
re-export it here; don't assume an XML in this folder still matches what's
actually registered, and don't delete a file here as "orphaned" without first
checking `Get-ScheduledTask -TaskPath "\ClearFeed\"` for a live task of that name.
