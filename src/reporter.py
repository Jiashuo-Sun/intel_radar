"""
reporter.py — 输出层（乐高积木：渲染层）

═══════════════════════════════════════════════════════════════════════
功能作用
═══════════════════════════════════════════════════════════════════════
把 ProcessedItem 列表渲染成结构化的 Markdown 日报文件。是一个纯粹的
"数据 → 文本"转换层：不做去重、不做打分、不调用 AI、不访问数据库。
只依赖 models.py（数据结构），不依赖 processor.py——这是本次重构刻意
调整的一处耦合方向：旧版本 reporter 从 processor 导入 ProcessedItem，
形成"输出层依赖处理层实现细节"的不必要耦合；现在两者都只依赖公共的
models.py，彼此互不感知，可以独立测试、独立替换。

═══════════════════════════════════════════════════════════════════════
输入 / 输出
═══════════════════════════════════════════════════════════════════════
Reporter(settings).generate(items, report_date, stats) -> str
  输入：
    items       —— List[ProcessedItem]，通常是 processor.process() 的输出
    report_date —— 日报归属日期（date 对象）
    stats       —— dict，包含 new/url_deduped/score_filtered/simhash_deduped
                    等统计字段，用于日报头部展示过滤分布
  输出：生成的日报文件绝对/相对路径（取决于 settings 中 output.dir 的配置）

═══════════════════════════════════════════════════════════════════════
限制与边界
═══════════════════════════════════════════════════════════════════════
- 写文件采用原子写入模式：先写临时文件 `path + ".tmp"`，成功后再用
  os.replace() 原子替换目标文件。这样即使进程在写入过程中被杀死，也
  不会留下半截内容的日报文件——读者要么看到完整的旧版本，要么看到
  完整的新版本，不会看到损坏的中间态。
- GROUP_META 是硬编码的分组展示元数据（emoji + 中文标签），与
  watch.yaml 中 topics 各分组的定义存在重复维护的风险（新增一个
  topic 分组时，watch.yaml 和这里都要改）。这是已知的拓展性债务，
  见 knowledge/todo.md P2 项。
- 同一天重复运行会直接覆盖旧日报文件（而不是追加），这是刻意设计：
  DB 层的 URL 去重已经保证"新内容会追加、旧内容不重复出现"，reporter
  每次都是根据 processor 传入的完整 items 列表重新渲染整份日报，
  保持文件内容与当次运行结果一致。
"""
import os
from datetime import datetime, date
from typing import List, Optional
import logging

from .models import ProcessedItem

log = logging.getLogger(__name__)

# 各分组的显示配置
GROUP_META = {
    "premium":           {"label": "精品监控",   "emoji": "🎯"},
    "drawing_ai":        {"label": "图纸AI核心", "emoji": "📐"},
    "yanfeng_ecosystem": {"label": "延峰生态",   "emoji": "🚗"},
    "tech_frontier":     {"label": "技术前沿",   "emoji": "🔬"},
    "market_signals":    {"label": "市场信号",   "emoji": "📊"},
}


def _source_badge(item: ProcessedItem) -> str:
    """生成条目的来源标记文本：arXiv 条目固定显示 "arXiv"，其余按 topic_group 查 GROUP_META。"""
    if item.source_type == "arxiv":
        return "arXiv"
    meta = GROUP_META.get(item.topic_group, {})
    return meta.get("label", item.topic_group)


def _format_date(s: Optional[str]) -> str:
    """
    把 ISO8601 日期字符串格式化为 "MM-DD HH:MM" 展示形式。
    输入为空或解析失败时分别返回空字符串 / 原字符串前 10 位，不抛异常
    （日期解析失败不应导致整份日报生成失败）。
    """
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s[:19])
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return s[:10]


