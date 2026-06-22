# Intel Radar — 行业情报雷达

> 每天自动采集行业新闻、竞品动态、技术前沿，输出结构化 Markdown 日报。

---

## 语言规范 / Language Policy

**所有代码注释、commit message、文档更新、PR 描述，仅允许使用英文或简体中文，不得使用其他语言。**

All code comments, commit messages, documentation updates, and PR descriptions must be written in **English or Simplified Chinese only**. No other languages are permitted.

---

## 一、产品定位

**Intel Radar** 是一个本地运行的行业情报系统，服务于 Drawing AI / 延峰项目的商务与技术团队。

核心价值：**不是给你更多信息，而是每天早上告诉你"今天有什么值得关注的"。**

### 用户场景

| 角色 | 使用方式 | 关注内容 |
|------|----------|----------|
| 商务（小湖） | 每天读日报 `.md` | 竞品融资/发布/中标、延峰动态、客户信号 |
| 技术负责人 | 周报技术专区 | 图像识别模型、OCR新论文、开源工具 |
| 管理层 | 周五周报综述 | 行业趋势、威胁/机会摘要 |

---

## 二、监控对象设计

监控对象分两类，配置在 `config/watch.yaml`。

### 2.1 精品监控（Premium Watch）

对指定公司进行深度跟踪：官网博客、PR 页、公众号。每天至少检查一次，命中即推送。

**当前精品监控列表：**

```
延峰集团 (Yanfeng)        — 官网 + 公众号
志丞科技                   — 官网 + 融资动态
Werk24                     — 官网博客 + LinkedIn
Energent.ai                — 官网 + Twitter/X
High QA                    — 官网新闻页
InspectionXpert / Ideagen  — 官网 + 新闻稿
CoLab Software             — 官网博客
1Factory                   — 官网新闻
DISCUS                     — 官网
```

### 2.2 行业监控（Topic Watch）

关键词广撒网，通过 Google News RSS 和 arXiv 订阅，按打分过滤。

**关键词分组：**

```yaml
drawing_ai:         # Drawing AI 核心场景
  - "工业图纸 AI 识别"
  - "工程图纸 智能处理"
  - "engineering drawing OCR"
  - "CAD drawing extraction AI"
  - "automated ballooning FAI"
  - "GD&T extraction software"

yanfeng_ecosystem:  # 延峰生态关注
  - "延峰 数字化"
  - "Yanfeng automotive AI"
  - "汽车内饰 智能制造"
  - "IATF 16949 数字化"
  - "PPAP automation"

tech_frontier:      # 技术前沿（模型/工具）
  - "document understanding model 2025"
  - "multimodal OCR industrial"
  - "vision language model manufacturing"
  - "HuggingFace document AI"
  - "PaddleOCR RapidOCR"

market_signals:     # 市场信号（招标/政策）
  - "工业数字化 招标 2025"
  - "制造业 AI 中标"
  - "智能制造 政府采购"
```

---

## 三、系统架构

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

### 目录结构

```
intel-radar/
├── CLAUDE.md              ← 本文件，项目设计文档
├── run.py                 ← 入口，`python run.py` 即可
├── config/
│   ├── watch.yaml         ← 监控对象配置（关键词、公司、源）
│   └── settings.yaml      ← 运行参数（调度、API密钥、输出路径）
├── src/
│   ├── fetcher.py         ← 采集层：RSS/网页/arXiv
│   ├── processor.py       ← 处理层：去重、打分、AI摘要
│   ├── reporter.py        ← 输出层：生成 Markdown 日报
│   ├── database.py        ← SQLite 封装
│   └── notifier.py        ← 可选：邮件/企微推送
├── output/
│   └── YYYY-MM-DD.md      ← 每日情报日报
└── logs/
    └── radar.log
```

---

## 四、数据流详解

### 4.1 采集层（`src/fetcher.py`）

每个数据源对应一个 Fetcher 类，统一返回 `List[RawItem]`：

```python
@dataclass
class RawItem:
    source_type: str      # "rss" | "webpage" | "arxiv"
    source_name: str      # "Google News / Werk24官网 / arXiv"
    title: str
    url: str
    summary: str          # 原始摘要（可能为空）
    published_at: datetime
    raw_html: str         # 网页爬虫时保留，用于后续提取
```

**支持的采集器：**

| 采集器类 | 数据源 | 说明 |
|---------|--------|------|
| `RssFetcher` | Google News RSS | 关键词订阅，最稳定 |
| `WebFetcher` | 公司官网博客/新闻页 | requests + BeautifulSoup |
| `ArxivFetcher` | arXiv API | 技术论文监控 |
| `PlaywrightFetcher` | JS渲染页面 | 按需启用，处理动态内容 |

