import sys
sys.path.insert(0, "src")
import db

with db.get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute("DELETE FROM ActionRuns WHERE profile_name = 'task_investment'")
    conn.commit()
    print("Deleted", cursor.rowcount, "rows")
