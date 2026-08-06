"""
ai_client.py — AI 摘要客户端（乐高积木：可插拔 AI 提供商层）

═══════════════════════════════════════════════════════════════════════
功能作用
═══════════════════════════════════════════════════════════════════════
封装"把一段情报文本发给 AI，拿回结构化摘要"这件事，并通过注册表模式
（PROVIDERS 字典）支持多个可插拔的后端：
  - anthropic：Anthropic 云端 Messages API
  - lmstudio ：LM Studio 本地推理（OpenAI 兼容接口）

新增第三个 provider（如 OpenAI、Ollama、Gemini）时，只需：
  1. 写一个 `_call_xxx(text, cfg) -> dict` 函数
  2. 在 PROVIDERS 字典里注册 "xxx": _call_xxx
不需要修改 summarize_batch() 或调用方 processor.py 的任何代码——这是
本次重构对"拓展性"债务（原 if/elif 硬编码）的具体修复。

═══════════════════════════════════════════════════════════════════════
输入 / 输出
═══════════════════════════════════════════════════════════════════════
summarize_batch(items, ai_cfg) -> None
  输入：
    items   —— List[ProcessedItem]，会被原地修改（写入 summary_ai/impact）
    ai_cfg  —— settings.yaml 中的 ai 配置字典（含 provider/anthropic/
                lmstudio 子配置）
  输出：无返回值（副作用：逐条填充 item.summary_ai 和 item.impact）
        单条调用失败只记录 warning 并跳过，不影响其余条目、不抛异常
        中断整批处理。

parse_ai_json(text) -> dict
  输入：AI 返回的原始文本（可能含 markdown 代码围栏或前置说明文字）
  输出：解析出的 dict；若确实找不到合法 JSON，抛出 ValueError
        （这是本模块唯一会向上抛异常的函数，由调用方 _call_xxx 的
        try 逻辑或 summarize_batch 的外层 try 捕获）

═══════════════════════════════════════════════════════════════════════
限制与边界
═══════════════════════════════════════════════════════════════════════
- provider 选择依赖 ai_cfg["provider"] 字符串，未识别的 provider 名称
  会静默回退到 "anthropic" 分支（保持向后兼容旧配置文件）。若需要严格
  校验，应在 Processor 初始化阶段做配置校验，而非在本模块内报错。
- Anthropic 分支要求环境变量中存在 API Key（变量名可配置，默认
  ANTHROPIC_API_KEY），缺失时记录 warning 并直接返回（不调用 AI，
  不影响主流程——AI 摘要设计为可选增强，不是流程必需环节）。
- LM Studio 分支假设本地服务已启动且可达，不做启动前探活；如服务
  未启动，单条请求会超时失败并被 warning 跳过（不会阻塞整批）。
- parse_ai_json 的 regex 兜底只匹配"不含嵌套花括号"的单层 JSON
  对象（`\{[^{}]*\}`），如果 AI 返回的 JSON 内部还有嵌套对象/数组，
  兜底会失败。当前 SYSTEM_PROMPT 要求的输出结构是扁平的
  {summary, impact, action}，不存在嵌套，因此暂不需要更复杂的解析器。
- SYSTEM_PROMPT 硬编码了产品业务背景（Drawing AI / 延峰 / 竞品名单），
  这是本模块唯一"不通用"的部分——若复用到其他项目，需要替换这段
  prompt 文本，其余代码（HTTP 调用、JSON 解析、provider 路由）可
  直接复用。
"""
import os
import re
import json
import logging
import urllib.request
from typing import List

log = logging.getLogger(__name__)

# 情报分析的角色设定与输出格式约束。
# 注意：这是本模块中唯一与"Intel Radar"业务强绑定的部分；复用到其他
# 项目时应替换为对应的角色设定。
SYSTEM_PROMPT = """你是一个服务于工业AI产品团队的情报分析助手。
你的团队正在推进 Drawing AI（工业图纸智能识别平台）的商业落地，
核心客户为延峰集团（汽车内饰Tier1），主要竞品包括 Werk24、Energent.ai、High QA 等。

对每条情报，你需要输出：
1. 核心内容（≤25字，说清楚发生了什么）
2. 影响判断（≤30字，对我方业务有何影响，用"威胁/机会/中性"开头）
3. 建议行动（≤20字，具体可执行，无相关性则填"无"）

严格以 JSON 格式输出：
{"summary": "...", "impact": "...", "action": "..."}"""


