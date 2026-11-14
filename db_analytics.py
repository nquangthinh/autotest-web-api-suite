"""SQLite storage with short-lived connections and parameterized statements."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


class BillStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                merchant_name TEXT NOT NULL,
                grand_total INTEGER NOT NULL CHECK (grand_total >= 0),
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")

    def save(self, merchant_name, split):
        created_at = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO bills(merchant_name, grand_total, payload, created_at) VALUES (?, ?, ?, ?)",
                (merchant_name, split["grand_total"], json.dumps(split, ensure_ascii=False), created_at))
            bill_id = cursor.lastrowid
        return {"id": bill_id, "merchant_name": merchant_name, "created_at": created_at, "split": split}

    def get(self, bill_id):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "merchant_name": row["merchant_name"],
                "created_at": row["created_at"], "split": json.loads(row["payload"])}

    def summary(self):
        with self.connect() as connection:
            # Python integers also keep aggregated totals exact beyond SQLite int64.
            bill_count = total_amount = 0
            for row in connection.execute("SELECT grand_total FROM bills"):
                bill_count += 1
                total_amount += row["grand_total"]
        return {"bill_count": bill_count, "total_amount": total_amount}
