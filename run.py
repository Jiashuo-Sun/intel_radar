#!/usr/bin/env python3
"""
run.py — Intel Radar 主入口

用法：
  python run.py                     # 立即运行，生成今日日报
  python run.py --date 2025-01-14   # 补跑指定日期（不影响调度）
  python run.py --dry-run           # 只采集打印，不写DB/文件
  python run.py --no-ai             # 跳过 AI 摘要
  python run.py --daemon            # 后台常驻，按 settings.yaml schedule 定时跑
  python run.py --reset-db          # 清空数据库后运行（仅用于测试/维护）
"""
import argparse
import logging
import os
import sys
import time
from datetime import date, datetime

import yaml

sys.path.insert(0, os.path.dirname(__file__))

os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)

from src.database import Database
from src.fetcher import FetcherOrchestrator
from src.processor import Processor
from src.reporter import Reporter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/radar.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("radar")


def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def run_once(watch_cfg: dict, settings: dict, target_date: date,
             dry_run: bool = False, use_ai: bool = True, reset_db: bool = False):
    log.info(f"=== Intel Radar run start — {target_date} ===")

    db_path = settings.get("database", {}).get("path", "./data/radar.db")
    db = Database(db_path)

    if reset_db:
        log.warning(f"--reset-db: clearing database {db_path}")
        db.clear()

    # 采集
    orchestrator = FetcherOrchestrator(watch_cfg, settings)
    raw_items = orchestrator.fetch_all()
    log.info(f"Fetched total: {len(raw_items)} raw items")

    if dry_run:
        log.info("[DRY RUN] Top 20 items (not saved):")
        for item in raw_items[:20]:
            print(f"  [{item.topic_group}] {item.title[:80]}")
        return

    report_path = ""
    error_msg = ""
    processed = []
    proc_stats = {"url_deduped": 0, "score_filtered": 0, "simhash_deduped": 0}

    try:
        # 处理
        processor = Processor(db, settings)
        processed, proc_stats = processor.process(raw_items, use_ai=use_ai)
        log.info(f"Items for report: {len(processed)}")

        # 生成日报
        reporter = Reporter(settings)
        stats = {
            "new": len(processed),
            "url_deduped": proc_stats["url_deduped"],
            "score_filtered": proc_stats["score_filtered"],
            "simhash_deduped": proc_stats["simhash_deduped"],
            "deduped": proc_stats["url_deduped"] + proc_stats["simhash_deduped"],
        }
        report_path = reporter.generate(processed, target_date, stats)

        # 标记已报告
        for item in processed:
            db.mark_reported(item.url, target_date.isoformat())

    except Exception as e:
        error_msg = str(e)
        log.exception(f"Pipeline error: {e}")
        raise
    finally:
        db.log_run(
            fetched=len(raw_items),
            new=len(processed),
            deduped=proc_stats["url_deduped"] + proc_stats["simhash_deduped"],
            report_path=report_path,
            error=error_msg,
        )

    log.info(f"=== Done. Report: {report_path} ===")
    print(f"\n日报已生成：{report_path}\n")
    return report_path


def daemon_loop(watch_cfg: dict, settings: dict, use_ai: bool):
    run_at = settings.get("schedule", {}).get("run_at", "06:30")
    log.info(f"Daemon mode — will run daily at {run_at}")
    while True:
        now = datetime.now().strftime("%H:%M")
        if now == run_at:
            run_once(watch_cfg, settings, date.today(), use_ai=use_ai)
            time.sleep(61)
        time.sleep(30)


def main():
    parser = argparse.ArgumentParser(description="Intel Radar — 行业情报雷达")
    parser.add_argument("--watch",    default="config/watch.yaml")
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--date",     default=None, help="指定日期 YYYY-MM-DD")
    parser.add_argument("--dry-run",  action="store_true", help="只采集不写DB，用于测试")
    parser.add_argument("--no-ai",    action="store_true", help="跳过 AI 摘要")
    parser.add_argument("--daemon",   action="store_true", help="后台常驻定时运行")
    parser.add_argument("--reset-db", action="store_true",
                        help="清空数据库后运行（测试/维护用，会丢失历史去重记录）")
    args = parser.parse_args()

    if not os.path.exists(args.watch):
        sys.exit(f"配置文件不存在：{args.watch}")
    if not os.path.exists(args.settings):
        sys.exit(f"配置文件不存在：{args.settings}")

    watch_cfg = load_yaml(args.watch)
    settings  = load_yaml(args.settings)

    target_date = date.fromisoformat(args.date) if args.date else date.today()
    use_ai = not args.no_ai

    if args.daemon:
        daemon_loop(watch_cfg, settings, use_ai)
    else:
        run_once(watch_cfg, settings, target_date,
                 dry_run=args.dry_run, use_ai=use_ai,
                 reset_db=getattr(args, "reset_db", False))


if __name__ == "__main__":
    main()
