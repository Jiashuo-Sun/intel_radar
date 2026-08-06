"""
models.py — 核心数据模型（乐高积木：基础数据层）

═══════════════════════════════════════════════════════════════════════
功能作用
═══════════════════════════════════════════════════════════════════════
定义贯穿整条 pipeline 的两个核心数据结构：
  - RawItem：采集层（fetcher）产出的原始情报条目
  - ProcessedItem：处理层（processor）产出的已打分/已摘要条目

这是整个系统里唯一被所有其他模块间接依赖的"公共语言"——fetcher 产出
RawItem，processor 消费 RawItem 并产出 ProcessedItem，reporter 消费
ProcessedItem。模块之间不直接调用彼此的内部逻辑，只通过这两个数据结构
传递信息，这是实现"高内聚低耦合"的关键设计。

═══════════════════════════════════════════════════════════════════════
输入 / 输出
═══════════════════════════════════════════════════════════════════════
本文件不含任何 I/O 逻辑，只有 dataclass 定义。无网络、无文件、无数据库。

═══════════════════════════════════════════════════════════════════════
限制与边界
═══════════════════════════════════════════════════════════════════════
- 本模块零依赖（仅使用 Python 标准库 dataclasses/typing），可被任意项目
  直接复制使用，无需引入其他文件。
- 不包含任何业务规则（打分、去重、摘要逻辑均不在此处，见 scorer.py /
  database.py / ai_client.py）。
- 字段变更会影响下游所有模块，修改前需确认 fetcher / processor /
  reporter 三层的字段访问都已同步更新。
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class RawItem:
    """
    采集层的原始条目，代表一条尚未打分/去重/摘要的情报。

    字段说明：
      source_type   —— 数据源类型，取值 "rss" | "webpage" | "arxiv"
      source_name   —— 人类可读的来源名（如 "Werk24"、"Google News / xxx"）
      topic_group   —— 所属分组 key，来自 watch.yaml（如 "premium"、"drawing_ai"）
      title         —— 标题（必填，空标题的条目不应被构造）
      url           —— 原文链接（必填，用于去重主键）
      summary       —— 原始摘要/正文片段，可能为空字符串
      published_at  —— 发布时间的 ISO8601 字符串，解析失败时可为 None

    边界：本类不做任何字段校验（如 URL 格式），调用方需自行保证
    title 和 url 非空后再构造实例。
    """
    source_type: str
    source_name: str
    topic_group: str
    title: str
    url: str
    summary: str = ""
    published_at: Optional[str] = None


@dataclass
class ProcessedItem:
    """
    处理层输出的条目：在 RawItem 基础上附加打分和 AI 摘要结果。

    采用组合（raw: RawItem）而非继承，保持 RawItem 的"纯采集数据"语义
    不被处理层字段污染；通过 @property 转发常用字段，方便 reporter 层
    像访问扁平对象一样使用（item.title 而非 item.raw.title）。

    字段说明：
      raw         —— 原始 RawItem，只读引用
      score       —— 打分结果（0-10，见 scorer.py），由 processor 计算后填入
      summary_ai  —— AI 生成的一句话摘要，未调用 AI 时为空字符串
      impact      —— AI 生成的"影响判断｜建议行动"，用全角竖线拼接

    priority 属性根据 score 派生优先级（high/medium/low），阈值硬编码
    为 4 和 2 —— 与 config/settings.yaml 中 high_priority_threshold 的
    默认值保持一致。若需要可配置阈值，应在 Processor 中重新计算
    priority 并覆盖此默认值，而非修改本类。
    """
    raw: RawItem
    score: int
    summary_ai: str = ""
    impact: str = ""

    @property
    def title(self) -> str:
        return self.raw.title

    @property
    def url(self) -> str:
        return self.raw.url

    @property
    def source_name(self) -> str:
        return self.raw.source_name

    @property
    def topic_group(self) -> str:
        return self.raw.topic_group

    @property
    def source_type(self) -> str:
        return self.raw.source_type

    @property
    def published_at(self) -> Optional[str]:
        return self.raw.published_at

    @property
    def summary(self) -> str:
        return self.raw.summary

    @property
    def priority(self) -> str:
        """派生优先级：score>=4 → high；score>=2 → medium；否则 low。"""
        if self.score >= 4:
            return "high"
        elif self.score >= 2:
            return "medium"
        return "low"
