# Intel Radar — 乐高模块文档 / Lego Module Reference

本文档汇总项目内每个源码文件的模块级文档（功能作用 / 输入 / 输出 / 限制与边界），
方便快速了解整体架构，也方便未来其他项目直接复用其中的独立模块。

**设计原则**：高内聚、低耦合。每个模块只做一件事，只通过 `models.py`
定义的公共数据结构（`RawItem` / `ProcessedItem`）与其他模块交换信息，
不直接调用彼此的内部实现细节。`run.py` 是唯一知道"完整流程长什么样"
的装配层，把所有模块像乐高积木一样按顺序拼接起来。

---

## 一、模块依赖关系图

```
                          ┌─────────────┐
                          │  models.py  │  ← 零依赖，公共数据结构
                          │ RawItem /   │    (RawItem, ProcessedItem)
                          │ProcessedItem│
                          └──────┬──────┘
                 ┌───────────────┼───────────────┐
                 ↑               ↑               ↑
          ┌──────────────┐ ┌───────────┐  ┌─────────────┐
          │ feed_parser.py│ │ scorer.py │  │ reporter.py │
          │ (纯解析函数)   │ │(纯打分函数)│  │ (纯渲染函数) │
          └──────┬────────┘ └─────┬─────┘  └──────┬──────┘
                 │                │                │
   ┌─────────────┼──────┐         │                │
   ↑             ↑      │         │                │
┌──────────┐┌───────────┴──┐      │                │
│http_client││  simhash.py  │      │                │
│(零依赖)   ││   (零依赖)    │      │                │
└─────┬────┘└──────┬───────┘      │                │
      │             │              │                │
      ↑             ↑              │                │
┌─────────────┐ ┌──────────┐       │                │
│ fetcher.py  │ │database.py│       │                │
│（编排采集）  │ │（SQLite） │       │                │
└─────┬───────┘ └────┬──────┘      │                │
      │               │            │                │
      │               └───┬────────┘                │
      │                   ↑                          │
      │             ┌───────────────┐   ┌───────────┐│
      │             │  processor.py  │──▶│ai_client.py││
      │             │（三阶段过滤编排）│   │(AI provider││
      │             └───────┬────────┘   │  可插拔)   ││
      │                     │            └───────────┘│
      │                     │                          │
      └─────────────────────┼──────────────────────────┘
                             ↓
                      ┌─────────────┐
                      │   run.py    │  ← 装配层，串联全部模块
                      │ (pipeline)  │    fetcher→processor→reporter
                      └─────────────┘
```

**依赖方向的关键原则**：箭头永远从"编排层"指向"原子层"，原子层
（models / http_client / simhash / feed_parser / scorer）互相之间
零依赖或只依赖 models，绝不反向依赖编排层。这保证任意一个原子模块
都可以被单独抽出复制到别的项目使用，不需要连带拖出一堆无关代码。

---

## 二、Pipeline 执行流程

```
config/watch.yaml ─┐
config/settings.yaml ┤
                     ↓
              ┌──────────────┐
              │  run.py main │
              └──────┬───────┘
                     ↓
        ┌────────────────────────┐
        │ FetcherOrchestrator     │  采集：RSS / arXiv / 官网
        │ .fetch_all()            │  → List[RawItem]
        └────────────┬────────────┘
                     ↓
        ┌────────────────────────┐
        │ Processor.process()     │  三阶段过滤：
        │  Stage1 URL去重(DB)      │  → List[ProcessedItem]
        │  Stage2 打分(scorer)     │  → proc_stats
        │  Stage3 SimHash去重(DB) │
        │  → AI摘要(ai_client)    │
        └────────────┬────────────┘
                     ↓
        ┌────────────────────────┐
        │ Reporter.generate()     │  渲染：Markdown 日报
        │                         │  → output/YYYY-MM-DD.md
        └────────────┬────────────┘
                     ↓
        ┌────────────────────────┐
        │ Database.log_run()      │  记录运行结果（成功/失败均记录）
        └─────────────────────────┘
```

---

## 三、原子层模块（零依赖或近零依赖，可独立复用）

### 3.1 `src/models.py` — 核心数据模型

| 项 | 内容 |
|----|------|
| 功能 | 定义 `RawItem`（采集层原始条目）和 `ProcessedItem`（处理层打分/摘要后条目）两个 dataclass，是全项目唯一的公共数据契约 |
| 依赖 | 无（仅标准库 dataclasses/typing） |
| 输入/输出 | 纯数据结构定义，无 I/O |
| 复用价值 | 可直接复制到任何"采集→处理→输出"类项目做数据契约模板 |
| 边界 | 不含任何业务规则（打分/去重/摘要逻辑不在此），不做字段校验 |

### 3.2 `src/http_client.py` — 通用 HTTP GET 客户端

