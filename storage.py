"""SQLite storage: accounts + activity log."""
import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    wpn_access_token TEXT,
    wpn_refresh_token TEXT,
    wpn_session_id TEXT,
    subscription_id TEXT,
    subscription_days_left INTEGER,
    subscription_valid_until TEXT,
    subscription_url TEXT,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    level TEXT DEFAULT 'info',
    message TEXT NOT NULL,
    account_email TEXT
);
"""


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.on_log = None  # optional callback(level, message, email) for live push
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # migrations: add columns if upgrading from an older schema
            cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
            for col in ("subscription_id", "subscription_url"):
                if col not in cols:
                    conn.execute(f"ALTER TABLE accounts ADD COLUMN {col} TEXT")

    # ── Accounts ──────────────────────────────────────────────

    def save_account(self, account: dict) -> int:
        """Insert or update account by email. Returns row id."""
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO accounts (
                    email, wpn_access_token, wpn_refresh_token,
                    wpn_session_id, subscription_id, subscription_days_left,
                    subscription_valid_until, subscription_url, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    wpn_access_token = excluded.wpn_access_token,
                    wpn_refresh_token = excluded.wpn_refresh_token,
                    wpn_session_id = excluded.wpn_session_id,
                    subscription_id = excluded.subscription_id,
                    subscription_days_left = excluded.subscription_days_left,
                    subscription_valid_until = excluded.subscription_valid_until,
                    subscription_url = excluded.subscription_url,
                    status = excluded.status,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    account["email"],
                    account.get("wpn_access_token"),
                    account.get("wpn_refresh_token"),
                    account.get("wpn_session_id"),
                    account.get("subscription_id"),
                    account.get("subscription_days_left"),
                    account.get("subscription_valid_until"),
                    account.get("subscription_url"),
                    account.get("status", "active"),
                ),
            )
            conn.commit()
            return cur.lastrowid

    def get_active_account(self) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE status = 'active' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def get_account_by_email(self, email: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE email = ?", (email,)
            ).fetchone()
            return dict(row) if row else None

    def get_all_accounts(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM accounts ORDER BY id DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_used_emails(self) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT email FROM accounts").fetchall()
            return {r["email"] for r in rows}

    def update_account_status(self, email: str, status: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE accounts SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE email = ?",
                (status, email),
            )
            conn.commit()

    def update_account_subscription(
        self, email: str, days_left: int, valid_until: str,
        subscription_id: str = None, subscription_url: str = None,
    ):
        with self._conn() as conn:
            conn.execute(
                """UPDATE accounts SET
                   subscription_days_left = ?,
                   subscription_valid_until = ?,
                   subscription_id = COALESCE(?, subscription_id),
                   subscription_url = COALESCE(?, subscription_url),
                   updated_at = CURRENT_TIMESTAMP
                   WHERE email = ?""",
                (days_left, valid_until, subscription_id, subscription_url, email),
            )
            conn.commit()

    def update_account_tokens(
        self, email: str, access_token: str, refresh_token: str = None
    ):
        with self._conn() as conn:
            if refresh_token:
                conn.execute(
                    """UPDATE accounts SET
                       wpn_access_token = ?,
                       wpn_refresh_token = ?,
                       updated_at = CURRENT_TIMESTAMP
                       WHERE email = ?""",
                    (access_token, refresh_token, email),
                )
            else:
                conn.execute(
                    """UPDATE accounts SET
                       wpn_access_token = ?,
                       updated_at = CURRENT_TIMESTAMP
                       WHERE email = ?""",
                    (access_token, email),
                )
            conn.commit()

    # ── Activity log ───────────────────────────────────────────

    def log(self, message: str, level: str = "info", account_email: str = None):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO activity_log (level, message, account_email) VALUES (?, ?, ?)",
                (level, message, account_email),
            )
            conn.commit()
        # notify live listeners (WebSocket)
        if self.on_log:
            try:
                self.on_log(level, message, account_email)
            except Exception:
                pass

    def get_recent_logs(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
