"""
fetcher.py — 多源采集层
统一返回 List[RawItem]，上层无感知来源类型。
"""
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
import logging

log = logging.getLogger(__name__)


@dataclass
class RawItem:
    source_type: str          # "rss" | "webpage" | "arxiv"
    source_name: str          # 人类可读名称
    topic_group: str          # 来自 watch.yaml 的分组
    title: str
    url: str
    summary: str = ""
    published_at: Optional[str] = None


def _fetch_url(url: str, timeout: int = 15, ua: str = "IntelRadar/1.0") -> str:
    """GET 请求，返回文本内容"""
    # 确保 URL 中的非 ASCII 字符被正确编码
    parsed = urllib.parse.urlparse(url)
    encoded = parsed._replace(
        path=urllib.parse.quote(parsed.path, safe="/:@!$&'()*+,;="),
        query=urllib.parse.quote(parsed.query, safe="=&+%:")
    )
    safe_url = urllib.parse.urlunparse(encoded)
    req = urllib.request.Request(safe_url, headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except Exception as e:
        log.warning(f"Fetch failed: {safe_url} — {e}")
        return ""


def _parse_rss_date(s: str) -> Optional[str]:
    """尝试解析 RSS 日期格式"""
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s.strip(), fmt).isoformat()
        except Exception:
            pass
    return s[:19] if s else None


