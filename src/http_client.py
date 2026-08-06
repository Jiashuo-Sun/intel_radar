"""
http_client.py — 通用 HTTP GET 客户端（乐高积木：网络原子能力）

═══════════════════════════════════════════════════════════════════════
功能作用
═══════════════════════════════════════════════════════════════════════
提供一个不依赖任何第三方库的、带重试与指数退避的 HTTP GET 封装。
仅使用 Python 标准库 urllib，不需要 requests，方便在无外部依赖环境中
直接复用。

这是全项目唯一发起网络请求的地方——fetcher.py 中的 RssFetcher /
ArxivFetcher / WebFetcher 都通过本模块获取原始文本，不自行处理
urllib 细节。这样做的好处：将来替换底层 HTTP 库（比如换成 requests
或 httpx）只需改这一个文件。

═══════════════════════════════════════════════════════════════════════
输入 / 输出
═══════════════════════════════════════════════════════════════════════
fetch_url(url, timeout, ua, retries) -> str
  输入：
    url      —— 目标地址，可包含非 ASCII 字符（会自动编码）
    timeout  —— 单次请求超时秒数
    ua       —— User-Agent 请求头
    retries  —— 失败后的重试次数（不含首次请求）
  输出：
    成功：响应体解码后的文本（按响应头 charset 解码，缺失时按 utf-8，
          解码失败的字节用 replace 策略处理，不抛异常）
    失败：空字符串 ""（网络错误、超时、HTTP 错误码均在内部捕获并返回空串，
          不向上抛出异常 —— 调用方无需 try/except，只需判断返回值是否为空）

═══════════════════════════════════════════════════════════════════════
限制与边界
═══════════════════════════════════════════════════════════════════════
- 仅支持 GET 请求，不支持 POST/PUT 等（AI 客户端的 POST 请求见
  ai_client.py，因为语义不同——那里失败需要抛异常而非静默返回空串）。
- 重试采用固定的指数退避序列：2s → 4s → 8s...（初始 2s，每次翻倍），
  不支持自定义退避策略。高频调用场景下退避总耗时可能较长，调用方应
  根据实际超时预算设置合理的 retries 值。
- 不处理需要登录/Cookie/JS 渲染的页面（那是 PlaywrightFetcher 等
  按需组件的职责，本模块只做最基础的静态 GET）。
- 不做速率限制/并发控制，调用频率由上层 fetcher.py 中的 time.sleep()
  礼貌延迟负责。
"""
import time
import logging
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)


def _encode_url(url: str) -> str:
    """
    将 URL 中的非 ASCII / 特殊字符正确编码，避免 urllib 因中文查询词等
    报错。仅对 path 和 query 部分编码，scheme/netloc 保持不变。
    """
    parsed = urllib.parse.urlparse(url)
    encoded = parsed._replace(
        path=urllib.parse.quote(parsed.path, safe="/:@!$&'()*+,;="),
        query=urllib.parse.quote(parsed.query, safe="=&+%:"),
    )
    return urllib.parse.urlunparse(encoded)


def fetch_url(url: str, timeout: int = 15, ua: str = "IntelRadar/1.0",
              retries: int = 2) -> str:
    """
    发起 GET 请求并返回文本内容，失败时按指数退避重试。

    参数：
      url      —— 目标地址
      timeout  —— 单次请求超时秒数，默认 15
      ua       —— User-Agent，默认 "IntelRadar/1.0"
      retries  —— 失败后重试次数（不含首次），默认 2（即最多请求 3 次）

    返回：
      成功返回响应文本；耗尽重试次数后仍失败则返回空字符串 ""。
      调用方约定：判断 `if not text:` 即可识别失败，无需 catch 异常。
    """
    safe_url = _encode_url(url)
    req = urllib.request.Request(safe_url, headers={"User-Agent": ua})
    delay = 2
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except Exception as e:
            if attempt < retries:
                log.warning(
                    f"Fetch attempt {attempt + 1} failed: {safe_url} — {e}; "
                    f"retrying in {delay}s"
                )
                time.sleep(delay)
                delay *= 2
            else:
                log.warning(
                    f"Fetch failed after {retries + 1} attempts: {safe_url} — {e}"
                )
    return ""
