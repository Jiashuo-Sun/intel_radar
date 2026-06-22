"""
database.py — SQLite 本地存储
"""
import sqlite3
import hashlib
import os
from datetime import datetime
from typing import Optional


def _simhash(text: str) -> int:
    """Minimal SimHash — returns a signed 64-bit integer compatible with SQLite INTEGER."""
    v = [0] * 64
    words = text.lower().split()
    for w in words:
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        for i in range(64):
            v[i] += 1 if (h >> i) & 1 else -1
    result = 0
    for i in range(64):
        if v[i] > 0:
            result |= (1 << i)
    if result >= (1 << 63):
        result -= (1 << 64)
    return result


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count('1')


class Database:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self._init()

    def _conn(self):
        return sqlite3.connect(self.path)

    def _init(self):
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS items (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                url_hash        TEXT UNIQUE,
                title_hash      INTEGER,
                title           TEXT,
                url             TEXT,
                source_name     TEXT,
                topic_group     TEXT,
                score           INTEGER DEFAULT 0,
                summary_ai      TEXT,
                impact          TEXT,
                published_at    TEXT,
                fetched_at      TEXT,
                included_in_report TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_fetched ON items(fetched_at);
            CREATE INDEX IF NOT EXISTS idx_score   ON items(score);

            CREATE TABLE IF NOT EXISTS runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at          TEXT,
                items_fetched   INTEGER DEFAULT 0,
                items_new       INTEGER DEFAULT 0,
                items_deduped   INTEGER DEFAULT 0,
                report_path     TEXT,
                error           TEXT
            );
            """)

    def is_url_duplicate(self, url: str) -> bool:
        """Return True if this URL was already saved to DB (seen and scored relevant before)."""
        url_hash = hashlib.md5(url.encode()).hexdigest()
        with self._conn() as c:
            return c.execute("SELECT 1 FROM items WHERE url_hash=?", (url_hash,)).fetchone() is not None

    def is_simhash_duplicate(self, title: str, window_days: int = 7, threshold: int = 3) -> bool:
        """Return True if a near-identical title exists within the dedup window."""
        title_hash = _simhash(title)
        with self._conn() as c:
            rows = c.execute(
                "SELECT title_hash FROM items WHERE fetched_at >= date('now', ?)",
                (f"-{window_days} days",)
            ).fetchall()
            for (th,) in rows:
                if th and _hamming(title_hash, th) <= threshold:
                    return True
        return False

    # Keep legacy method for any external callers
    def is_duplicate(self, url: str, title: str, window_days: int = 7, threshold: int = 3) -> bool:
        return self.is_url_duplicate(url) or self.is_simhash_duplicate(title, window_days, threshold)

    def save_item(self, url: str, title: str, source_name: str, topic_group: str,
                  score: int, published_at: Optional[str], summary_ai: str = "",
                  impact: str = "") -> int:
        url_hash = hashlib.md5(url.encode()).hexdigest()
        title_hash = _simhash(title)
        now = datetime.now().isoformat()
        with self._conn() as c:
            cur = c.execute("""
                INSERT OR IGNORE INTO items
                (url_hash, title_hash, title, url, source_name, topic_group,
                 score, summary_ai, impact, published_at, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (url_hash, title_hash, title, url, source_name, topic_group,
                  score, summary_ai, impact, published_at, now))
            return cur.lastrowid or 0

    def update_ai(self, url: str, summary_ai: str, impact: str):
        url_hash = hashlib.md5(url.encode()).hexdigest()
        with self._conn() as c:
            c.execute("UPDATE items SET summary_ai=?, impact=? WHERE url_hash=?",
                      (summary_ai, impact, url_hash))

    def mark_reported(self, url: str, report_date: str):
        url_hash = hashlib.md5(url.encode()).hexdigest()
        with self._conn() as c:
            c.execute("UPDATE items SET included_in_report=? WHERE url_hash=?",
                      (report_date, url_hash))

    def log_run(self, fetched: int, new: int, deduped: int, report_path: str, error: str = ""):
        with self._conn() as c:
            c.execute("""
                INSERT INTO runs (run_at, items_fetched, items_new, items_deduped, report_path, error)
                VALUES (?,?,?,?,?,?)
            """, (datetime.now().isoformat(), fetched, new, deduped, report_path, error))

    def clear(self):
        """Drop and recreate all tables — use only for maintenance/testing."""
        with self._conn() as c:
            c.executescript("DROP TABLE IF EXISTS items; DROP TABLE IF EXISTS runs;")
        self._init()