| 项 | 内容 |
|----|------|
| 功能 | 提供 `fetch_url(url, timeout, ua, retries)`，纯标准库 urllib 实现的 GET 请求，内置指数退避重试 |
| 依赖 | 无（仅标准库 urllib/time/logging） |
| 输入 | URL + 超时/UA/重试次数 |
| 输出 | 成功返回响应文本；失败返回空字符串 `""`（不抛异常，调用方无需 try/except） |
| 复用价值 | 任何需要轻量级 HTTP GET（无需 requests 依赖）的项目均可直接复用 |
| 边界 | 仅支持 GET；退避策略固定为 2s→4s→8s...；不支持 JS 渲染页面 |

### 3.3 `src/simhash.py` — SimHash 近似去重算法

| 项 | 内容 |
|----|------|
| 功能 | 提供 `simhash(text)` 和 `hamming_distance(a, b)`，支持中英文混合文本的 64 位近似相似度指纹 |
| 依赖 | 无（仅标准库 hashlib） |
| 输入 | 任意字符串 |
| 输出 | `simhash()` 返回 64 位有符号整数（兼容 SQLite INTEGER）；`hamming_distance()` 返回汉明距离 |
| 复用价值 | 任何需要短文本近似去重/相似度比较的项目（日志去重、标题聚类等）均可复用 |
| 边界 | 中文按字符切分（非真正分词）；短文本（<4 token）区分度下降；阈值判断由调用方决定 |

### 3.4 `src/feed_parser.py` — RSS/Atom/arXiv/HTML 纯解析层

| 项 | 内容 |
|----|------|
| 功能 | `parse_rss_atom()` / `parse_arxiv()` / `parse_html_links()` 三个纯函数，把已下载的文本解析为 `RawItem` 列表 |
| 依赖 | `models.py`（RawItem） |
| 输入 | XML/HTML 字符串 + 元数据（来源名/分组） |
| 输出 | `List[RawItem]`；解析失败或字段缺失时静默跳过，不抛异常 |
| 复用价值 | 任何 RSS/Atom 聚合类项目可直接复用解析逻辑；不发起网络请求，可脱离网络单独单元测试 |
| 边界 | `parse_html_links` 依赖调用方注入 BeautifulSoup 类（不强制 import bs4）；不做 HTML 转义/XSS 过滤 |

### 3.5 `src/scorer.py` — 关键词打分引擎

| 项 | 内容 |
|----|------|
| 功能 | `calculate_score(item)`，按 `SCORE_RULES` 正则规则表对条目打分（0-10 分） |
| 依赖 | `models.py`（RawItem） |
| 输入 | RawItem（读取 title/summary/topic_group） |
| 输出 | 非负整数分数 |
| 复用价值 | 任何"关键词命中打分"类需求（舆情监控、内容过滤）可参考此模式 |
| 边界 | 规则硬编码在 Python 常量中（非 YAML 配置，见 todo.md P2）；正则子串匹配，不理解语义；同一关键词多次出现只计一次分 |

---

## 四、编排层模块（依赖原子层，负责协调具体业务流程）

### 4.1 `src/ai_client.py` — AI 摘要客户端（可插拔 provider）

| 项 | 内容 |
|----|------|
| 功能 | `summarize_batch(items, ai_cfg)` 批量调用 AI 生成摘要；通过 `PROVIDERS` 注册表支持多后端（当前：`anthropic` / `lmstudio`） |
| 依赖 | 无强依赖（鸭子类型接受任意含 title/summary/source_name 属性的对象） |
| 输入 | ProcessedItem 列表 + ai 配置字典 |
| 输出 | 无返回值，原地写入 `item.summary_ai` / `item.impact`；单条失败只 warning 不中断批次 |
| 扩展方式 | 新增 provider：写 `_call_xxx(text, cfg)` 函数 + 在 `PROVIDERS` 注册一行，无需改动调用方 |
| 边界 | `parse_ai_json()` 的 regex 兜底只处理不含嵌套花括号的单层 JSON；SYSTEM_PROMPT 硬编码业务背景（复用到其他项目需替换） |

### 4.2 `src/fetcher.py` — 多源采集编排层

| 项 | 内容 |
|----|------|
| 功能 | `FetcherOrchestrator.fetch_all()` 读取 watch.yaml 配置，依次调度 `RssFetcher` / `ArxivFetcher` / `WebFetcher` 三个采集器 |
| 依赖 | `models.py` + `http_client.py` + `feed_parser.py` |
| 输入 | watch_cfg（监控对象配置）+ settings（fetcher 运行参数） |
| 输出 | 合并后的 `List[RawItem]`，未去重未打分 |
| 组合关系 | 每个采集器 = `http_client.fetch_url()` 拿文本 + `feed_parser.parse_xxx()` 解析，采集器本身不含网络/解析细节 |
| 边界 | 顺序同步执行，请求间有礼貌延迟（1.0~1.5s）；WebFetcher 依赖可选的 beautifulsoup4；单来源失败不影响其余来源 |

### 4.3 `src/database.py` — SQLite 本地存储层

