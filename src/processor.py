"""
processor.py — 去重、打分、AI摘要

Supports two AI providers:
  - anthropic: Anthropic cloud API (requires ANTHROPIC_API_KEY)
  - lmstudio:  LM Studio local inference (OpenAI-compatible, no key needed)

Provider is selected via settings.yaml ai.provider field.

Dedup strategy (three independent stages, each logged separately):
  1. URL-exact match  — skip if URL already in DB (seen and relevant before)
  2. Score filter     — skip if score < min_score (not relevant; NOT saved to DB)
  3. SimHash dedup    — skip if title too similar to a recent report item
"""
import re
import os
import json
import logging
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .fetcher import RawItem
from .database import Database

log = logging.getLogger(__name__)


@dataclass
class ProcessedItem:
    raw: RawItem
    score: int
    summary_ai: str = ""
    impact: str = ""

    @property
    def title(self): return self.raw.title
    @property
    def url(self): return self.raw.url
    @property
    def source_name(self): return self.raw.source_name
    @property
    def topic_group(self): return self.raw.topic_group
    @property
    def source_type(self): return self.raw.source_type
    @property
    def published_at(self): return self.raw.published_at
    @property
    def summary(self): return self.raw.summary

    @property
    def priority(self) -> str:
        if self.score >= 4:
            return "high"
        elif self.score >= 2:
            return "medium"
        return "low"


# ── 打分规则 ──────────────────────────────────────────────────────
SCORE_RULES = [
    (r"融资|募资|完成.*轮|Series [A-D]|funding|raises?\b", 3, "融资信号"),
    (r"中标|签约|合同|采购|awarded|contract", 3, "中标信号"),
    (r"收购|并购|acqui[rs]|merger", 3, "并购信号"),
    (r"发布|上线|launch|release|released|推出", 2, "产品发布"),
    (r"合作|partnership|partner|战略合作", 1, "合作信号"),
    (r"裁员|倒闭|shutdown|broke", -1, "负面信号"),
    (r"Werk24|werk24", 3, "竞品Werk24"),
    (r"Energent|energent", 3, "竞品Energent"),
    (r"CoLab|colab software", 2, "竞品CoLab"),
    (r"High\s*QA|HighQA", 2, "竞品HighQA"),
    (r"InspectionXpert|Ideagen", 2, "竞品Ideagen"),
    (r"1Factory", 2, "竞品1Factory"),
    (r"DISCUS", 2, "竞品DISCUS"),
    (r"志丞", 2, "竞品志丞"),
    (r"延峰|Yanfeng|YF\b", 3, "延峰"),
    (r"工业图纸|engineering drawing|technical drawing", 1, "核心场景"),
    (r"GD&T|PPAP|APQP|IATF", 1, "汽车质量标准"),
    (r"图纸识别|drawing recognition|drawing OCR", 2, "核心场景精确"),
    (r"arXiv|preprint", 1, "学术论文"),
]


def calculate_score(item: RawItem) -> int:
    text = f"{item.title} {item.summary}".lower()
    score = 0
    for pattern, delta, _ in SCORE_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            score += delta
    if item.topic_group == "premium":
        score += 1
    return max(0, score)


# ── AI 摘要 ───────────────────────────────────────────────────────
SYSTEM_PROMPT = """你是一个服务于工业AI产品团队的情报分析助手。
你的团队正在推进 Drawing AI（工业图纸智能识别平台）的商业落地，
核心客户为延峰集团（汽车内饰Tier1），主要竞品包括 Werk24、Energent.ai、High QA 等。

对每条情报，你需要输出：
1. 核心内容（≤25字，说清楚发生了什么）
2. 影响判断（≤30字，对我方业务有何影响，用"威胁/机会/中性"开头）
3. 建议行动（≤20字，具体可执行，无相关性则填"无"）

严格以 JSON 格式输出：
{"summary": "...", "impact": "...", "action": "..."}"""


def _parse_ai_json(text: str) -> dict:
    """Extract JSON from AI response, handling markdown code fences."""
    text = text.strip()
    if text.startswith("```"):
        inner, in_block = [], False
        for line in text.splitlines():
            if line.startswith("```"):
                in_block = not in_block
                continue
            if in_block:
                inner.append(line)
        text = "\n".join(inner).strip()
    return json.loads(text)


