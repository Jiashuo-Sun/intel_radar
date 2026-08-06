"""
feed_parser.py — RSS/Atom/arXiv/HTML 纯解析层（乐高积木：解析原子能力）

═══════════════════════════════════════════════════════════════════════
功能作用
═══════════════════════════════════════════════════════════════════════
把"下载文本"和"解析文本"两件事拆开：http_client.py 只管拿到原始
字符串，本模块只管把字符串解析成结构化的 RawItem 列表。不发起任何
网络请求，纯函数式（相同输入永远得到相同输出），因此可以脱离网络
环境单独用单元测试验证解析逻辑是否正确（例如把一段固定的 RSS XML
字符串粘进测试用例）。

支持三类输入格式：
  - RSS 2.0 / Atom Feed（Google News、公司博客等常见格式）
  - arXiv API 返回的 Atom Feed（字段语义略有不同，单独处理）
  - 静态 HTML 页面中按 CSS selector 提取的新闻链接列表

═══════════════════════════════════════════════════════════════════════
输入 / 输出
═══════════════════════════════════════════════════════════════════════
parse_rss_atom(xml_text, source_name, topic_group) -> List[RawItem]
  输入：RSS/Atom 格式的 XML 字符串 + 元数据（来源名、分组）
  输出：RawItem 列表；XML 非法或无条目时返回空列表（不抛异常）

parse_arxiv(xml_text) -> List[RawItem]
  输入：arXiv API 返回的 Atom XML 字符串
  输出：RawItem 列表，topic_group 固定为 "tech_frontier"
        （arXiv 本身只用于技术前沿监控，不接受外部指定分组）

parse_html_links(html_text, base_url, selector, source_name, topic_group,
                  bs4_class) -> List[RawItem]
  输入：HTML 字符串 + CSS selector + BeautifulSoup 类（依赖注入，
        避免本模块强制依赖 bs4 包）
  输出：RawItem 列表，最多取前 20 条命中元素

═══════════════════════════════════════════════════════════════════════
限制与边界
═══════════════════════════════════════════════════════════════════════
- 只做"能解析多少解析多少"的宽容处理：单条 item 缺少必填字段（标题或
  链接为空）会被静默跳过，不影响其余条目的解析，也不记录日志（日志
  职责交给调用方 fetcher.py，本模块保持无副作用）。
- 日期解析尝试三种常见格式，都失败时退化为取字符串前 19 位或原样返回，
  不保证是合法的 ISO8601（下游 reporter._format_date 对非法日期也有
  兜底处理）。
- parse_html_links 依赖调用方传入已导入的 BeautifulSoup 类（而非在本
  模块内部 import bs4），这样即使运行环境没装 bs4，只要不调用这个
  函数，模块本身仍可正常 import——这是"低耦合"的具体体现之一。
- 不做 HTML/XML 清洗或 XSS 过滤，因为输出只用于生成 Markdown 文本，
  不会被渲染为 HTML 页面。若未来输出层改为渲染 HTML，需要在 reporter
  层补充转义。
"""
import xml.etree.ElementTree as ET
import urllib.parse
from datetime import datetime
from typing import List, Optional

from .models import RawItem

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _parse_date(s: str) -> Optional[str]:
    """
    尝试将 RSS/Atom 常见日期格式解析为 ISO8601 字符串。
    依次尝试 RFC822（RSS 常用）、带时区 ISO、UTC 'Z' 后缀 ISO 三种格式；
    全部失败时退化为截取原字符串前 19 位（尽力保留可读的日期时间部分）。
    空字符串输入返回 None。
    """
    if not s:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s.strip(), fmt).isoformat()
        except Exception:
            pass
    return s[:19]


