# Intel Radar — 使用手册

## 安装

```bash
cd intel-radar
pip install -r requirements.txt
```

可选依赖（按需安装）：
```bash
pip install beautifulsoup4 requests   # 公司官网爬虫
# pip install playwright              # JS渲染页面（极少数网站需要）
```

---

## 配置

### 第一步：监控对象（`config/watch.yaml`）

配置两类监控：精品公司 + 行业关键词。文件内有详细注释，主要修改：

```yaml
premium:
  - name: "竞品公司名"
    sources:
      - type: rss
        url: "https://news.google.com/rss/search?q=公司名"
    priority: high

topics:
  drawing_ai:
    queries:
      zh:
        - "你的中文关键词"
      en:
        - "your english keyword"
```

### 第二步：运行参数（`config/settings.yaml`）

主要需要修改的字段：

```yaml
ai:
  enabled: true
  provider: "anthropic"       # 或 "lmstudio"

  anthropic:
    model: "claude-haiku-4-5-20251001"
    api_key_env: "ANTHROPIC_API_KEY"

  lmstudio:
    base_url: "http://localhost:1234/v1"
    model: "your-loaded-model-name"     # LM Studio 中已加载的模型名
    timeout_seconds: 60
```

### 第三步：设置 API Key（Anthropic）

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

建议加入 shell 配置文件（`~/.zshrc` 或 `~/.bashrc`）持久化。

---

## 运行方式

### 手动运行（最常用）

```bash
# 立即运行，生成今日日报
python run.py

# 跳过 AI 摘要（测试用/无 key 时）
python run.py --no-ai

# 补跑历史日期
python run.py --date 2025-01-14

# 只采集不存DB，用于测试配置是否正确
python run.py --dry-run

# 清空数据库后运行（重置去重记录）
python run.py --reset-db
```

### 定时运行（推荐用 crontab）

```bash
crontab -e
# 每天 6:30 自动运行
30 6 * * * cd /path/to/intel-radar && python run.py >> logs/cron.log 2>&1
```

### 后台常驻模式

```bash
python run.py --daemon
```

---

## 使用 LM Studio 本地模型

1. 打开 LM Studio，加载一个模型（推荐 Qwen2.5 7B 或更大）
2. 点击左侧 **Local Server** → 启动服务器（默认 http://localhost:1234）
3. 查看已加载模型的 ID：
   ```bash
   curl http://localhost:1234/v1/models
   ```
4. 将 model ID 填入 `config/settings.yaml`：
   ```yaml
   ai:
     provider: "lmstudio"
     lmstudio:
       model: "lmstudio-community/Qwen2.5-7B-Instruct-GGUF"
   ```
5. 运行 `python run.py` 即可

---

## 查看日报

```bash
# 列出所有日报
ls output/

# 阅读今日日报
cat output/$(date +%Y-%m-%d).md
```

日报在 Markdown 阅读器中（VS Code / Typora / Obsidian）效果最佳。

---

## 日报字段说明

### 日报 Header 统计行

```
> 采集时间：06:30 | 进入日报：8 | 高优先级：2 | 已见过：120 | 低相关过滤：820 | 近似去重：3
```

| 字段 | 含义 |
|------|------|
| 进入日报 | 本次进入日报的新条目数 |
| 高优先级 | 评分 ≥ 4 的条目数 |
| 已见过 | URL 已在历史DB中（近期日报已有）|
| 低相关过滤 | 评分=0，与监控主题无关 |
| 近似去重 | SimHash 检测到的近似重复文章 |

`低相关过滤` 数量大（几百条）是正常的。

### 条目优先级

| 图标 | 评分 | 说明 |
|------|------|------|
| 🔴 | ≥ 4 | 高优先级，今日必读，含完整 AI 分析 |
| 🟡 | 2-3 | 行业动态，简要展示 |
| 🔵 | 2-3（精品监控来源） | 竞品动态表格 |
| 📚 | arXiv 来源 | 技术论文列表 |

---

## 调整监控灵敏度

### 捕获更多内容（误报率上升）

```yaml
processor:
  min_score_for_report: 0   # 默认 1，降低会显示更多内容
  simhash_threshold: 5      # 默认 3，提高会减少近似去重
```

### 过滤更严格（可能漏掉内容）

```yaml
processor:
  min_score_for_report: 2   # 只显示有明确信号的条目
  dedup_window_days: 14     # 去重窗口延长到14天
```

---

## 常见问题

**Q: 日报内容很少，只有 1-2 条？**

A: 查看日志中的过滤分布。如果 `url-seen` 数量很大（几百条），说明数据库中积累了大量历史记录。
- 短期：用 `--reset-db` 清空重来
- 长期原因：之前多次测试运行导致DB积累

**Q: AI 摘要没有出现？**

A: 按顺序检查：
1. `config/settings.yaml` 中 `ai.enabled: true`
2. provider 是 anthropic 时：`echo $ANTHROPIC_API_KEY` 是否有值
3. provider 是 lmstudio 时：`curl http://localhost:1234/v1/models` 是否正常响应
4. 查看 `logs/radar.log` 中的 WARNING 信息

**Q: 如何只测试采集效果而不影响数据库？**

A: 使用 `python run.py --dry-run`，只打印前20条采集结果，不写入DB。

**Q: 如何重跑昨天的日报？**

A: `python run.py --date $(date -v -1d +%Y-%m-%d)`（macOS）或 `python run.py --date $(date -d yesterday +%Y-%m-%d)`（Linux）
