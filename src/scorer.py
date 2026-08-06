"""
scorer.py — 关键词打分引擎（乐高积木：业务规则层）

═══════════════════════════════════════════════════════════════════════
功能作用
═══════════════════════════════════════════════════════════════════════
对一条 RawItem 按预定义的关键词规则表打分（0-10 分），分数决定该条目
是否进入日报、进入哪个优先级区块。这是全项目"什么内容值得关注"的
唯一判断依据，独立于采集来源（RSS/网页/arXiv 用同一套规则打分）、
独立于去重逻辑（打分与是否重复无关）、独立于 AI 摘要（打分不调用
任何外部服务，是纯本地正则匹配，速度快、零成本、可离线运行）。

═══════════════════════════════════════════════════════════════════════
输入 / 输出
═══════════════════════════════════════════════════════════════════════
calculate_score(item) -> int
  输入：RawItem 实例（读取其 title / summary / topic_group 字段）
  输出：0 到理论上限之间的整数分数（各规则命中分值之和，负分规则
        可拉低总分，但最终结果会被截断为不小于 0）

═══════════════════════════════════════════════════════════════════════
限制与边界
═══════════════════════════════════════════════════════════════════════
- SCORE_RULES 目前是 Python 源码中的常量列表，不是 YAML 配置。这是
  已知的可扩展性债务（见 knowledge/todo.md P2 项）：优点是启动快、
  无需额外解析；缺点是调整打分权重需要改代码、重新部署。若未来迁移
  到配置文件，只需替换 SCORE_RULES 的加载方式，calculate_score() 的
  接口不变，不影响调用方（processor.py）。
- 规则基于正则表达式在"标题+摘要"拼接文本上做子串匹配，不理解语义。
  同一关键词出现多次只按命中/未命中计一次分（re.search 只判断是否
  存在，不计数），因此"融资"出现 3 次和出现 1 次分数相同。
- 大小写不敏感（re.IGNORECASE），但不做同义词归一化（如"人工智能"
  和"AI"是两条独立规则，需要都配置才能都命中）。
- topic_group == "premium" 的条目会额外 +1 分（体现"精品监控命中
  即比同等内容的广播抓取更重要"的产品设计意图，见 CLAUDE.md 精品
  监控章节）。
"""
import re

from .models import RawItem

# ── 打分规则表 ──────────────────────────────────────────────────────
# 每条规则：(正则表达式, 命中分值, 规则说明)
# 规则说明字段目前仅用于代码可读性/调试，未参与运行时逻辑。
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

# 精品监控命中的固定加分（见模块说明）
_PREMIUM_BONUS = 1


def calculate_score(item: RawItem) -> int:
    """
    对一条 RawItem 按 SCORE_RULES 计算总分。

    计算方式：拼接 "title + summary"（转小写）后，依次用每条规则的
    正则做 re.search；命中则累加该规则的 delta（可正可负）。
    topic_group 为 "premium" 的条目额外加 _PREMIUM_BONUS 分。
    最终结果用 max(0, ...) 截断，保证返回值不为负数
    （即使命中多条负面信号规则，最低也是 0 分，不会产生"负分优先级"
    这种无意义状态）。
    """
    text = f"{item.title} {item.summary}".lower()
    score = 0
    for pattern, delta, _ in SCORE_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            score += delta
    if item.topic_group == "premium":
        score += _PREMIUM_BONUS
    return max(0, score)
