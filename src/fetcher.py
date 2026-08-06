"""
fetcher.py — 多源采集编排层（乐高积木：采集编排）

═══════════════════════════════════════════════════════════════════════
功能作用
═══════════════════════════════════════════════════════════════════════
本文件不再自己处理 HTTP 细节或 XML/HTML 解析——那些原子能力已下沉到
http_client.py（网络）和 feed_parser.py（解析）。本文件的职责收窄为
"编排"：根据来源类型选择对应的 http_client + feed_parser 组合，把
结果统一包装成 RawItem 列表返回给上层。

三个具体采集器（RssFetcher / ArxivFetcher / WebFetcher）职责单一，
互不调用，只是"网络+解析"两个原子能力的不同组合方式：
  - RssFetcher   = http_client.fetch_url + feed_parser.parse_rss_atom
  - ArxivFetcher = http_client.fetch_url + feed_parser.parse_arxiv
  - WebFetcher   = http_client.fetch_url + feed_parser.parse_html_links

FetcherOrchestrator 是最上层的编排器，读取 watch.yaml 配置，依次调用
三个采集器，汇总成一个 RawItem 列表交给 processor.py。

═══════════════════════════════════════════════════════════════════════
输入 / 输出
═══════════════════════════════════════════════════════════════════════
FetcherOrchestrator(watch_cfg, settings).fetch_all() -> List[RawItem]
  输入：
    watch_cfg —— config/watch.yaml 解析后的 dict（premium + topics）
    settings  —— config/settings.yaml 解析后的 dict（读取 settings["fetcher"]
                  子配置：timeout_seconds / user_agent / google_news_locale）
  输出：所有来源合并后的 RawItem 列表（未去重、未打分，这些是 processor
        的职责，fetcher 层不做任何过滤判断）

═══════════════════════════════════════════════════════════════════════
限制与边界
═══════════════════════════════════════════════════════════════════════
- fetch_all() 是同步顺序执行，每次请求之间插入 time.sleep() 礼貌延迟
  （1.0~1.5 秒），避免对 Google News / 公司官网造成突发流量。这意味着
  监控项越多，一次完整采集耗时越长（几十个关键词 × 1.5s ≈ 数十秒到
  几分钟属正常范围）。如需并发加速，需要引入线程池/异步 IO，但要
  相应调整礼貌延迟策略以免触发目标站点的限流。
- WebFetcher 依赖可选的 beautifulsoup4 包，未安装时自动降级为
  "返回空列表"而不是报错（fetch_all 仍会正常完成，只是 webpage 类型
  的来源采集不到内容）。
- 单个来源采集失败（网络错误、XML 解析失败等）只影响该来源的结果为
  空列表，不会中断 fetch_all() 对其余来源的采集——这是刻意设计：一个
  公司官网挂了不应该导致当天全部情报采集失败。
- 不做去重、不做打分、不做内容过滤，这些统一交给 processor.py，保持
  "采集层只管拿数据"的单一职责。
"""
import time
import logging
import urllib.parse
from typing import List

from .models import RawItem
from .http_client import fetch_url
from .feed_parser import parse_rss_atom, parse_arxiv, parse_html_links

log = logging.getLogger(__name__)

# 重新导出 RawItem，保持对"from .fetcher import RawItem"这类旧引用路径的兼容。
__all__ = ["RawItem", "RssFetcher", "ArxivFetcher", "WebFetcher", "FetcherOrchestrator"]


