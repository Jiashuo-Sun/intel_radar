# Intel Radar — 踩坑记录

记录开发和运维过程中遇到的问题、根因分析和修复方案。

---

## Bug #1：二次运行时大量内容被 "deduped" 误判

**现象**
```
Fetched: 962 raw items
Processed: 1 kept, 957 deduped
```
几乎所有内容被丢弃，日报只有 1 条。

**根因**
`processor.py` 在 `score < min_score` 时仍调用 `db.save_item()`，把评分=0 的噪音条目 URL 写入数据库。RSS feeds 的相同文章会持续出现在 feed 中数天，第二次运行时全部命中 URL 精确匹配，被计为"重复"。

**错误代码**（修复前）
```python
if score < self.min_score:
    self.db.save_item(...)  # ← 错误：存入DB导致下次被dedup
    continue
```

**修复**
- score=0 的条目直接跳过，**不存入DB**
- 三阶段过滤分别计数：`url_deduped`、`score_filtered`、`simhash_deduped`
- 日报 header 展示完整分布，便于诊断

**修复代码**
```python
if score < self.min_score:
    score_filtered += 1
    continue  # 不存DB，下次正常重新评分
```

**影响**
已有数据库中存有大量 score=0 的历史条目，删除 DB 或用 `--reset-db` 清空后可恢复正常。

---

## Bug #2：AI 摘要从未被调用

**现象**
日志中没有 "AI summarizing" 或 "AI provider:" 字样，日报中所有条目无 AI 摘要。

**根因**
`settings.yaml` 中 `summarize_threshold: 3`，但因为 Bug #1，通过过滤的条目数极少且评分低（通常1-2分）。`to_summarize` 列表为空，AI 调用被完全跳过。

**修复**
将 `summarize_threshold` 从 `3` 降至 `1`（与 `min_score_for_report` 对齐），确保所有进入日报的条目都获得 AI 摘要。

```yaml
# 修改前
ai:
  summarize_threshold: 3

# 修改后
ai:
  summarize_threshold: 1
```

---

## 最佳实践

### 测试时避免污染数据库

```bash
# 推荐：dry-run 模式只采集，不写DB
python run.py --dry-run

# 测试完整流程但需清空历史记录时
python run.py --reset-db
```

**不推荐**：直接反复 `python run.py`，会逐渐累积大量 dedup 记录。

### 日志中的过滤计数含义

```
Filter breakdown — kept: 5 | url-seen: 120 | score<1: 820 | simhash-dedup: 2
```

| 字段 | 含义 | 正常范围 |
|------|------|---------|
| `kept` | 进入日报的条目数 | 每天 3-30 条 |
| `url-seen` | URL 已在DB中（近期日报出现过） | 0-200 |
| `score<1` | 评分=0，与业务无关 | 大多数条目（500-900） |
| `simhash-dedup` | 近似重复的文章 | 0-20 |

`score<1` 数量大是正常的 — Google News 返回的大多数文章与监控主题无关。

### AI 提供商切换

只需修改 `config/settings.yaml` 中的 `ai.provider`：
- `"anthropic"` → 需要 `export ANTHROPIC_API_KEY="sk-ant-..."`
- `"lmstudio"` → 需要 LM Studio 本地服务器已启动，无需 API key

### 每天重复运行（幂等性）

同一天重复运行：已进过日报的条目被 `url-seen` 过滤，真正新的条目追加进去。日报文件被覆写（不会重复内容）。这是正确行为。

---

---

## Bug #3：AI 输出 JSON 前有前置文本导致解析失败

**现象**
日志出现 `AI summarize failed: Expecting value: line 1 column 1`，但模型实际返回了 JSON（只是前面有说明文字）。

**根因**
`_parse_ai_json()` 原版只处理 markdown code fence 开头的情况。部分模型会在 JSON 前输出如"以下是分析结果：\n{...}"。

**修复**
增加 regex 兜底，从任意文本中提取第一个 `{...}` 块：
```python
m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
if m:
    return json.loads(m.group())
```

---

## Bug #4：database.py 路径无目录名时 os.makedirs 报错

**现象**
若 db path 配置为相对路径且无 `/`（如 `radar.db`），`os.path.dirname("radar.db")` 返回空字符串 `""`，`os.makedirs("")` 抛出 `FileNotFoundError`。

**修复**
构造前先 `os.path.abspath(path)`，确保 dirname 始终有效。

---

## 架构问题记录（已修复）

### 稳定性

- `_fetch_url()` 忽略 `retry: 2` 配置 → **已修复**：增加 retry + 指数退避（延迟 2s/4s）
- `run_once()` 无错误处理，异常不写 runs 表 → **已修复**：try/except/finally 确保 db.log_run() 始终执行
- daemon 模式用字符串比较时间（`HH:MM == run_at`）+ 30s sleep，可能漏掉分钟窗口 → **待修复**（P1）

### 鲁棒性

- `_parse_ai_json()` 只处理 fence 开头，无法处理前置文本 → **已修复**：regex 兜底
- `_simhash()` 中文无空格时退化为单词哈希 → **已修复**：追加 CJK 字符级 tokenize
- `database.py` 空 dirname 问题 → **已修复**：os.path.abspath

### 原子性

- reporter.py 用 `open(path, "w")` 直接写，崩溃可能产生残缺文件 → **已修复**：写 .tmp 再 os.replace
- db.log_run() 只在成功路径调用 → **已修复**：finally 块保证

### 拓展性（待改进，见 todo.md）

- AI provider if/elif 硬编码，新增 provider 需改核心代码 → P2
- SCORE_RULES 硬编码在 processor.py，调整权重需改代码 → P2
- GROUP_META 与 watch.yaml topics 脱节 → P2

---

## 已知限制（未计划修复）

1. **SimHash 对中文短标题效果有限（已改善）**：已增加 CJK 字符级 tokenize，相似度检测准确率提升；但极短标题（3字以内）仍难以区分。
2. **Google News RSS 同一文章多个URL**：同一篇报道可能通过不同 redirect URL 出现，URL 去重无法捕获，依赖 SimHash 兜底。
3. **本地 LM Studio 模型 JSON 输出不稳定**：部分模型会在 JSON 外包裹 markdown code fence 或前置文本，`_parse_ai_json()` 已通过 fence 解析 + regex 兜底处理；极端情况仍会 warning 跳过。