def parse_rss_atom(xml_text: str, source_name: str, topic_group: str) -> List[RawItem]:
    """
    解析 RSS 2.0 或 Atom 格式的 XML 文本为 RawItem 列表。
    两种格式的标签结构不同（RSS 用 <item>，Atom 用 <entry>），本函数
    同时尝试两种路径，兼容大多数 Feed 来源（包括 Google News RSS）。
    XML 解析失败（ET.ParseError）时返回空列表，不抛异常。
    """
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    items: List[RawItem] = []

    # RSS 2.0: <channel><item>...
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        date = _parse_date(item.findtext("pubDate") or "")
        if title and url:
            items.append(RawItem(
                source_type="rss", source_name=source_name,
                topic_group=topic_group, title=title,
                url=url, summary=desc[:500], published_at=date,
            ))

    # Atom: <feed><entry>...
    for entry in root.findall("atom:entry", _ATOM_NS):
        title = (entry.findtext("atom:title", namespaces=_ATOM_NS) or "").strip()
        link = entry.find("atom:link", _ATOM_NS)
        url = (link.attrib.get("href", "") if link is not None else "").strip()
        summ = (entry.findtext("atom:summary", namespaces=_ATOM_NS) or "").strip()
        date = _parse_date(entry.findtext("atom:updated", namespaces=_ATOM_NS) or "")
        if title and url:
            items.append(RawItem(
                source_type="rss", source_name=source_name,
                topic_group=topic_group, title=title,
                url=url, summary=summ[:500], published_at=date,
            ))
    return items


def parse_arxiv(xml_text: str) -> List[RawItem]:
    """
    解析 arXiv API 返回的 Atom XML 为 RawItem 列表。
    与通用 Atom 解析的区别：arXiv 用 <id> 标签存论文永久链接（而非
    <link href=...>），且所有条目固定归入 "tech_frontier" 分组、
    source_name 固定为 "arXiv"（因为 arXiv 采集器本身只服务于技术
    前沿监控场景，见 CLAUDE.md 中 tech_frontier 分组定义）。
    """
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    items: List[RawItem] = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        title = (entry.findtext("atom:title", namespaces=_ATOM_NS) or "").replace("\n", " ").strip()
        url_el = entry.find("atom:id", _ATOM_NS)
        url = (url_el.text or "").strip() if url_el is not None else ""
        summary = (entry.findtext("atom:summary", namespaces=_ATOM_NS) or "").replace("\n", " ").strip()
        date = _parse_date(entry.findtext("atom:published", namespaces=_ATOM_NS) or "")
        if title and url:
            items.append(RawItem(
                source_type="arxiv", source_name="arXiv",
                topic_group="tech_frontier", title=title,
                url=url, summary=summary[:600], published_at=date,
            ))
    return items


def parse_html_links(html_text: str, base_url: str, selector: str,
                      source_name: str, topic_group: str, bs4_class) -> List[RawItem]:
    """
    从 HTML 页面中按 CSS selector 提取新闻条目链接。

    参数：
      html_text    —— 页面 HTML 文本
      base_url     —— 页面自身 URL，用于把相对链接补全为绝对链接
      selector     —— CSS 选择器，圈定包含链接的容器元素（如 "article"）
      source_name  —— 归属来源名
      topic_group  —— 归属分组
      bs4_class    —— 调用方传入的 BeautifulSoup 类本体（依赖注入，
                       本模块不 import bs4，避免强制依赖）

    每个匹配到 selector 的元素内取第一个 <a> 标签的文本作为标题、
    href 作为链接；相对路径会用 base_url 的 scheme+netloc 补全为绝对
    路径。最多处理前 20 个匹配元素，防止个别页面结构异常导致爬取
    结果爆炸。标题或链接为空的元素会被跳过。
    """
    if not html_text:
        return []
    soup = bs4_class(html_text, "html.parser")
    items: List[RawItem] = []
    for el in soup.select(selector)[:20]:
        a = el.find("a")
        if not a:
            continue
        title = a.get_text(strip=True)
        href = a.get("href", "")
        if href and not href.startswith("http"):
            base = urllib.parse.urlparse(base_url)
            href = f"{base.scheme}://{base.netloc}{href}"
        if title and href:
            items.append(RawItem(
                source_type="webpage", source_name=source_name,
                topic_group=topic_group, title=title,
                url=href, summary="", published_at=None,
            ))
    return items
