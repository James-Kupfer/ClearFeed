"""Quick PostgreSQL health check: server version, tables, and top RecordTerms."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # security_config
import db  # noqa: E402

with db.get_connection() as c:
    cur = c.cursor()
    cur.execute("SELECT version()")
    print(cur.fetchone()[0].splitlines()[0])

    cur.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    print("tables:", ", ".join(r[0] for r in cur.fetchall()))

    cur.execute(
        "SELECT kind, value, COUNT(*) AS n FROM RecordTerms "
        "GROUP BY kind, value ORDER BY n DESC LIMIT 25"
    )
    for kind, value, n in cur.fetchall():
        print(kind, repr(value), n)

    cur.execute("SELECT COUNT(*) FROM SourceDocuments")
    print("SourceDocuments rows:", cur.fetchone()[0])
