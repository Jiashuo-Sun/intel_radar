#!/usr/bin/env python3
"""
run.py — Intel Radar 主入口

用法：
  python run.py                     # 立即运行，生成今日日报
  python run.py --date 2025-01-14   # 补跑指定日期（不影响调度）
  python run.py --dry-run           # 只采集打印，不写DB/文件
  python run.py --no-ai             # 跳过 AI 摘要
  python run.py --daemon            # 后台常驻，按 settings.yaml schedule 定时跑
"""
import argparse
import logging
import os
import sys
import time
from datetime import date, datetime

import yaml

# 确保 src 包可导入
sys.path.insert(0, os.path.dirname(__file__))

# 确保 logs 目录存在（在 logging 初始化之前）
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
             dry_run: bool = False, use_ai: bool = True):
    log.info(f"=== Intel Radar run start — {target_date} ===")

    db_path = settings.get("database", {}).get("path", "./data/radar.db")
    db = Database(db_path)

    # 采集
    orchestrator = FetcherOrchestrator(watch_cfg, settings)
    raw_items = orchestrator.fetch_all()
    log.info(f"Fetched total: {len(raw_items)} raw items")

    if dry_run:
        log.info("[DRY RUN] Items fetched (not saved):")
        for item in raw_items[:20]:
            print(f"  [{item.topic_group}] {item.title[:80]}")
        return

    # 处理
    processor = Processor(db, settings)
    processed = processor.process(raw_items, use_ai=use_ai)
    log.info(f"Processed: {len(processed)} items to report")

    # 生成日报
    reporter = Reporter(settings)
    stats = {
        "new": len(processed),
        "deduped": len(raw_items) - len(processed),
    }
    report_path = reporter.generate(processed, target_date, stats)

    # 标记已报告
    for item in processed:
        db.mark_reported(item.url, target_date.isoformat())

    db.log_run(
        fetched=len(raw_items),
        new=len(processed),
        deduped=stats["deduped"],
        report_path=report_path,
    )

    log.info(f"=== Done. Report: {report_path} ===")
    print(f"\n✅  日报已生成：{report_path}\n")
    return report_path


def daemon_loop(watch_cfg: dict, settings: dict, use_ai: bool):
    run_at = settings.get("schedule", {}).get("run_at", "06:30")
    log.info(f"Daemon mode — will run daily at {run_at}")
    while True:
        now = datetime.now().strftime("%H:%M")
        if now == run_at:
            run_once(watch_cfg, settings, date.today(), use_ai=use_ai)
            time.sleep(61)   # 防止同分钟重复触发
        time.sleep(30)


def main():
    parser = argparse.ArgumentParser(description="Intel Radar — 行业情报雷达")
    parser.add_argument("--watch",    default="config/watch.yaml")
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--date",     default=None, help="指定日期 YYYY-MM-DD")
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--no-ai",    action="store_true")
    parser.add_argument("--daemon",   action="store_true")
    args = parser.parse_args()

    if not os.path.exists(args.watch):
        sys.exit(f"❌  配置文件不存在：{args.watch}\n请先复制并编辑 config/watch.yaml")
    if not os.path.exists(args.settings):
        sys.exit(f"❌  配置文件不存在：{args.settings}\n请先复制并编辑 config/settings.yaml")

    watch_cfg = load_yaml(args.watch)
    settings  = load_yaml(args.settings)

    target_date = date.fromisoformat(args.date) if args.date else date.today()
    use_ai = not args.no_ai

    if args.daemon:
        daemon_loop(watch_cfg, settings, use_ai)
    else:
        run_once(watch_cfg, settings, target_date,
                 dry_run=args.dry_run, use_ai=use_ai)


if __name__ == "__main__":
    main()