class Reporter:
    def __init__(self, settings: dict):
        self.output_dir = settings.get("output", {}).get("dir", "./output")
        self.filename_fmt = settings.get("output", {}).get("filename_format", "%Y-%m-%d.md")
        os.makedirs(self.output_dir, exist_ok=True)

    def report_path(self, report_date: date) -> str:
        fname = report_date.strftime(self.filename_fmt)
        return os.path.join(self.output_dir, fname)

    def generate(self, items: List[ProcessedItem], report_date: date,
                 stats: dict) -> str:
        """生成 Markdown 日报，返回文件路径"""
        path = self.report_path(report_date)

        # 分组
        high    = [i for i in items if i.priority == "high"]
        medium  = [i for i in items if i.priority == "medium"]
        arxiv   = [i for i in items if i.source_type == "arxiv"]
        medium_non_arxiv = [i for i in medium if i.source_type != "arxiv"]

        lines = []

        # ── 标题 ──────────────────────────────────────────────
        lines.append(f"# 情报日报 · {report_date.strftime('%Y-%m-%d')}\n")
        lines.append(
            f"> 采集时间：{datetime.now().strftime('%H:%M')}  "
            f"| 进入日报：{stats.get('new', 0)}  "
            f"| 高优先级：{len(high)}  "
            f"| 已见过：{stats.get('url_deduped', stats.get('deduped', 0))}  "
            f"| 低相关过滤：{stats.get('score_filtered', 0)}  "
            f"| 近似去重：{stats.get('simhash_deduped', 0)}\n"
        )
        lines.append("---\n")

        # ── 高优先级（详细展开）──────────────────────────────
        if high:
            lines.append("## 🔴 高优先级（今日必读）\n")
            for item in high:
                lines.append(f"### [{item.title}]({item.url})\n")
                badge = _source_badge(item)
                date_str = _format_date(item.published_at)
                lines.append(f"**来源：** {item.source_name}  ·  `{badge}`  ·  {date_str}  ")
                lines.append(f"**评分：** {item.score}/10\n")
                if item.summary_ai:
                    lines.append(f"**摘要：** {item.summary_ai}\n")
                    if item.impact:
                        parts = item.impact.split("｜")
                        lines.append(f"**影响：** {parts[0]}\n")
                        if len(parts) > 1 and parts[1] and parts[1] != "无":
                            lines.append(f"**行动：** {parts[1]}\n")
                elif item.summary:
                    # 无 AI 摘要时用原始摘要
                    snippet = item.summary[:200].replace("\n", " ")
                    lines.append(f"**摘要：** {snippet}…\n")
                lines.append("")
        else:
            lines.append("## 🔴 高优先级（今日必读）\n\n> 今日无高优先级条目\n")

        lines.append("---\n")

        # ── 中优先级：竞品 vs 行业，分两个子分区 ─────────────
        premium_medium = [i for i in medium_non_arxiv if i.topic_group == "premium"]
        other_medium   = [i for i in medium_non_arxiv if i.topic_group != "premium"]

        if premium_medium:
            lines.append("## 🔵 竞品动态\n")
            lines.append("| 公司/来源 | 标题 | 日期 |")
            lines.append("|-----------|------|------|")
            for item in premium_medium:
                date_str = _format_date(item.published_at)
                title_link = f"[{item.title[:60]}]({item.url})"
                lines.append(f"| {item.source_name} | {title_link} | {date_str} |")
            lines.append("")

        if other_medium:
            lines.append("## 🟡 行业动态\n")
            # 按 topic_group 再细分
            by_group: dict = {}
            for item in other_medium:
                by_group.setdefault(item.topic_group, []).append(item)

            for group_key, group_items in by_group.items():
                meta = GROUP_META.get(group_key, {"label": group_key, "emoji": "📌"})
                lines.append(f"### {meta['emoji']} {meta['label']}\n")
                for item in group_items:
                    date_str = _format_date(item.published_at)
                    snippet = item.summary[:100].replace("\n", " ") if item.summary else ""
                    lines.append(f"- **[{item.title[:70]}]({item.url})**")
                    if snippet:
                        lines.append(f"  {snippet}…")
                    if date_str:
                        lines.append(f"  *{item.source_name} · {date_str}*\n")
                    else:
                        lines.append(f"  *{item.source_name}*\n")

        lines.append("---\n")

        # ── 技术前沿（arXiv）──────────────────────────────────
        if arxiv:
            lines.append("## 📚 技术前沿（arXiv 新论文）\n")
            lines.append("| 论文标题 | 发布时间 | 摘要片段 |")
            lines.append("|---------|---------|---------|")
            for item in arxiv[:10]:
                date_str = _format_date(item.published_at)
                snippet = item.summary[:80].replace("\n", " ").replace("|", "，") if item.summary else ""
                title_link = f"[{item.title[:55]}]({item.url})"
                lines.append(f"| {title_link} | {date_str} | {snippet}… |")
            lines.append("")
        else:
            lines.append("## 📚 技术前沿\n\n> 今日无新论文\n")

        lines.append("---\n")

        # ── 尾部 ──────────────────────────────────────────────
        total_shown = len(high) + len(medium) + len(arxiv)
        archived = stats.get("new", 0) - total_shown
        if archived > 0:
            lines.append(f"## 📦 归档\n\n本日另有 **{archived}** 条低相关条目已存库，不显示在日报中。\n")

        lines.append("---")
        lines.append(f"*Intel Radar · 下次运行：明日 {datetime.now().strftime('%H:%M')}*")

        content = "\n".join(lines)

        # 原子写入：先写临时文件，成功后再用 os.replace() 替换目标文件，
        # 避免进程中途被杀死时留下半截内容的日报。
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)

        log.info(f"Report written: {path}")
        return path
