"""
database.py — SQLite 本地存储
"""
import sqlite3
import hashlib
import os
from datetime import datetime, date
from dataclasses import dataclass
from typing import Optional, List


def _simhash(text: str) -> int:
    """极简 SimHash，返回 64 位有符号整数（SQLite INTEGER 兼容）"""
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
    # 转为有符号 64 位整数
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

    def is_duplicate(self, url: str, title: str, window_days: int = 7, threshold: int = 3) -> bool:
        url_hash = hashlib.md5(url.encode()).hexdigest()
        with self._conn() as c:
            # 精确URL匹配
            row = c.execute("SELECT 1 FROM items WHERE url_hash=?", (url_hash,)).fetchone()
            if row:
                return True
            # SimHash相似度（只查最近window_days天）
            title_hash = _simhash(title)
            rows = c.execute(
                "SELECT title_hash FROM items WHERE fetched_at >= date('now', ?)",
                (f"-{window_days} days",)
            ).fetchall()
            for (th,) in rows:
                if th and _hamming(title_hash, th) <= threshold:
                    return True
        return False

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