def parse_ai_json(text: str) -> dict:
    """
    从 AI 返回文本中提取 JSON 对象。

    处理三种情况，按顺序尝试：
      1. 文本整体就是合法 JSON（含被 ```/```json 围栏包裹的情况，
         围栏会先被剥离）
      2. 剥离围栏后仍不是合法 JSON，尝试直接 json.loads 整个字符串
      3. 以上都失败，用正则在文本中提取第一个 {...} 块再解析
         （处理"以下是分析结果：\n{...}"这类前置说明文字的情况）

    三种方式都失败时抛出 ValueError，附带原文本前 120 字符便于调试。
    """
    text = text.strip()
    if "```" in text:
        inner, in_block = [], False
        for line in text.splitlines():
            if line.startswith("```"):
                in_block = not in_block
                continue
            if in_block:
                inner.append(line)
        text = "\n".join(inner).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group())
    raise ValueError(f"No JSON object found in AI response: {text[:120]!r}")


def _call_anthropic(text: str, cfg: dict) -> dict:
    """
    调用 Anthropic Messages API。
    cfg 需含：model, api_key（已从环境变量读取好的明文 key）。
    超时/网络错误会向上抛出异常，由 summarize_batch 的 try 块捕获。
    """
    payload = json.dumps({
        "model": cfg["model"],
        "max_tokens": 256,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": text}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": cfg["api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=cfg.get("timeout", 20)) as resp:
        data = json.loads(resp.read())
        return parse_ai_json(data["content"][0]["text"])


def _call_lmstudio(text: str, cfg: dict) -> dict:
    """
    调用 LM Studio 本地 OpenAI 兼容接口。
    cfg 需含：model, base_url。Authorization 头使用固定占位符
    "Bearer lm-studio"（LM Studio 本地服务不校验真实 key）。
    """
    payload = json.dumps({
        "model": cfg["model"],
        "max_tokens": 256,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }).encode()
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": "Bearer lm-studio",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=cfg.get("timeout", 60)) as resp:
        data = json.loads(resp.read())
        return parse_ai_json(data["choices"][0]["message"]["content"])


# 注册表：provider 名称 -> 调用函数。
# 新增 provider 时只需在此追加一行，无需改动 summarize_batch()。
PROVIDERS = {
    "anthropic": _call_anthropic,
    "lmstudio": _call_lmstudio,
}


def _build_call_fn(ai_cfg: dict):
    """
    根据 ai_cfg 构造 (call_fn, provider_name) 或 (None, provider_name)。
    call_fn 为 None 表示该 provider 因缺少必要配置（如 API Key）而
    不可用，调用方应据此跳过 AI 摘要而不是让异常向上传播。
    """
    provider = ai_cfg.get("provider", "anthropic")

    if provider == "lmstudio":
        lm_cfg = ai_cfg.get("lmstudio", {})
        cfg = {
            "model": lm_cfg.get("model", "local-model"),
            "base_url": lm_cfg.get("base_url", "http://localhost:1234/v1"),
            "timeout": lm_cfg.get("timeout_seconds", 60),
        }
        log.info(f"AI provider: LM Studio  base_url={cfg['base_url']}  model={cfg['model']}")
        return lambda text: PROVIDERS["lmstudio"](text, cfg), provider

    # 默认走 anthropic 分支（含未识别 provider 名的向后兼容回退）
    ant_cfg = ai_cfg.get("anthropic", {})
    api_key_env = ant_cfg.get("api_key_env") or ai_cfg.get("api_key_env", "ANTHROPIC_API_KEY")
    model = ant_cfg.get("model") or ai_cfg.get("model", "claude-haiku-4-5-20251001")
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        log.warning(f"Env var {api_key_env!r} not set — skipping AI summary")
        return None, "anthropic"
    cfg = {"model": model, "api_key": api_key, "timeout": 20}
    log.info(f"AI provider: Anthropic  model={model}")
    return lambda text: PROVIDERS["anthropic"](text, cfg), "anthropic"


def summarize_batch(items: List, ai_cfg: dict) -> None:
    """
    对一批 ProcessedItem 做 AI 摘要，原地写入 item.summary_ai / item.impact。

    provider 不可用（如缺 API Key）时直接返回，不修改任何 item。
    单条调用失败只 warning 并跳过该条，不影响批次内其余条目。

    注意：本函数接受任意鸭子类型对象（只要求有 .title / .summary /
    .source_name 属性，以及可写的 .summary_ai / .impact 属性），不
    直接依赖 ProcessedItem 类型，进一步降低与 models.py 的耦合。
    """
    call_fn, provider = _build_call_fn(ai_cfg)
    if call_fn is None:
        return

    for item in items:
        text = f"标题：{item.title}\n摘要：{item.summary[:300]}\n来源：{item.source_name}"
        try:
            parsed = call_fn(text)
            item.summary_ai = parsed.get("summary", "")
            item.impact = parsed.get("impact", "") + "｜" + parsed.get("action", "")
        except Exception as e:
            log.warning(f"AI summarize failed for '{item.title[:30]}': {e}")
