# Intel Radar — src package
#
# 模块依赖关系（乐高积木式架构，箭头表示"依赖"方向）：
#
#   models.py  (零依赖，公共数据结构)
#      ↑  ↑  ↑
#      │  │  └── reporter.py     （渲染 Markdown，纯函数式）
#      │  └───── feed_parser.py  （解析 RSS/Atom/arXiv/HTML，纯函数式）
#      └──────── ai_client.py    （AI 摘要，provider 可插拔）
#
#   http_client.py（零依赖，通用 HTTP GET+重试）
#   simhash.py     （零依赖，纯算法）
#   scorer.py       依赖 models.py
#
#   fetcher.py    依赖 models / http_client / feed_parser
#   database.py   依赖 simhash
#   processor.py  依赖 models / database / scorer / ai_client
#
#   run.py（项目根目录）——装配层，依赖 database/fetcher/processor/reporter，
#   把以上积木按固定顺序串成一条 pipeline。
#
# 详见 knowledge/lego-modules.md 完整模块文档。