class RssFetcher:
    """
    RSS/Atom Feed 采集器，包括 Google News RSS 搜索结果。
    组合方式：http_client.fetch_url() 拿文本 → feed_parser.parse_rss_atom() 解析。
    """
    GNEWS_BASE = "https://news.google.com/rss/search"

    def __init__(self, settings: dict):
        self.timeout = settings.get("timeout_seconds", 15)
        self.ua = settings.get("user_agent", "IntelRadar/1.0")
        self.retries = settings.get("retry", 2)
        self.locales = settings.get("google_news_locale", {
            "zh": {"hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
            "en": {"hl": "en-US", "gl": "US", "ceid": "US:en"},
        })

    def google_news_url(self, query: str, lang: str = "zh") -> str:
        """按语言 locale 拼接 Google News RSS 搜索 URL。"""
        loc = self.locales.get(lang, self.locales["en"])
        params = {"q": query, **loc}
        return self.GNEWS_BASE + "?" + urllib.parse.urlencode(params)

    def fetch_url(self, feed_url: str, source_name: str, topic_group: str) -> List[RawItem]:
        """直接拉取任意 RSS/Atom feed_url 并解析，用于公司自有 RSS 源。"""
        text = fetch_url(feed_url, self.timeout, self.ua, self.retries)
        return parse_rss_atom(text, source_name, topic_group)

    def fetch_query(self, query: str, lang: str, source_name: str, topic_group: str) -> List[RawItem]:
        """按关键词构造 Google News RSS URL 并拉取解析，用于行业监控关键词广播。"""
        url = self.google_news_url(query, lang)
        return self.fetch_url(url, source_name, topic_group)


class ArxivFetcher:
    """
    arXiv API 采集器，用于技术前沿论文监控。
    组合方式：http_client.fetch_url() 拿文本 → feed_parser.parse_arxiv() 解析。
    """
    BASE = "http://export.arxiv.org/api/query"

    def __init__(self, settings: dict):
        self.timeout = settings.get("timeout_seconds", 15)
        self.ua = settings.get("user_agent", "IntelRadar/1.0")
        self.retries = settings.get("retry", 2)

    def fetch(self, query: str, max_results: int = 8) -> List[RawItem]:
        """按关键词查询 arXiv，返回最新提交的最多 max_results 篇论文。"""
        params = {
            "search_query": f"all:{query}",
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": str(max_results),
        }
        url = self.BASE + "?" + urllib.parse.urlencode(params)
        text = fetch_url(url, self.timeout, self.ua, self.retries)
        return parse_arxiv(text)


class WebFetcher:
    """
    公司官网新闻页采集器（可选，依赖 beautifulsoup4）。
    组合方式：http_client.fetch_url() 拿文本 → feed_parser.parse_html_links() 解析。
    未安装 bs4 时自动降级为空结果，不影响其余采集器正常工作。
    """
    def __init__(self, settings: dict):
        self.timeout = settings.get("timeout_seconds", 15)
        self.ua = settings.get("user_agent", "IntelRadar/1.0")
        self.retries = settings.get("retry", 2)
        try:
            from bs4 import BeautifulSoup
            self.bs4 = BeautifulSoup
        except ImportError:
            self.bs4 = None
            log.info("beautifulsoup4 not installed — WebFetcher disabled")

    def fetch(self, url: str, selector: str, source_name: str, topic_group: str) -> List[RawItem]:
        """抓取 url 页面并按 CSS selector 提取新闻链接列表。bs4 不可用时返回空列表。"""
        if not self.bs4:
            return []
        text = fetch_url(url, self.timeout, self.ua, self.retries)
        return parse_html_links(text, url, selector, source_name, topic_group, self.bs4)


class FetcherOrchestrator:
    """
    根据 watch.yaml 配置，依次调用 RssFetcher / ArxivFetcher / WebFetcher，
    合并所有来源的 RawItem 列表返回。是 pipeline 中"采集"阶段的唯一入口。
    """
    def __init__(self, watch_cfg: dict, settings: dict):
        self.watch = watch_cfg
        self.settings = settings
        fetcher_cfg = settings.get("fetcher", {})
        self.rss = RssFetcher(fetcher_cfg)
        self.arxiv = ArxivFetcher(fetcher_cfg)
        self.web = WebFetcher(fetcher_cfg)

    def fetch_all(self) -> List[RawItem]:
        """
        执行完整一轮采集：先精品监控（premium），再行业关键词监控（topics）。
        返回所有来源合并后的 RawItem 列表，未做任何去重/打分处理。
        """
        results: List[RawItem] = []

        # 1. 精品监控：逐个公司、逐个配置的 source 采集
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
                time.sleep(1.5)  # 礼貌延迟，避免对目标站点造成突发流量

        # 2. 行业监控：按分组遍历关键词，逐个查询 Google News + arXiv
        for group_key, group in self.watch.get("topics", {}).items():
            queries = group.get("queries", {})

            for lang, qlist in queries.items():
                if not isinstance(qlist, list):
                    continue
                for q in qlist:
                    items = self.rss.fetch_query(q, lang, f"Google News / {q[:20]}", group_key)
                    results.extend(items)
                    log.info(f"[topic/{group_key}] [{lang}] '{q}': {len(items)} items")
                    time.sleep(1.5)

            for q in group.get("arxiv", []):
                items = self.arxiv.fetch(q)
                results.extend(items)
                log.info(f"[arxiv] '{q}': {len(items)} items")
                time.sleep(1.0)

        log.info(f"Total raw items fetched: {len(results)}")
        return results
