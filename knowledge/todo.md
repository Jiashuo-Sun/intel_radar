# Intel Radar — 待办事项 / Todo

优先级说明：P0=阻断性 | P1=重要 | P2=有价值 | P3=锦上添花

---

## P0 — 已完成 ✅

- [x] 三阶段去重（url-dedup / score-filter / simhash-dedup），解决 957/962 误判问题
- [x] score=0 条目不存 DB，避免污染去重索引
- [x] 双 AI 提供商（Anthropic + LM Studio），settings.yaml 切换
- [x] summarize_threshold 与 min_score_for_report 对齐（均为 1）
- [x] `_fetch_url()` 增加 retry + 指数退避（retries=2，延迟 2/4s）
- [x] `reporter.py` 原子写文件（写 .tmp → os.replace）
- [x] `run.py` try/except/finally 确保 db.log_run() 始终执行
- [x] `_parse_ai_json()` 增加 regex 兜底，处理 AI 输出 JSON 前有前置文本的情况
- [x] `database.py` 路径安全：`os.path.abspath()` 避免空目录名报错
- [x] `_simhash()` 中文字符级 tokenize，修复中文标题相似度检测退化问题

---

## P1 — 近期计划

- [ ] **邮件推送（SMTP）**：每日日报自动发送到指定邮箱，无需手动看文件
  - 实现：`src/notifier.py` + settings.yaml `notify.email` 配置项
  - 参考：Python `smtplib` / `email` 标准库，无需额外依赖

- [ ] **企业微信 Webhook 推送**：高优先级条目（score≥4）实时推送到群机器人
  - 实现：`src/notifier.py` + settings.yaml `notify.wecom_webhook` 配置项
  - 触发时机：日报生成后，若有高优先级条目则推送摘要

- [ ] **daemon 模式稳定性**：当前时间比较用字符串匹配（`HH:MM == run_at`），30s 轮询可能漏分钟
  - 修复：改用 `schedule` 库或计算下次触发时间点 sleep 到精确时刻

---

## P2 — 中期计划

- [ ] **Web 看板（Flask）**：浏览历史日报，支持按日期、关键词搜索
  - 路由：`GET /` 列出日报列表；`GET /<date>` 渲染当日日报；`GET /search?q=` 全文搜索
  - 数据：直接读 SQLite items 表，无需额外存储

- [ ] **微信公众号监控（RSSHub 桥接）**：通过 RSSHub 订阅公众号 RSS
  - 前提：需自建或使用公共 RSSHub 实例
  - 配置：在 watch.yaml 中添加 type=rss，URL 为 RSSHub 生成的 feed

- [ ] **AI 提供商插件化**：当前 if/elif 硬编码，新增 provider 需改核心代码
  - 重构：定义 `AIProvider` 协议 + 注册表字典，`provider_name → call_fn`
  - 收益：新增 OpenAI / Ollama / Gemini 等只需添加一个函数

- [ ] **SCORE_RULES 可配置化**：当前硬编码在 `processor.py`
  - 方案：迁移到 `config/settings.yaml` 或单独 `config/score_rules.yaml`
  - 注意：正则规则需在 YAML 中用引号包裹，加载后编译

- [ ] **GROUP_META 动态化**：reporter.py 中 GROUP_META 字典与 watch.yaml topics 脱节
  - 方案：从 watch.yaml topic 的 `label` / `emoji` 字段动态构建，无需代码改动

---

## P3 — 长期/探索

- [ ] **招投标平台爬虫**：政府采购网、中国招标投标公共服务平台
  - 挑战：反爬严格，需配合代理池或控制频率
  - 价值：`market_signals` 分组质量大幅提升

- [ ] **向量数据库 + 语义搜索**：替换 SimHash，用 embedding 做语义去重和搜索
  - 选型：ChromaDB（本地）或 Qdrant，embedding 模型用 `text-embedding-3-small`
  - 收益：中文近似去重准确率大幅提升；支持"找所有关于 Werk24 的报道"语义查询

- [ ] **周报自动生成**：每周五汇总本周高优先级条目，生成周报 Markdown
  - 实现：`python run.py --weekly`，查询 DB 中 `included_in_report` 在本周的高分条目

- [ ] **LLM 关键词自动扩展**：用 AI 定期分析日报，建议新的监控关键词
  - 触发：`python run.py --suggest-keywords`，输出建议供人工审核

---

## 已知限制（不计划修复）

| 限制 | 原因 | 影响 |
|------|------|------|
| Google News 同一文章多个 URL | 平台 redirect 机制，无法控制 | 依赖 SimHash 兜底 |
| LM Studio 模型 JSON 输出不稳定 | 模型本身行为，regex 兜底已处理 | 极少数情况 warning 跳过 |
| arXiv 论文标题无法 SimHash 去重 | 英文学术标题差异大，SimHash 阈值难调 | 论文重复率本身低 |