### 4.2 处理层（`src/processor.py`）

```
RawItem → 去重检查 → 关键词过滤 → 打分 → AI摘要 → ProcessedItem
```

**去重策略（双重保障）：**
- URL 精确匹配（MD5 哈希存 DB）
- 标题相似度（SimHash，Hamming 距离 < 3 视为重复）

**打分规则（0-10分）：**

```python
SCORE_RULES = {
    # 信号词（高优先级事件）
    "融资|募资|完成.*轮":     +3,
    "发布|上线|launch|release": +2,
    "中标|签约|合同":          +3,
    "收购|并购|acquire":       +3,
    "合作|partnership":        +1,

    # 竞品精确命中
    "Werk24|Energent|CoLab":   +3,
    "延峰|Yanfeng":             +3,
    "志丞":                     +2,

    # 技术信号
    "arXiv|论文|paper":        +1,
    "开源|open source":        +1,
}
# 分数 >= 4 → 高优先级（日报置顶）
# 分数 1-3  → 普通条目
# 分数 0    → 归档，不出现在日报正文
```

**AI 摘要（可选）：**

支持两种 AI 后端，通过 `settings.yaml` 中 `ai.provider` 字段切换：

| provider | 后端 | 条件 |
|----------|------|------|
| `anthropic` | Anthropic 云端 API | 需设置 `ANTHROPIC_API_KEY` 环境变量 |
| `lmstudio` | LM Studio 本地推理 | 需本地启动 LM Studio 并开启服务器 |

对每条高分条目生成：
1. 一句话核心内容（≤25字）
2. 对 Drawing AI / 延峰项目的影响判断（威胁/机会/中性）
3. 建议行动（≤20字）

输出格式为 JSON：`{"summary": "...", "impact": "...", "action": "..."}`

### 4.3 输出层（`src/reporter.py`）

每天生成 `output/YYYY-MM-DD.md`，结构如下：

---

## 五、日报格式规范

每日输出文件：`output/YYYY-MM-DD.md`

````markdown
# 情报日报 · 2025-01-15

> 采集时间：06:30 | 新增条目：47 | 高优先级：3 | 去重过滤：12

---

## 🔴 高优先级（今日必读）

### [Werk24 完成 800 万欧元 A 轮融资](https://werk24.ai/news/...)
**来源：** TechCrunch EU · 2025-01-15
**评分：** 8/10 · 信号词：融资、工业图纸AI
**AI 摘要：** Werk24 获得 A 轮，主投方为德国工业基金 XYZ，将用于扩展亚太市场销售团队。
**影响判断：** ⚠️ 直接竞品获得资金，亚太攻势预计6个月内启动，需加快延峰 POC 推进节奏。
**建议行动：** 本周约延峰会议，强调我方本地化和离线优势。

---

## 🟡 行业动态

### [PaddleOCR 3.0 正式发布，工业图像识别准确率提升 15%](https://...)
**来源：** arXiv · 2025-01-15 · `tech_frontier`
**AI 摘要：** 百度开源新版 OCR，专项优化工业复杂背景场景，可作为 Drawing AI 底层引擎备选。

### [工信部发布 2025 年智能制造示范工厂名单](https://...)
**来源：** Google News · `market_signals`
**摘要：** 名单中含 3 家延峰体系供应商，或有数字化采购预算释放。

---

## 🔵 竞品动态

| 公司 | 内容摘要 | 来源 | 日期 |
|------|----------|------|------|
| High QA | 新增 SolidWorks 插件集成 | 官网博客 | 01-15 |
| CoLab | 发布 Q4 产品路线图 | 官网 | 01-14 |

---

## 📚 技术前沿（arXiv）

