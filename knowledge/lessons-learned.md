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

## 已知限制

1. **SimHash 对中文短标题效果有限**：中文新闻标题通常没有空格，整个标题被当作一个词处理。SimHash 退化为 MD5 低64位，仍然有效但丢失了词级别的相似度。
2. **Google News RSS 同一文章多个URL**：同一篇报道可能通过不同 redirect URL 出现，URL 去重无法捕获，依赖 SimHash 处理。
3. **本地 LM Studio 模型 JSON 输出不稳定**：部分模型会在 JSON 外包裹 markdown code fence，`_parse_ai_json()` 已处理；但个别模型输出非 JSON 内容时会 warning 并跳过。
