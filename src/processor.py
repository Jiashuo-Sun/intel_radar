"""
processor.py — 处理层编排（乐高积木：pipeline 中段编排）

═══════════════════════════════════════════════════════════════════════
功能作用
═══════════════════════════════════════════════════════════════════════
本文件不再自己实现打分规则或 AI 调用细节——那些已下沉为独立模块
scorer.py（打分）和 ai_client.py（AI 摘要）。processor.py 的职责收窄
为"编排三阶段过滤流程 + 决定哪些条目需要 AI 摘要"，是连接 database /
scorer / ai_client 三个独立乐高积木的胶水层。

三阶段过滤流程（每阶段独立计数，便于诊断"为什么日报条目这么少"）：
  Stage 1 · URL 精确去重  —— database.is_url_duplicate()
  Stage 2 · 打分过滤      —— scorer.calculate_score()
  Stage 3 · SimHash 近似去重 —— database.is_simhash_duplicate()

═══════════════════════════════════════════════════════════════════════
输入 / 输出
═══════════════════════════════════════════════════════════════════════
Processor(db, settings).process(raw_items, use_ai) -> (List[ProcessedItem], dict)
  输入：
    raw_items —— fetcher 产出的 RawItem 列表（未去重/未打分）
    use_ai    —— 是否允许调用 AI 摘要（对应 --no-ai 命令行参数）
  输出：
    (results, proc_stats)
    results    —— 通过全部三阶段过滤的 ProcessedItem 列表，按 score 降序排列
    proc_stats —— {"url_deduped": int, "score_filtered": int, "simhash_deduped": int}
                  三阶段各自过滤掉的数量，供 reporter 展示统计行

═══════════════════════════════════════════════════════════════════════
限制与边界
═══════════════════════════════════════════════════════════════════════
- **关键设计约束**：score < min_score 的条目不会调用 db.save_item()。
  若在此处改动逻辑导致低分条目也被存库，会在下次运行时被 Stage 1
  误判为"已见过"而被吞掉——这正是 knowledge/lessons-learned.md 记录
  的 Bug #1 的根因，修改本文件时必须保持这一约束不变。
- 三个过滤阶段的顺序不可随意调换：URL 去重必须先于打分（避免对已见
  过的条目重复计算分数、重复调用后续逻辑），打分必须先于 SimHash
  去重（避免对已确定无关的条目做近似比较，浪费计算）。
- AI 摘要只对"最终进入 results 的条目中，score >= ai_threshold"的
  子集调用，且受 ai_max（max_items_per_run）截断，用于控制单次运行
  的 AI 调用次数上限（成本/耗时控制）。
- 本文件不感知具体 AI provider 是什么（Anthropic/LM Studio），也不
  感知具体打分规则的正则内容——这两部分的修改分别去 ai_client.py 和
  scorer.py，不应该改动 processor.py。
"""
import logging
from typing import List, Tuple

from .models import RawItem, ProcessedItem
from .database import Database
from .scorer import calculate_score
from .ai_client import summarize_batch

log = logging.getLogger(__name__)

# 重新导出，保持对旧引用路径（如 "from .processor import ProcessedItem"）的兼容。
__all__ = ["ProcessedItem", "Processor"]


class Processor:
    def __init__(self, db: Database, settings: dict):
        self.db = db
        self.proc_cfg = settings.get("processor", {})
        self.ai_cfg = settings.get("ai", {})
        self.window_days = self.proc_cfg.get("dedup_window_days", 7)
        self.simhash_threshold = self.proc_cfg.get("simhash_threshold", 3)
        self.min_score = self.proc_cfg.get("min_score_for_report", 1)
        self.high_threshold = self.proc_cfg.get("high_priority_threshold", 4)
        self.ai_threshold = self.ai_cfg.get("summarize_threshold", 1)
        self.ai_max = self.ai_cfg.get("max_items_per_run", 20)

    def process(self, raw_items: List[RawItem], use_ai: bool = True
                ) -> Tuple[List[ProcessedItem], dict]:
        """
        对 raw_items 依次执行三阶段过滤，再对幸存条目做 AI 摘要。
        返回 (results, proc_stats)，见模块顶部文档。
        """
        results: List[ProcessedItem] = []
        url_deduped = 0
        score_filtered = 0
        simhash_deduped = 0

        for item in raw_items:
            # Stage 1: URL 精确匹配 —— 历史上已判定相关并存过库的条目
            if self.db.is_url_duplicate(item.url):
                url_deduped += 1
                continue

            # Stage 2: 打分过滤 —— 分数不够的条目不存库（保持去重索引干净）
            score = calculate_score(item)
            if score < self.min_score:
                score_filtered += 1
                continue

            # Stage 3: SimHash 近似去重 —— 标题与近期已存条目高度相似
            if self.db.is_simhash_duplicate(item.title, self.window_days, self.simhash_threshold):
                simhash_deduped += 1
                continue

            pi = ProcessedItem(raw=item, score=score)
            results.append(pi)
            self.db.save_item(item.url, item.title, item.source_name,
                              item.topic_group, score, item.published_at)

        log.info(
            f"Filter breakdown — kept: {len(results)} | "
            f"url-seen: {url_deduped} | "
            f"score<{self.min_score}: {score_filtered} | "
            f"simhash-dedup: {simhash_deduped}"
        )

        self._maybe_summarize(results, use_ai)

        results.sort(key=lambda x: x.score, reverse=True)

        proc_stats = {
            "url_deduped": url_deduped,
            "score_filtered": score_filtered,
            "simhash_deduped": simhash_deduped,
        }
        return results, proc_stats

    def _maybe_summarize(self, results: List[ProcessedItem], use_ai: bool) -> None:
        """
        对满足 AI 摘要门槛的条目调用 ai_client.summarize_batch()，
        并把结果回写数据库。use_ai=False 或 ai.enabled=False 时跳过。
        """
        if not (use_ai and self.ai_cfg.get("enabled")):
            return

        to_summarize = [p for p in results if p.score >= self.ai_threshold][: self.ai_max]
        if not to_summarize:
            log.info("No items meet AI summarization threshold")
            return

        log.info(
            f"AI summarizing {len(to_summarize)} items "
            f"(provider={self.ai_cfg.get('provider', 'anthropic')}, "
            f"threshold={self.ai_threshold})..."
        )
        summarize_batch(to_summarize, self.ai_cfg)
        for p in to_summarize:
            self.db.update_ai(p.url, p.summary_ai, p.impact)
