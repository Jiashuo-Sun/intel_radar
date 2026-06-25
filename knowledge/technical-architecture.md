# Intel Radar — 技术架构文档

## 系统架构

```
┌─────────────────────────────────────────────────────┐
│                   Intel Radar                        │
│                                                      │
│  ┌──────────┐   ┌──────────┐   ┌──────────────────┐ │
│  │  采集层   │ → │  处理层   │ → │    输出层         │ │
│  │ Fetcher  │   │Processor │   │  Report Writer   │ │
│  └──────────┘   └──────────┘   └──────────────────┘ │
│       ↕               ↕                              │
│  ┌──────────┐   ┌──────────┐                        │
│  │  配置中心 │   │  本地DB   │                        │
│  │  YAML    │   │  SQLite  │                        │
│  └──────────┘   └──────────┘                        │
└─────────────────────────────────────────────────────┘
```

## 目录结构

```
intel-radar/
├── CLAUDE.md                 ← 项目总设计文档
├── run.py                    ← 入口
├── config/
│   ├── watch.yaml            ← 监控对象配置
│   └── settings.yaml         ← 运行参数
├── src/
│   ├── fetcher.py            ← 采集层
│   ├── processor.py          ← 处理层（去重 + 打分 + AI摘要）
│   ├── reporter.py           ← 输出层（生成Markdown）
│   └── database.py           ← SQLite封装
├── knowledge/                ← 设计与运维文档
├── output/                   ← 每日日报（不提交到git）
├── data/                     ← SQLite数据库（不提交到git）
└── logs/                     ← 运行日志（不提交到git）
```

---

## 关键质量保证

| 问题域 | 措施 |
|--------|------|
| 稳定性 | `_fetch_url()` retry=2 + 指数退避（2s/4s）|
| 原子性 | reporter 写 `.tmp` → `os.replace()` 避免残缺文件 |
| 错误记录 | `run_once()` try/finally 确保 `db.log_run()` 始终写入 |
| 鲁棒性 | `_parse_ai_json()` fence 解析 + regex 兜底 |
| 路径安全 | `Database.__init__` 先 `os.path.abspath()` |
| 中文去重 | `_simhash()` 追加 CJK 字符级 tokenize |

---

## 采集层（`src/fetcher.py`）

### 数据结构

```python
@dataclass
class RawItem:
    source_type: str     # "rss" | "webpage" | "arxiv"
    source_name: str     # 人类可读名称
    topic_group: str     # watch.yaml 中的分组 key
    title: str
    url: str
    summary: str = ""
    published_at: Optional[str] = None
```

### 采集器类型

| 类 | 数据源 | 说明 |
|----|--------|------|
| `RssFetcher` | Google News RSS / 公司RSS | 最稳定，无需JS渲染 |
| `ArxivFetcher` | arXiv API | 按关键词获取最新论文 |
| `WebFetcher` | 公司官网新闻页 | requests + BeautifulSoup，需bs4 |

### Google News RSS URL 构造

```
https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans
```

中文查询用 `zh` locale，英文查询用 `en` locale，参数来自 `settings.yaml`。

### arXiv API

```
http://export.arxiv.org/api/query?search_query=all:{query}&sortBy=submittedDate&max_results=10
```

---

## 处理层（`src/processor.py`）

### 三阶段过滤流程

```
RawItem
  │
  ├─[Stage 1]─ URL 精确去重（URL已在DB中 → 已经进过日报）→ url_deduped++
  │
  ├─[Stage 2]─ 打分过滤（score < min_score → 不进日报）→ score_filtered++
  │             注意：score=0 的条目不存入DB（避免污染去重索引）
  │
  ├─[Stage 3]─ SimHash 近似去重（标题相似度过高）→ simhash_deduped++
  │
  └─[Pass]──── 存入DB + 进入 ProcessedItem 列表 → AI摘要 → 日报
```