def _call_anthropic(text: str, model: str, api_key: str, timeout: int = 20) -> dict:
    """Call Anthropic Messages API, return parsed summary dict."""
    payload = json.dumps({
        "model": model,
        "max_tokens": 256,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": text}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
        return _parse_ai_json(data["content"][0]["text"])


def _call_lmstudio(text: str, model: str, base_url: str, timeout: int = 60) -> dict:
    """Call LM Studio OpenAI-compatible API, return parsed summary dict."""
    payload = json.dumps({
        "model": model,
        "max_tokens": 256,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }).encode()
    url = base_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": "Bearer lm-studio",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
        return _parse_ai_json(data["choices"][0]["message"]["content"])


def ai_summarize_batch(items: List[ProcessedItem], ai_cfg: dict) -> None:
    """Batch AI summarization with provider routing. Modifies items in-place."""
    provider = ai_cfg.get("provider", "anthropic")

    if provider == "lmstudio":
        lm_cfg = ai_cfg.get("lmstudio", {})
        base_url = lm_cfg.get("base_url", "http://localhost:1234/v1")
        model = lm_cfg.get("model", "local-model")
        timeout = lm_cfg.get("timeout_seconds", 60)
        log.info(f"AI provider: LM Studio  base_url={base_url}  model={model}")
        call_fn = lambda text: _call_lmstudio(text, model, base_url, timeout)
    else:
        # Anthropic — support both nested (ai.anthropic.*) and legacy flat keys
        ant_cfg = ai_cfg.get("anthropic", {})
        api_key_env = ant_cfg.get("api_key_env") or ai_cfg.get("api_key_env", "ANTHROPIC_API_KEY")
        model = ant_cfg.get("model") or ai_cfg.get("model", "claude-haiku-4-5-20251001")
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            log.warning(f"Env var {api_key_env!r} not set — skipping AI summary")
            return
        log.info(f"AI provider: Anthropic  model={model}")
        call_fn = lambda text: _call_anthropic(text, model, api_key)

    for item in items:
        text = f"标题：{item.title}\n摘要：{item.summary[:300]}\n来源：{item.source_name}"
        try:
            parsed = call_fn(text)
            item.summary_ai = parsed.get("summary", "")
            item.impact = parsed.get("impact", "") + "｜" + parsed.get("action", "")
        except Exception as e:
            log.warning(f"AI summarize failed for '{item.title[:30]}': {e}")


# ── 主处理流程 ────────────────────────────────────────────────────
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
        Process raw items through three sequential filters.
        Returns (results, stats) where stats breaks down each filter stage.
        """
        results: List[ProcessedItem] = []
        url_deduped = 0
        score_filtered = 0
        simhash_deduped = 0

        for item in raw_items:
            # Stage 1: URL exact match — already tracked as a relevant item before
            if self.db.is_url_duplicate(item.url):
                url_deduped += 1
                continue

            # Stage 2: Score filter — not relevant enough; do NOT save to DB
            score = calculate_score(item)
            if score < self.min_score:
                score_filtered += 1
                continue

            # Stage 3: SimHash dedup — similar article already in a recent report
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

        # AI summarization for all items that made it to the report
        if use_ai and self.ai_cfg.get("enabled"):
            to_summarize = [p for p in results if p.score >= self.ai_threshold][: self.ai_max]
            if to_summarize:
                log.info(
                    f"AI summarizing {len(to_summarize)} items "
                    f"(provider={self.ai_cfg.get('provider', 'anthropic')}, "
                    f"threshold={self.ai_threshold})..."
                )
                ai_summarize_batch(to_summarize, self.ai_cfg)
                for p in to_summarize:
                    self.db.update_ai(p.url, p.summary_ai, p.impact)
            else:
                log.info("No items meet AI summarization threshold")

        results.sort(key=lambda x: x.score, reverse=True)

        proc_stats = {
            "url_deduped": url_deduped,
            "score_filtered": score_filtered,
            "simhash_deduped": simhash_deduped,
        }
        return results, proc_stats