| 项 | 内容 |
|----|------|
| 功能 | 封装 items 表（已采集条目，去重依据）和 runs 表（运行记录）的全部读写 |
| 依赖 | `simhash.py`（simhash/hamming_distance） |
| 输入/输出 | 见类方法：`is_url_duplicate` / `is_simhash_duplicate` / `save_item` / `update_ai` / `mark_reported` / `log_run` / `clear` |
| 边界 | 每次调用新开连接（无连接池）；`save_item` 用 INSERT OR IGNORE 保证幂等；路径用 `os.path.abspath()` 防止空目录名报错；`clear()` 为破坏性操作仅供维护使用 |

### 4.4 `src/processor.py` — 处理层编排（三阶段过滤）

| 项 | 内容 |
|----|------|
| 功能 | `Processor.process()` 编排三阶段过滤（URL去重→打分→SimHash去重）+ AI摘要触发逻辑 |
| 依赖 | `models.py` + `database.py` + `scorer.py` + `ai_client.py` |
| 输入 | `List[RawItem]` + use_ai 标志 |
| 输出 | `(List[ProcessedItem], proc_stats dict)`，proc_stats 含三阶段各自过滤数量 |
| **关键约束** | score < min_score 的条目**不得**调用 `db.save_item()`，否则会在下次运行时被误判为"已见过"（见 lessons-learned.md Bug #1），修改本文件时必须保持此约束 |
| 边界 | 三阶段顺序不可调换；本文件不感知具体打分规则/AI provider 细节 |

### 4.5 `src/reporter.py` — 输出层（Markdown 渲染）

| 项 | 内容 |
|----|------|
| 功能 | `Reporter.generate()` 把 `List[ProcessedItem]` 渲染为结构化 Markdown 日报 |
| 依赖 | `models.py`（仅依赖数据结构，**不依赖** processor.py，避免输出层耦合处理层实现细节）|
| 输入 | ProcessedItem 列表 + 日期 + 统计字典 |
| 输出 | 生成的文件路径；写入方式为原子写（`.tmp` + `os.replace()`），避免进程中断产生残缺文件 |
| 边界 | `GROUP_META` 硬编码分组展示元数据，与 watch.yaml 存在重复维护风险（见 todo.md P2）；同一天重复运行会整份覆盖旧日报（非追加） |

---

## 五、装配层

### 5.1 `run.py` — Pipeline 装配入口

| 项 | 内容 |
|----|------|
| 功能 | 唯一知道"完整流程"的地方：加载配置 → 创建 Database → FetcherOrchestrator.fetch_all() → Processor.process() → Reporter.generate() → db.log_run() |
| 依赖 | 全部四个编排层模块 |
| 命令行参数 | `--date` `--dry-run` `--no-ai` `--daemon` `--reset-db` |
| 错误处理 | `run_once()` 用 try/except/finally 包裹处理阶段，保证无论成功/失败都调用一次 `db.log_run()` 留下记录 |
| 边界 | 不实现任何业务逻辑，只做参数解析和模块装配；daemon 模式的调度精度依赖 30s 轮询，生产环境推荐用系统 crontab |

---

## 六、复用指南 / How to Reuse These Modules Elsewhere

若要在其他项目中复用某个模块，按以下优先级评估改造成本：

| 模块 | 复用难度 | 说明 |
|------|---------|------|
| `models.py` | 零成本 | 直接复制，按需改字段名 |
| `http_client.py` | 零成本 | 直接复制，无需任何修改 |
| `simhash.py` | 零成本 | 直接复制，通用文本相似度工具 |
| `feed_parser.py` | 低成本 | 复制后按需删减不需要的 parse_xxx 函数 |
| `scorer.py` | 中成本 | 需要替换 SCORE_RULES 为目标领域的关键词规则 |
| `ai_client.py` | 中成本 | 需要替换 SYSTEM_PROMPT；PROVIDERS 注册表本身可直接复用 |
| `database.py` | 中成本 | 表结构与业务强相关，需按需调整字段，但连接管理/去重方法可参考 |
| `fetcher.py` / `processor.py` / `reporter.py` | 高成本（仅供参考架构） | 与 Intel Radar 业务强绑定，建议参考其"编排层只做胶水、不含实现细节"的设计模式，而非直接复制代码 |

---

## 七、代码规模统计

| 文件 | 行数 | 类型 |
|------|------|------|
| `models.py` | ~121 | 原子层 |
| `http_client.py` | ~100 | 原子层 |
| `simhash.py` | ~93 | 原子层 |
| `feed_parser.py` | ~193 | 原子层 |
| `scorer.py` | ~89 | 原子层 |
| `ai_client.py` | ~232 | 编排层 |
| `fetcher.py` | ~202 | 编排层 |
| `database.py` | ~182 | 编排层 |
| `processor.py` | ~144 | 编排层 |
| `reporter.py` | ~212 | 编排层 |
| `run.py` | ~203 | 装配层 |

大部分文件体量控制在 100-230 行以内，符合"每个模块只做一件事"的
设计目标；即使不熟悉全局架构，阅读单个文件的模块级 docstring 也能
快速理解其职责边界。