class RssFetcher:
    """
    抓取 RSS/Atom Feed，包括 Google News RSS。
    """
    GNEWS_BASE = "https://news.google.com/rss/search"

    def __init__(self, settings: dict):
        self.timeout = settings.get("timeout_seconds", 15)
        self.ua = settings.get("user_agent", "IntelRadar/1.0")
        self.locales = settings.get("google_news_locale", {
            "zh": {"hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
            "en": {"hl": "en-US", "gl": "US", "ceid": "US:en"},
        })

    def google_news_url(self, query: str, lang: str = "zh") -> str:
        loc = self.locales.get(lang, self.locales["en"])
        params = {"q": query, **loc}
        return self.GNEWS_BASE + "?" + urllib.parse.urlencode(params)

    def fetch_url(self, feed_url: str, source_name: str, topic_group: str) -> List[RawItem]:
        text = _fetch_url(feed_url, self.timeout, self.ua)
        if not text:
            return []
        return self._parse(text, source_name, topic_group)

    def fetch_query(self, query: str, lang: str, source_name: str, topic_group: str) -> List[RawItem]:
        url = self.google_news_url(query, lang)
        return self.fetch_url(url, source_name, topic_group)

    def _parse(self, xml_text: str, source_name: str, topic_group: str) -> List[RawItem]:
        items = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        # RSS 2.0
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            url   = (item.findtext("link") or "").strip()
            desc  = (item.findtext("description") or "").strip()
            date  = _parse_rss_date(item.findtext("pubDate") or "")
            if title and url:
                items.append(RawItem(
                    source_type="rss", source_name=source_name,
                    topic_group=topic_group, title=title,
                    url=url, summary=desc[:500], published_at=date
                ))
        # Atom
        for entry in root.findall("atom:entry", ns):
            title = (entry.findtext("atom:title", namespaces=ns) or "").strip()
            link  = entry.find("atom:link", ns)
            url   = (link.attrib.get("href", "") if link is not None else "").strip()
            summ  = (entry.findtext("atom:summary", namespaces=ns) or "").strip()
            date  = _parse_rss_date(entry.findtext("atom:updated", namespaces=ns) or "")
            if title and url:
                items.append(RawItem(
                    source_type="rss", source_name=source_name,
                    topic_group=topic_group, title=title,
                    url=url, summary=summ[:500], published_at=date
                ))
        return items


class ArxivFetcher:
    """
    arXiv API 采集最新论文。
    """
    BASE = "http://export.arxiv.org/api/query"

    def __init__(self, settings: dict):
        self.timeout = settings.get("timeout_seconds", 15)
        self.ua = settings.get("user_agent", "IntelRadar/1.0")

    def fetch(self, query: str, max_results: int = 8) -> List[RawItem]:
        params = {
            "search_query": f"all:{query}",
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": str(max_results),
        }
        url = self.BASE + "?" + urllib.parse.urlencode(params)
        text = _fetch_url(url, self.timeout, self.ua)
        if not text:
            return []
        return self._parse(text)

    def _parse(self, xml_text: str) -> List[RawItem]:
        items = []
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []
        for entry in root.findall("atom:entry", ns):
            title   = (entry.findtext("atom:title", namespaces=ns) or "").replace("\n", " ").strip()
            url_el  = entry.find("atom:id", ns)
            url     = (url_el.text or "").strip() if url_el is not None else ""
            summary = (entry.findtext("atom:summary", namespaces=ns) or "").replace("\n", " ").strip()
            date    = _parse_rss_date(entry.findtext("atom:published", namespaces=ns) or "")
            if title and url:
                items.append(RawItem(
                    source_type="arxiv", source_name="arXiv",
                    topic_group="tech_frontier", title=title,
                    url=url, summary=summary[:600], published_at=date
                ))
        return items


class WebFetcher:
    """
    抓取指定公司官网新闻页（BeautifulSoup，可选）。
    没有 bs4 时自动降级跳过。
    """
    def __init__(self, settings: dict):
        self.timeout = settings.get("timeout_seconds", 15)
        self.ua = settings.get("user_agent", "IntelRadar/1.0")
        try:
            from bs4 import BeautifulSoup
            self.bs4 = BeautifulSoup
        except ImportError:
            self.bs4 = None
            log.info("beautifulsoup4 not installed — WebFetcher disabled")

    def fetch(self, url: str, selector: str, source_name: str, topic_group: str) -> List[RawItem]:
        if not self.bs4:
            return []
        text = _fetch_url(url, self.timeout, self.ua)
        if not text:
            return []
        soup = self.bs4(text, "html.parser")
        items = []
        for el in soup.select(selector)[:20]:
            a = el.find("a")
            if not a:
                continue
            title = a.get_text(strip=True)
            href  = a.get("href", "")
            if not href.startswith("http"):
                base = urllib.parse.urlparse(url)
                href = f"{base.scheme}://{base.netloc}{href}"
            if title and href:
                items.append(RawItem(
                    source_type="webpage", source_name=source_name,
                    topic_group=topic_group, title=title,
                    url=href, summary="", published_at=None
                ))
        return items


class FetcherOrchestrator:
    """
    根据 watch.yaml 配置，协调所有采集器，返回合并后的 RawItem 列表。
    """
    def __init__(self, watch_cfg: dict, settings: dict):
        self.watch = watch_cfg
        self.settings = settings
        self.rss = RssFetcher(settings.get("fetcher", {}))
        self.arxiv = ArxivFetcher(settings.get("fetcher", {}))
        self.web = WebFetcher(settings.get("fetcher", {}))

    def fetch_all(self) -> List[RawItem]:
        results: List[RawItem] = []

        # 1. 精品监控
        for company in self.watch.get("premium", []):
            name = company["name"]
            for src in company.get("sources", []):
                stype = src.get("type", "rss")
                if stype == "rss":
                    items = self.rss.fetch_url(src["url"], name, "premium")
                    results.extend(items)
                    log.info(f"[premium/rss] {name}: {len(items)} items")
                elif stype == "webpage":
                    items = self.web.fetch(src["url"], src.get("selector", "article"), name, "premium")
                    results.extend(items)
                    log.info(f"[premium/web] {name}: {len(items)} items")
                time.sleep(1.5)   # 礼貌延迟

        # 2. 行业监控关键词
        for group_key, group in self.watch.get("topics", {}).items():
            queries = group.get("queries", {})
            label = group.get("label", group_key)

            for lang, qlist in queries.items():
                if not isinstance(qlist, list):
                    continue
                for q in qlist:
                    items = self.rss.fetch_query(q, lang, f"Google News / {q[:20]}", group_key)
                    results.extend(items)
                    log.info(f"[topic/{group_key}] [{lang}] '{q}': {len(items)} items")
                    time.sleep(1.5)

            # arXiv
            for q in group.get("arxiv", []):
                items = self.arxiv.fetch(q)
                results.extend(items)
                log.info(f"[arxiv] '{q}': {len(items)} items")
                time.sleep(1.0)

        log.info(f"Total raw items fetched: {len(results)}")
        return results