| 论文 | 核心方法 | 与我们的关联 |
|------|----------|-------------|
| [IndustrialDraw-LLM: ...](https://arxiv.org/...) | 多模态微调工业图纸 | 直接相关，值得复现 |
| [TableFormer: ...](https://arxiv.org/...) | 表格结构识别 | 可用于标题栏提取 |

---

## 📦 归档（低相关，略）

本日共 18 条低相关条目已归档，可在数据库中查询。

---
*Intel Radar v1.0 · 下次运行：明日 06:30*
````

---

## 六、配置文件规范

### `config/watch.yaml`

```yaml
# ============================================================
# 精品监控：指定公司深度跟踪
# ============================================================
premium:
  - name: "延峰集团"
    aliases: ["Yanfeng", "YF", "延锋"]
    sources:
      - type: webpage
        url: "https://www.yanfeng.com/news"
        selector: "article.news-item"   # CSS选择器
      - type: rss
        url: "https://www.yanfeng.com/feed"
    keywords: ["AI", "数字化", "图纸", "质检", "制造"]
    priority: critical   # critical | high | normal

  - name: "Werk24"
    aliases: ["werk24.ai"]
    sources:
      - type: webpage
        url: "https://werk24.ai/blog"
        selector: "article"
      - type: rss
        url: "https://news.google.com/rss/search?q=Werk24"
    priority: critical

  - name: "志丞科技"
    aliases: ["zhicheng", "志丞"]
    sources:
      - type: rss
        url: "https://news.google.com/rss/search?q=志丞科技+图纸"
    priority: high

# ============================================================
# 行业监控：关键词广播
# ============================================================
topics:
  drawing_ai:
    label: "图纸AI核心"
    color: red
    queries:
      zh:
        - "工业图纸 AI 识别"
        - "工程图纸 智能处理"
        - "CAD图纸 数字化 2025"
      en:
        - "engineering drawing OCR AI"
        - "automated ballooning FAI software"
        - "GD&T extraction machine learning"
        - "technical drawing understanding LLM"

  yanfeng_ecosystem:
    label: "延峰生态"
    color: orange
    queries:
      zh:
        - "延峰 数字化"
        - "延峰 AI"
        - "汽车内饰 智能制造 2025"
        - "IATF16949 数字化工具"
      en:
        - "Yanfeng automotive digitalization"
        - "automotive Tier1 quality AI"
        - "PPAP automation software"

  tech_frontier:
    label: "技术前沿"
    color: blue
    queries:
      en:
        - "document understanding model 2025"
        - "multimodal OCR industrial"
        - "vision language model manufacturing"
    arxiv:
      - "engineering drawing extraction"
      - "technical drawing deep learning"
      - "industrial document understanding"

  market_signals:
    label: "市场信号"
    color: green
    queries:
      zh:
        - "工业数字化 招标 2025"
        - "制造业 AI 中标"
        - "智能制造 政府采购"
        - "汽车零部件 数字化 解决方案"
```

### `config/settings.yaml`

```yaml
# ============================================================
# 运行参数
# ============================================================
schedule:
  run_at: "06:30"           # 每天几点跑
  lookback_days: 2          # 回看几天（避免漏掉昨天的）
  timezone: "Asia/Shanghai"

output:
  dir: "./output"
  filename_format: "%Y-%m-%d.md"
  keep_days: 90             # 本地保留多少天

database:
  path: "./data/radar.db"

fetcher:
  timeout_seconds: 15
  retry: 2
  user_agent: "IntelRadar/1.0 (internal monitoring tool)"
  google_news_locale:
    zh: { hl: "zh-CN", gl: "CN", ceid: "CN:zh-Hans" }
    en: { hl: "en-US", gl: "US", ceid: "US:en" }

processor:
  dedup_window_days: 7      # 7天内相同内容视为重复
  simhash_threshold: 3      # Hamming距离阈值
  min_score_for_report: 1   # 低于此分不出现在日报
  high_priority_threshold: 4

# AI 摘要（可选）
# provider 字段控制使用哪个后端："anthropic" | "lmstudio"
ai:
  enabled: true
  provider: "anthropic"        # 切换为 "lmstudio" 即可使用本地模型
  summarize_threshold: 3       # 分数 >= 此值才调用 AI 摘要
  max_items_per_run: 20        # 每次最多摘要多少条（控制成本/耗时）

  # Anthropic 云端 API（provider: "anthropic" 时生效）
  anthropic:
    model: "claude-haiku-4-5-20251001"
    api_key_env: "ANTHROPIC_API_KEY"   # 从环境变量读，不写明文

  # LM Studio 本地推理（provider: "lmstudio" 时生效）
  lmstudio:
    base_url: "http://localhost:1234/v1"
    model: "local-model"               # 填写 LM Studio 中已加载模型的名称
    timeout_seconds: 60
```

---

## 七、核心模块说明

### `src/database.py`

SQLite 表结构：

```sql
-- 已采集条目（去重主表）
CREATE TABLE items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url_hash    TEXT UNIQUE,          -- MD5(url)
    title_hash  TEXT,                 -- SimHash(title)
    title       TEXT,
    url         TEXT,
    source_name TEXT,
    topic_group TEXT,
    score       INTEGER DEFAULT 0,
    summary_ai  TEXT,                 -- AI生成摘要
    impact      TEXT,                 -- AI生成影响判断
    published_at DATETIME,
    fetched_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    included_in_report DATE           -- 哪天的日报用了它
);

-- 采集运行日志
CREATE TABLE runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      DATETIME,
    items_fetched INTEGER,
    items_new     INTEGER,
    items_deduped INTEGER,
    report_path   TEXT,
    error         TEXT
);
```

### `src/fetcher.py`

关键实现点：
- Google News RSS URL 构造：`https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans`
- arXiv API：`http://export.arxiv.org/api/query?search_query=all:{query}&sortBy=submittedDate&max_results=10`
- 网页爬虫：requests + BeautifulSoup，CSS selector 从 `watch.yaml` 读取
- 所有 fetcher 统一返回 `List[RawItem]`，processor 无感知来源

### `src/processor.py`

三阶段过滤，各阶段独立计数，方便诊断：

```python
# Stage 1: URL 精确去重（已进过日报的条目）
if db.is_url_duplicate(url): url_deduped++; continue

# Stage 2: 打分过滤（评分=0 不存DB，避免污染去重索引）
score = calculate_score(item)
if score < min_score: score_filtered++; continue

# Stage 3: SimHash 近似去重（标题相似度高）
if db.is_simhash_duplicate(title): simhash_deduped++; continue

# 通过全部过滤 → 存DB + 进日报 + AI摘要
```

**关键约束**：score < min_score 的条目**不保存到DB**。保存会导致下次运行时 URL 命中去重，产生大量误导性的 "deduped" 计数。

### `src/reporter.py`

按分数分组，写入 Markdown：
- **分数 >= `high_priority_threshold`（默认4）** → 🔴 高优先级，详细展开，含 AI 摘要
- **分数 2-3** → 🟡 行业动态 / 🔵 竞品动态，简要列表
- **arXiv 条目** → 📚 技术前沿专区
- **分数 1** → 归档，日报末尾一行说明

---

## 八、运行方式

### 安装

```bash
cd intel-radar
pip install -r requirements.txt
cp config/settings.yaml.example config/settings.yaml
# 编辑 config/watch.yaml 和 config/settings.yaml
export ANTHROPIC_API_KEY="your-key"  # 可选，用于AI摘要
```

### 手动运行

```bash
python run.py              # 立即运行一次，生成今日日报
python run.py --date 2025-01-14   # 补跑指定日期
python run.py --dry-run    # 只采集，不写DB，用于测试
python run.py --no-ai      # 跳过AI摘要（节省费用/无key时）
```

### 自动调度（定时跑）

```bash
# 方案A：crontab（推荐）
crontab -e
30 6 * * * cd /path/to/intel-radar && python run.py >> logs/cron.log 2>&1

# 方案B：内置调度（进程常驻）
python run.py --daemon
```

### 查看输出

```bash
ls output/                    # 列出所有日报
cat output/2025-01-15.md      # 阅读今日日报
```

---

## 九、扩展路径

| 阶段 | 功能 | 优先级 |
|------|------|--------|
| v1.0 | RSS + 网页爬虫 + 打分 + Markdown日报 | ✅ 当前版本 |
| v1.1 | 邮件推送（SMTP） | 高 |
| v1.2 | 企业微信 webhook 推送高优先级 | 高 |
| v2.0 | Web 看板（Flask，浏览历史日报） | 中 |
| v2.1 | 微信公众号监控（RSSHub桥接） | 中 |
| v2.2 | 招投标平台爬虫 | 中 |
| v3.0 | 向量数据库 + 语义搜索 | 低 |

---

## 十、开发约定

- **语言规范**：所有代码注释、commit message、文档，仅使用英文或简体中文
- **所有秘钥从环境变量读取**，不写入任何配置文件
- **数据全部本地存储**，不上传任何内容到外部服务（除 AI 摘要调用外）
- **新增数据源**：在 `watch.yaml` 添加配置，在 `fetcher.py` 中实现对应 Fetcher 类
- **修改日报格式**：只改 `reporter.py`，不影响其他模块
- **AI 摘要为可选**：`--no-ai` 参数或不设置 API Key 时自动降级跳过
- **AI 提供商切换**：只需修改 `settings.yaml` 中的 `ai.provider` 字段，无需改代码
- **新增 AI 提供商**：在 `processor.py` 中添加 `_call_xxx()` 函数，并在 `ai_summarize_batch()` 中注册
- **幂等性**：同一天重复运行，只追加新条目，不重复写日报
