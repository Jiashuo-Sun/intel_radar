"""
database.py — SQLite 本地存储层（乐高积木：持久化层）

═══════════════════════════════════════════════════════════════════════
功能作用
═══════════════════════════════════════════════════════════════════════
封装所有对本地 SQLite 数据库的读写操作，是全项目唯一直接执行 SQL 的
模块。上层（processor.py / run.py）只通过本类提供的方法名操作数据，
不拼接 SQL 语句，也不感知表结构细节——表结构变化只需改这一个文件。

承担两个职责，靠两张表分开：
  - items 表：已采集且判定为相关的条目，用于 URL 精确去重 + SimHash
              近似去重的历史依据
  - runs  表：每次运行的执行记录（成功/失败均记录），用于运维排查

═══════════════════════════════════════════════════════════════════════
输入 / 输出
═══════════════════════════════════════════════════════════════════════
Database(path) —— 构造函数，path 为数据库文件路径（相对/绝对均可，
                   内部会转换为绝对路径），不存在会自动创建父目录和表结构

is_url_duplicate(url) -> bool           —— URL 精确去重判断
is_simhash_duplicate(title, days, thr) -> bool —— 近似去重判断（依赖 simhash.py）
save_item(...) -> int                   —— 保存条目，返回自增 ID（重复 URL 时返回 0）
update_ai(url, summary_ai, impact)      —— 回写 AI 摘要结果
mark_reported(url, report_date)         —— 标记条目所属的日报日期
log_run(fetched, new, deduped, report_path, error) —— 记录一次运行结果
clear()                                 —— 清空所有表（仅测试/维护用）

═══════════════════════════════════════════════════════════════════════
限制与边界
═══════════════════════════════════════════════════════════════════════
- 每次方法调用都新开一个 sqlite3 连接（self._conn()），不维护长连接
  池。对于"每天跑一次、几千条数据"的使用规模，这个开销可以忽略；但
  如果未来改造成高频调用场景（如 Web API 后端），应改为连接池或
  单例长连接 + 显式事务管理。
- save_item 使用 INSERT OR IGNORE，依赖 url_hash 列的 UNIQUE 约束
  实现幂等写入；重复 URL 静默忽略（返回 0），不报错、不覆盖已有记录
  （包括已有的 AI 摘要字段也不会被清空）。
- is_simhash_duplicate 的查询范围是"fetched_at 在最近 window_days 天
  内的所有条目"，条目数量随时间窗口和抓取量增长，目前没有对
  title_hash 建索引做区间查询优化——SQLite 只能全表扫描窗口内的行做
  逐条汉明距离比较。数据量达到数万级别时需要考虑优化（如增加
  title_hash 的 B-Tree 索引配合分桶查询，或迁移到向量数据库，参见
  knowledge/todo.md P3 项）。
- clear() 是破坏性操作（DROP TABLE），仅供 `--reset-db` 命令行参数
  和测试代码调用，不应在生产 pipeline 中默认触发。
- 路径处理使用 os.path.abspath()，保证即使传入不含目录分隔符的裸
  文件名（如 "radar.db"）也不会导致 os.makedirs("") 报错。
"""
import sqlite3
import hashlib
import os
from datetime import datetime
from typing import Optional

from .simhash import simhash, hamming_distance


class Database:
    def __init__(self, path: str):
        path = os.path.abspath(path)
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
        """判断 URL 是否已存在于 items 表（即历史上已被判定为相关并保存过）。"""
        url_hash = hashlib.md5(url.encode()).hexdigest()
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM items WHERE url_hash=?", (url_hash,)
            ).fetchone() is not None

    def is_simhash_duplicate(self, title: str, window_days: int = 7, threshold: int = 3) -> bool:
        """
        判断标题是否与最近 window_days 天内已保存的某条目"近似重复"
        （SimHash 汉明距离 <= threshold）。用于捕获同一报道被不同 URL
        转载的情况（URL 精确去重无法识别这种重复）。
        """
        title_hash = simhash(title)
        with self._conn() as c:
            rows = c.execute(
                "SELECT title_hash FROM items WHERE fetched_at >= date('now', ?)",
                (f"-{window_days} days",),
            ).fetchall()
            for (th,) in rows:
                if th and hamming_distance(title_hash, th) <= threshold:
                    return True
        return False

    # 保留旧方法名以兼容外部调用方（若有）。
    def is_duplicate(self, url: str, title: str, window_days: int = 7, threshold: int = 3) -> bool:
        return self.is_url_duplicate(url) or self.is_simhash_duplicate(title, window_days, threshold)

    def save_item(self, url: str, title: str, source_name: str, topic_group: str,
                  score: int, published_at: Optional[str], summary_ai: str = "",
                  impact: str = "") -> int:
        """
        保存一条相关条目。使用 INSERT OR IGNORE，URL 已存在时静默跳过
        （不覆盖、不报错），返回值为新插入行的自增 ID；若因重复被忽略
        则返回 0。
        """
        url_hash = hashlib.md5(url.encode()).hexdigest()
        title_hash = simhash(title)
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
        """按 URL 回写 AI 摘要结果（processor 完成 AI 调用后调用）。"""
        url_hash = hashlib.md5(url.encode()).hexdigest()
        with self._conn() as c:
            c.execute("UPDATE items SET summary_ai=?, impact=? WHERE url_hash=?",
                      (summary_ai, impact, url_hash))

    def mark_reported(self, url: str, report_date: str):
        """标记该条目已被写入哪一天的日报（report_date 为 YYYY-MM-DD 字符串）。"""
        url_hash = hashlib.md5(url.encode()).hexdigest()
        with self._conn() as c:
            c.execute("UPDATE items SET included_in_report=? WHERE url_hash=?",
                      (report_date, url_hash))

    def log_run(self, fetched: int, new: int, deduped: int, report_path: str, error: str = ""):
        """
        记录一次运行的执行结果。error 非空表示本次运行失败/异常中断；
        调用方（run.py）应在 try/finally 中无条件调用本方法，确保
        失败的运行也留下记录，便于事后排查趋势（见 knowledge/lessons-learned.md）。
        """
        with self._conn() as c:
            c.execute("""
                INSERT INTO runs (run_at, items_fetched, items_new, items_deduped, report_path, error)
                VALUES (?,?,?,?,?,?)
            """, (datetime.now().isoformat(), fetched, new, deduped, report_path, error))

    def clear(self):
        """清空并重建所有表结构。仅用于 --reset-db 命令行参数或测试，不可在生产流程默认调用。"""
        with self._conn() as c:
            c.executescript("DROP TABLE IF EXISTS items; DROP TABLE IF EXISTS runs;")
        self._init()
