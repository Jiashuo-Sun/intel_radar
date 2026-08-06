#!/usr/bin/env python3
"""
run.py — Intel Radar 主入口（乐高积木：pipeline 装配层）

═══════════════════════════════════════════════════════════════════════
功能作用
═══════════════════════════════════════════════════════════════════════
把各个独立乐高模块按固定顺序串联成一条完整 pipeline：

    配置加载 → Database → FetcherOrchestrator → Processor → Reporter → 日志落库

本文件不实现任何业务逻辑（不解析 RSS、不打分、不调 AI、不写
Markdown），只负责"装配"——创建各模块实例、把上一步的输出传给下一步
的输入、处理命令行参数、统一异常处理。这是整个项目里唯一知道"完整
流程长什么样"的地方；单个模块永远不需要知道它在流程中的上下游是谁。

用法：
  python run.py                     # 立即运行，生成今日日报
  python run.py --date 2025-01-14   # 补跑指定日期（不影响调度）
  python run.py --dry-run           # 只采集打印，不写DB/文件
  python run.py --no-ai             # 跳过 AI 摘要
  python run.py --daemon            # 后台常驻，按 settings.yaml schedule 定时跑
  python run.py --reset-db          # 清空数据库后运行（仅用于测试/维护）

═══════════════════════════════════════════════════════════════════════
限制与边界
═══════════════════════════════════════════════════════════════════════
- run_once() 用 try/except/finally 包裹处理阶段，确保无论成功还是
  异常中断，db.log_run() 都会被调用一次，在 runs 表中留下记录（成功
  记录 error=""，失败记录 error=异常信息）。这样运维排查时可以直接
  查 runs 表判断"哪天跑失败了、为什么"，不需要翻日志文件。
- --dry-run 模式在采集完成后直接 return，不经过 Processor/Reporter/
  db.log_run()，即"只探测数据源是否可用"，不产生任何持久化副作用。
- daemon 模式的调度用简单的字符串比较当前时间（HH:MM）+ 30 秒轮询，
  在分钟边界附近可能有极小概率错过触发窗口。生产环境更推荐用系统级
  crontab（见用法说明），daemon 模式仅作为无 cron 环境下的备选方案。
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
    """加载并解析一个 YAML 配置文件，空文件返回空字典而非 None。"""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def run_once(watch_cfg: dict, settings: dict, target_date: date,
             dry_run: bool = False, use_ai: bool = True, reset_db: bool = False):
    """
    执行一次完整 pipeline：采集 → 处理 → 生成日报 → 记录运行结果。

    参数：
      watch_cfg  —— watch.yaml 解析结果（监控对象配置）
      settings   —— settings.yaml 解析结果（运行参数配置）
      target_date —— 日报归属日期（用于补跑历史日期场景）
      dry_run    —— True 时只采集打印前 20 条，不写 DB/文件，直接返回
      use_ai     —— False 时跳过 AI 摘要阶段（对应 --no-ai）
      reset_db   —— True 时先清空数据库再运行（对应 --reset-db）

    返回：生成的日报文件路径；dry_run 模式下返回 None。
    异常：处理阶段（Processor/Reporter）抛出的异常会在 finally 中先
          记录到 runs 表，再重新向上抛出，不吞异常。
    """
    log.info(f"=== Intel Radar run start — {target_date} ===")

    db_path = settings.get("database", {}).get("path", "./data/radar.db")
    db = Database(db_path)

    if reset_db:
        log.warning(f"--reset-db: clearing database {db_path}")
        db.clear()

    # ── 采集阶段 ──────────────────────────────────────────────
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
        # ── 处理阶段：去重 + 打分 + AI 摘要 ──────────────────
        processor = Processor(db, settings)
        processed, proc_stats = processor.process(raw_items, use_ai=use_ai)
        log.info(f"Items for report: {len(processed)}")

        # ── 输出阶段：生成 Markdown 日报 ─────────────────────
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
        # 无论成功/失败都记录一次运行结果，保证 runs 表可用于排查历史趋势
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
    """
    后台常驻模式：按 settings.schedule.run_at（HH:MM）每天触发一次 run_once()。
    触发后 sleep 61 秒跳过当前分钟避免重复触发，其余时间每 30 秒轮询一次。
    """
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
