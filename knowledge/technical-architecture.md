# Intel Radar — 技术架构文档

> **模块级架构（乐高积木式）**：完整的模块依赖图、每个文件的输入/输出/
> 限制说明，见 [`knowledge/lego-modules.md`](./lego-modules.md)。
> 本文档聚焦整体数据流与配置，模块细节不再重复维护，避免两处文档
> 内容漂移不一致。

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

自 2026-08 重构后，采集层/处理层内部进一步拆分为多个高内聚低耦合的
"乐高模块"，各自零依赖或近零依赖，通过 `models.py` 定义的公共数据
结构（`RawItem` / `ProcessedItem`）交换信息。整体流程不变，仅内部
实现细节按职责拆分为独立文件。

## 目录结构

```
intel-radar/
├── CLAUDE.md                 ← 项目总设计文档
├── run.py                    ← 入口 / pipeline 装配层
├── config/
│   ├── watch.yaml            ← 监控对象配置
│   └── settings.yaml         ← 运行参数
├── src/
│   ├── models.py             ← 公共数据结构（RawItem / ProcessedItem）
│   ├── http_client.py        ← 通用 HTTP GET + 重试（零依赖）
│   ├── simhash.py            ← SimHash 近似去重算法（零依赖）
│   ├── feed_parser.py        ← RSS/Atom/arXiv/HTML 纯解析函数
│   ├── scorer.py             ← 关键词打分引擎
│   ├── ai_client.py          ← AI 摘要客户端（provider 可插拔）
│   ├── fetcher.py            ← 采集编排层（组合 http_client+feed_parser）
│   ├── database.py           ← SQLite 封装（依赖 simhash）
│   ├── processor.py          ← 处理层编排（三阶段过滤 + AI摘要触发）
│   └── reporter.py           ← 输出层（生成Markdown，原子写文件）
├── knowledge/                ← 设计与运维文档
│   └── lego-modules.md       ← 每个模块的详细文档（功能/输入/输出/边界）
├── output/                   ← 每日日报（不提交到git）
├── data/                     ← SQLite数据库（不提交到git）
└── logs/                     ← 运行日志（不提交到git）
```

---

## 采集层（`src/fetcher.py` 编排，`src/http_client.py` + `src/feed_parser.py` 实现）

`fetcher.py` 本身不再直接处理网络请求或 XML/HTML 解析——这两部分已
下沉为独立的零依赖模块：`http_client.fetch_url()` 负责"下载文本"
（含重试退避），`feed_parser.parse_xxx()` 负责"解析文本"（纯函数，
可脱离网络单独测试）。每个采集器类只是这两个原子能力的组合方式。

### 数据结构（`src/models.py`）

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

| 类 | 数据源 | 组合方式 | 说明 |
|----|--------|---------|------|
| `RssFetcher` | Google News RSS / 公司RSS | http_client + feed_parser.parse_rss_atom | 最稳定，无需JS渲染 |
| `ArxivFetcher` | arXiv API | http_client + feed_parser.parse_arxiv | 按关键词获取最新论文 |
| `WebFetcher` | 公司官网新闻页 | http_client + feed_parser.parse_html_links | 需 bs4，未安装自动降级为空结果 |

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

## 处理层（`src/processor.py` 编排，`src/scorer.py` + `src/ai_client.py` + `src/database.py` 实现）

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

### 打分规则（SCORE_RULES，独立于 `src/scorer.py`）

规则列表见 `src/scorer.py`。关键权重：
- 融资/中标/并购：+3（最高权重）
- 竞品公司名：+2 到 +3
- 延峰：+3
- 产品发布：+2
- 行业关键词：+1

打分逻辑与去重/AI摘要完全解耦，`calculate_score(item)` 是纯函数，
可独立单元测试。

### SimHash 去重（独立于 `src/simhash.py`）

使用字符级 SimHash（64位，中文按单字切分、英文按空格切分），
Hamming 距离 ≤ 3 认为重复。只与 DB 中 `dedup_window_days`（默认7天）
内的条目比较。算法本身与 SQLite 存储解耦，`simhash()` / `hamming_distance()`
是零依赖纯函数。

### AI 摘要（独立于 `src/ai_client.py`）

支持两个 provider，通过 `settings.yaml` 的 `ai.provider` 字段切换，
provider 路由采用注册表模式（`PROVIDERS` 字典），新增 provider 无需
改动调用方：

| provider | 接口 | 认证 |
|----------|------|------|
| `anthropic` | `https://api.anthropic.com/v1/messages` | `ANTHROPIC_API_KEY` 环境变量 |
| `lmstudio` | `http://localhost:1234/v1/chat/completions` | Bearer lm-studio（无需真实key） |

摘要 prompt 要求输出结构化 JSON：
```json
{"summary": "≤25字核心内容", "impact": "威胁/机会/中性 + 影响说明", "action": "建议行动"}
```

`ai_client.parse_ai_json()` 支持三级兜底解析：markdown 围栏剥离 →
直接 json.loads → regex 提取 `{...}` 块，应对不同模型输出格式不稳定
的情况。

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
