# Woody News 🗞️

每日自动采集新闻，AI 翻译与总结，简洁阅读体验。

## 特性

- 🤖 **自动采集** — GitHub Actions 每天定时抓取 RSS / News Sitemap 新闻源
- 🌐 **AI 翻译** — 英文新闻自动翻译为中文，保留原文
- ✍️ **智能摘要** — 大模型生成精炼摘要总结，非原文截断
- 🧩 **主题聚合** — 同一期次内自动合并同主题重复报道，展示相关来源
- 📱 **响应式** — 适配桌面和手机，深色模式支持
- ⚡ **极速加载** — Astro 静态站，默认零 JS
- 💰 **近乎免费** — GitHub + Vercel + 火山引擎 API

## 技术栈

| 层面 | 技术 |
|------|------|
| 前端 | Astro + Tailwind CSS |
| 采集 | Python (feedparser + httpx) |
| AI | 火山引擎大模型 API |
| 定时 | GitHub Actions |
| 部署 | Vercel |

## 快速开始

### 本地开发

```bash
# 安装前端依赖
npm install

# 启动开发服务器
npm run dev
```

### 手动运行采集

```bash
# 安装 Python 依赖
pip install -r scripts/requirements.txt

# 设置环境变量
export VOLCENGINE_API_KEY="your-api-key"
export VOLCENGINE_BASE_URL="https://ark.cn-beijing.volces.com/api/v3"
export VOLCENGINE_MODEL="doubao-pro-256k"

# 运行采集
cd scripts && python fetch_news.py
```

### 部署

1. Fork 或推送到 GitHub
2. 在仓库 Settings → Secrets 中设置 `VOLCENGINE_API_KEY`、`VOLCENGINE_BASE_URL`、`VOLCENGINE_MODEL`
3. 连接 Vercel 自动部署
4. GitHub Actions 会每天自动采集新闻

## 添加/修改新闻分类

编辑 `config/categories.json`，新增分类项即可，前端会自动适应。

## 主题聚合说明

- 聚合范围：**同一期次内**（早报内、晚报内分别聚合）
- 策略：**规则预筛 + 大模型判定 + 聚类合并**
- 数据结构：保留原始 `articles`，新增 `clusters` 供前端优先展示
- 前端展示：默认显示主题卡片，并在卡片底部展示“相关报道”来源入口

## 特殊来源说明

- `type: "rss"`：标准 RSS/Atom 源，沿用原有抓取逻辑
- `type: "news_sitemap"`：适用于 Google News Sitemap 形式的来源，例如 The Information
- `route_categories`：当单一来源覆盖多个主题时，可按标题/链接规则把文章路由到 `ai` / `tech` / `business`
- 对于仅公开标题的来源，会走“标题型摘要”策略，避免输出空摘要

## 目录结构

```
├── .github/workflows/    # GitHub Actions 定时任务
├── config/               # 分类 & RSS 源配置
├── data/                 # 每日新闻 JSON 数据
├── scripts/              # Python 采集脚本
├── src/                  # Astro 前端源码
└── public/               # 静态资源
```

## License

MIT
