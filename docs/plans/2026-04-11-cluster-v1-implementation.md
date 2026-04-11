# 新闻主题聚合 V1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为 Woody News 增加“同一期次新闻主题聚合”能力，减少多家媒体对同一事件的重复展示，并在前端展示“相关报道”。

**Architecture:** 在采集脚本中保留原始 `articles`，新增 `clusters` 作为聚合展示层。聚合流程采用“规则预筛 + 大模型判定 + 聚类合并”，仅在同一期次内生效；前端优先展示 `clusters`，旧数据自动回退到原始文章卡片。

**Tech Stack:** Python, Astro, 火山引擎大模型 API, JSON 文件存储

---

### Task 1: 数据层聚合输出

**Files:**
- Modify: `scripts/fetch_news.py`
- Modify: `scripts/translator.py`

1. 在 `fetch_news.py` 中新增标题归一化、候选预筛、并查集聚类、cluster 组装逻辑。
2. 在 `translator.py` 中新增“是否同主题”判定与“聚合标题/摘要生成”能力。
3. 修改 `save_data()`，输出 `clusters`、`cluster_count`、`article_count`。
4. 兼容单条新闻 cluster 和旧数据回退。

### Task 2: 前端展示层改造

**Files:**
- Create: `src/components/NewsFeed.astro`
- Modify: `src/pages/index.astro`
- Modify: `src/pages/archive/[date].astro`
- Modify: `src/styles/global.css`

1. 抽取共享的新闻流组件，统一处理分类过滤、统计展示、相关报道列表。
2. 首页和往期页改为优先展示 `clusters`。
3. 卡片新增“相关报道”“X 家媒体报道”等信息。
4. 过滤时联动更新“主题数 / 报道数”。

### Task 3: 兼容与数据回填

**Files:**
- Modify: `data/*.json`（由脚本回填生成 `clusters`）
- Modify: `data/index.json`（如需要补充统计字段）

1. 对现有数据文件回填 `clusters`，保证当前站点立即可见效果。
2. 保持旧格式可访问：无 `clusters` 时前端退回原始文章列表。

### Task 4: 验证与文档更新

**Files:**
- Modify: `README.md`
- Modify: `docs/design.md`

1. 本地构建验证 Astro 页面。
2. 运行聚合回填，检查输出 JSON 合法。
3. 更新 README / 设计文档中关于“主题聚合”的说明。