**关键设计决策**：score=0 的条目**不保存到DB**。原因：
- 保存会导致下次运行时 URL 命中 "已见过"，产生误导性的大量 dedup 计数
- score=0 意味着与业务无关，即便第二天被重新评分仍然是0，重新评分成本极低

### 打分规则（SCORE_RULES）

规则列表见 `src/processor.py`。关键权重：
- 融资/中标/并购：+3（最高权重）
- 竞品公司名：+2 到 +3
- 延峰：+3
- 产品发布：+2
- 行业关键词：+1

### SimHash 去重

使用字节级 SimHash（64位），Hamming 距离 ≤ 3 认为重复。
只与 DB 中 `dedup_window_days`（默认7天）内的条目比较。

### AI 摘要

支持两个 provider，通过 `settings.yaml` 的 `ai.provider` 字段切换：

| provider | 接口 | 认证 |
|----------|------|------|
| `anthropic` | `https://api.anthropic.com/v1/messages` | `ANTHROPIC_API_KEY` 环境变量 |
| `lmstudio` | `http://localhost:1234/v1/chat/completions` | Bearer lm-studio（无需真实key） |

摘要 prompt 要求输出结构化 JSON：
```json
{"summary": "≤25字核心内容", "impact": "威胁/机会/中性 + 影响说明", "action": "建议行动"}
```

---

## 数据库（`src/database.py`）

### 表结构

```sql
CREATE TABLE items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    url_hash            TEXT UNIQUE,        -- MD5(url)，去重主键
    title_hash          INTEGER,            -- SimHash(title)
    title               TEXT,
    url                 TEXT,
    source_name         TEXT,
    topic_group         TEXT,
    score               INTEGER DEFAULT 0,
    summary_ai          TEXT,
    impact              TEXT,
    published_at        TEXT,
    fetched_at          TEXT,
    included_in_report  TEXT                -- 哪天日报用了它（YYYY-MM-DD）
);

CREATE TABLE runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT,
    items_fetched   INTEGER DEFAULT 0,
    items_new       INTEGER DEFAULT 0,
    items_deduped   INTEGER DEFAULT 0,
    report_path     TEXT,
    error           TEXT
);
```

### 关键方法

| 方法 | 说明 |
|------|------|
| `is_url_duplicate(url)` | URL MD5 精确匹配 |
| `is_simhash_duplicate(title, days, threshold)` | SimHash 近似匹配 |
| `save_item(...)` | 只保存 score >= min_score 的条目 |
| `update_ai(url, summary_ai, impact)` | AI摘要写回DB |
| `mark_reported(url, date)` | 标记已出现在日报中 |
| `clear()` | 清空DB（维护/测试用） |

---

## 输出层（`src/reporter.py`）

按 `ProcessedItem.priority` 分组：
- `high`（score ≥ 4）→ 详细展开，含 AI 摘要
- `medium`（score 2-3）→ 竞品动态 / 行业动态，简要列表
- `low`（score 1）→ 行业动态，简要列表
- arXiv 条目 → 独立技术前沿区块

---

## 配置文件

### `config/settings.yaml` 关键字段

```yaml
processor:
  dedup_window_days: 7        # 去重时间窗口（天）
  simhash_threshold: 3        # SimHash Hamming距离阈值（越小越严格）
  min_score_for_report: 1     # 进入日报的最低分
  high_priority_threshold: 4  # 高优先级分数线

ai:
  enabled: true
  provider: "anthropic"       # "anthropic" | "lmstudio"
  summarize_threshold: 1      # AI摘要触发分数（应 = min_score_for_report）
  max_items_per_run: 15
```

### `config/watch.yaml` 结构

```yaml
premium:            # 精品公司监控列表
  - name: ...
    sources: [...]
    priority: critical | high | normal

topics:             # 行业关键词监控
  drawing_ai:
    queries:
      zh: [...]
      en: [...]
    arxiv: [...]
```
