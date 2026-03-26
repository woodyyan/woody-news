# Woody News — 新闻聚合网站设计方案

> 版本：v1.0 | 日期：2026-03-26 | 作者：Woody + WorkBuddy

---

## 1. 项目概述

一个简洁精美的新闻聚合网站，每天自动抓取海内外新闻，经 AI 翻译和总结后呈现给用户。零运维成本，全自动运行。

### 核心特点

- **自动化采集**：GitHub Actions 每天定时抓取，无需人工干预
- **AI 驱动**：英文新闻自动翻译，所有新闻自动生成摘要总结
- **简洁阅读**：无广告、无干扰，专注内容，适配手机
- **零成本**：GitHub + Vercel 免费额度完全覆盖

---

## 2. 技术架构

```
┌─────────────────────────────────────────────────────────┐
│                                                         │
│  GitHub Actions (每天 08:00 / 20:00 北京时间)             │
│       │                                                 │
│       ▼                                                 │
│  Python 采集脚本                                         │
│   ├── Google News RSS (英文)                             │
│   ├── 36氪 RSS (中文)                                    │
│   ├── TechCrunch RSS (英文)                              │
│   └── 其他可配置 RSS 源                                   │
│       │                                                 │
│       ▼                                                 │
│  火山引擎大模型 API                                       │
│   ├── 英文 → 翻译标题 + 生成中文总结摘要                    │
│   └── 中文 → 生成精炼总结摘要                              │
│       │                                                 │
│       ▼                                                 │
│  去重 & 清洗 → 生成 data/2026-03-26.json                  │
│       │                                                 │
│       ▼                                                 │
│  Git Push → Vercel 自动部署 → Astro 静态站点               │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 技术栈

| 层面 | 技术选型 | 说明 |
|------|---------|------|
| 前端框架 | **Astro** | 内容优先，默认零 JS，加载极快 |
| 样式方案 | **Tailwind CSS** | 原子化 CSS，响应式开发效率高 |
| 采集脚本 | **Python** | feedparser + httpx，生态成熟 |
| AI 翻译/摘要 | **火山引擎大模型 API** | OpenAI 兼容接口，翻译+总结一步完成 |
| 定时任务 | **GitHub Actions** | cron 调度，免费 2000 分钟/月 |
| 数据存储 | **GitHub 仓库 JSON 文件** | 按日期命名，版本可追溯 |
| 网站部署 | **Vercel** | 自动部署，全球 CDN，免费 |

---

## 3. 新闻分类（可配置）

分类定义存放在 `config/categories.json`，采集脚本和前端共同读取，增删分类只需修改此文件。

**初始分类：**

| ID | 名称 | 图标 | 主要来源 |
|----|------|------|---------|
| `world` | 国际要闻 | 🌍 | Google News (US), NYTimes, BBC |
| `china` | 国内要闻 | 🏠 | 36氪, 澎湃新闻 |
| `tech` | 科技新闻 | 💻 | TechCrunch, The Verge, 36氪科技 |
| `business` | 商业新闻 | 📈 | Bloomberg, 36氪商业 |

### 配置文件结构

```json
{
  "categories": [
    {
      "id": "world",
      "name": "国际要闻",
      "icon": "🌍",
      "sources": [
        {
          "name": "Google News",
          "type": "rss",
          "url": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en&topic=WORLD",
          "lang": "en"
        }
      ]
    }
  ]
}
```

**扩展方式**：在 `categories` 数组中新增一个对象即可，前端 Tab 栏自动出现新分类。

---

## 4. 数据结构

### 每日数据文件：`data/YYYY-MM-DD.json`

```json
{
  "date": "2026-03-26",
  "updated_at": "2026-03-26T20:00:00+08:00",
  "articles": [
    {
      "id": "a1b2c3d4",
      "title": "OpenAI 发布 GPT-6，推理能力大幅提升",
      "title_original": "OpenAI Releases GPT-6 with Major Reasoning Improvements",
      "summary": "OpenAI 发布了最新一代大语言模型 GPT-6，在数学推理、代码生成和多语言理解三个维度上均取得显著突破，基准测试得分较 GPT-5 提升约 40%。该模型同时优化了响应延迟，推理速度提升 2 倍。",
      "summary_original": "OpenAI today unveiled GPT-6, its latest large language model...",
      "image": "https://example.com/gpt6-launch.jpg",
      "link": "https://techcrunch.com/2026/03/26/openai-gpt6",
      "category": "tech",
      "source": "TechCrunch",
      "lang": "en",
      "published_at": "2026-03-26T14:30:00Z"
    }
  ]
}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | ✅ | 基于链接哈希生成，用于去重 |
| `title` | string | ✅ | 中文标题（英文源经翻译） |
| `title_original` | string | 否 | 原始标题（仅英文源保留） |
| `summary` | string | ✅ | AI 生成的中文总结摘要（非截断） |
| `summary_original` | string | 否 | 原始摘要（仅英文源保留） |
| `image` | string\|null | ✅ | 配图 URL，无图时为 null |
| `link` | string | ✅ | 原文链接 |
| `category` | string | ✅ | 分类 ID，对应 categories.json |
| `source` | string | ✅ | 来源站点名称 |
| `lang` | string | ✅ | 原始语言：`en` / `zh` |
| `published_at` | string | ✅ | 原文发布时间 ISO 8601 |

### 数据索引文件：`data/index.json`

```json
{
  "dates": ["2026-03-26", "2026-03-25", "2026-03-24"],
  "latest": "2026-03-26"
}
```

用于前端快速获取可用日期列表，每次采集时自动更新。

---

## 5. 采集脚本设计

### 运行环境

- **平台**：GitHub Actions
- **调度**：每天 2 次（北京时间 08:00 和 20:00）
- **Cron 表达式**：`0 0,12 * * *`（UTC 0:00 和 12:00）

### 采集流程

```
1. 读取 config/categories.json，获取所有 RSS 源
2. 并发抓取所有 RSS Feed
3. 解析条目，提取标题、链接、图片、原始摘要
4. 基于链接 hash 去重（对比当天已有数据）
5. 调用火山引擎大模型 API：
   - 英文新闻 → 翻译标题 + 生成中文总结
   - 中文新闻 → 生成精炼总结
6. 组装 JSON 数据，写入 data/YYYY-MM-DD.json
7. 更新 data/index.json
8. Git commit + push
```

### 大模型 Prompt 设计

**英文新闻翻译+总结：**

```
你是一个专业的新闻编辑。请完成以下任务：
1. 将标题翻译成简洁准确的中文
2. 基于原文内容，用中文写一段 80-120 字的新闻摘要总结，要求涵盖核心事实，不要使用"本文"、"该文"等指代词

标题：{title}
内容：{description}

请以 JSON 格式返回：
{"title": "中文标题", "summary": "中文摘要总结"}
```

**中文新闻总结：**

```
你是一个专业的新闻编辑。请基于以下新闻内容，写一段 80-120 字的摘要总结，涵盖核心事实，语言精炼。不要使用"本文"、"该文"等指代词。

标题：{title}
内容：{description}

请以 JSON 格式返回：
{"summary": "摘要总结"}
```

### 关键依赖

```
feedparser    # RSS 解析
httpx         # HTTP 请求
openai        # 火山引擎 API（OpenAI 兼容格式）
```

---

## 6. 前端页面设计

### 页面结构

**首页 (`/`)**

```
┌──────────────────────────────────────┐
│  🗞️ Woody News          [🌙 暗色]    │  ← 顶栏：网站名 + 主题切换
├──────────────────────────────────────┤
│  全部 | 国际要闻 | 国内 | 科技 | 商业  │  ← 分类 Tab（从 config 动态生成）
├──────────────────────────────────────┤
│                                      │
│  ┌─────────┐  ┌─────────┐           │
│  │  图片    │  │  图片    │           │  ← 桌面端：双列卡片
│  │  标题    │  │  标题    │           │
│  │  摘要... │  │  摘要... │           │
│  │  来源·时间│  │  译·来源 │           │
│  └─────────┘  └─────────┘           │
│                                      │
│  ┌─────────┐  ┌─────────┐           │
│  │  ...     │  │  ...     │           │
│  └─────────┘  └─────────┘           │
│                                      │
├──────────────────────────────────────┤
│  📅 往期新闻                          │  ← 底部：按日期浏览历史
│  2026-03-25 | 2026-03-24 | ...       │
├──────────────────────────────────────┤
│  © Woody News · Powered by Astro     │  ← 页脚
└──────────────────────────────────────┘
```

**手机端自动切换为单列布局。**

### 设计风格

- **简洁留白**：大量留白，卡片间距舒适，参考 Readwise / Apple News 阅读感
- **无干扰**：无广告、无弹窗、无注册登录
- **深色模式**：支持 Light / Dark 切换，跟随系统偏好
- **响应式**：桌面双列 → 平板双列 → 手机单列

### 交互细节

- 分类 Tab 切换为前端过滤，不刷新页面
- 英文来源新闻显示「译」标签，可展开查看原文标题
- 新闻卡片点击跳转原文链接（新窗口打开）
- 无图新闻使用分类对应的默认配色占位
- 底部往期新闻按日期列表展示，点击加载对应日期数据

---

## 7. 项目目录结构

```
woody-news/
├── .github/
│   └── workflows/
│       └── fetch-news.yml        # GitHub Actions 定时任务
├── config/
│   └── categories.json           # 分类 & RSS 源配置（可热更新）
├── data/
│   ├── index.json                # 日期索引
│   ├── 2026-03-26.json           # 每日新闻数据
│   └── 2026-03-25.json
├── scripts/
│   ├── fetch_news.py             # 采集主脚本
│   ├── translator.py             # AI 翻译 & 摘要模块
│   └── requirements.txt          # Python 依赖
├── src/                          # Astro 前端源码
│   ├── components/
│   │   ├── Header.astro          # 顶栏
│   │   ├── CategoryTabs.astro    # 分类 Tab
│   │   ├── NewsCard.astro        # 新闻卡片
│   │   ├── ArchiveList.astro     # 往期新闻列表
│   │   └── Footer.astro          # 页脚
│   ├── layouts/
│   │   └── Layout.astro          # 页面布局
│   ├── pages/
│   │   ├── index.astro           # 首页（当天新闻）
│   │   └── archive/
│   │       └── [date].astro      # 往期新闻页
│   └── styles/
│       └── global.css            # 全局样式
├── public/
│   └── favicon.svg
├── astro.config.mjs
├── tailwind.config.mjs
├── package.json
└── README.md
```

---

## 8. 部署配置

### GitHub Actions (`fetch-news.yml`)

```yaml
name: Fetch News

on:
  schedule:
    - cron: '0 0,12 * * *'   # UTC 0:00 & 12:00 = 北京 08:00 & 20:00
  workflow_dispatch:           # 支持手动触发

jobs:
  fetch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install -r scripts/requirements.txt
      - run: python scripts/fetch_news.py
        env:
          VOLCENGINE_API_KEY: ${{ secrets.VOLCENGINE_API_KEY }}
          VOLCENGINE_BASE_URL: ${{ secrets.VOLCENGINE_BASE_URL }}
          VOLCENGINE_MODEL: ${{ secrets.VOLCENGINE_MODEL }}
      - name: Commit & Push
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add data/
          git diff --cached --quiet || git commit -m "📰 Update news $(date +%Y-%m-%d)"
          git push
```

### Vercel 配置

- 连接 GitHub 仓库后自动识别 Astro 项目
- 每次 push 自动构建部署
- 无需额外配置

### 环境变量（GitHub Secrets）

| 变量名 | 说明 |
|--------|------|
| `VOLCENGINE_API_KEY` | 火山引擎 API Key |
| `VOLCENGINE_BASE_URL` | 火山引擎 API 地址 |
| `VOLCENGINE_MODEL` | 使用的模型名称 |

---

## 9. 成本估算

| 项目 | 费用 |
|------|------|
| GitHub Actions | 免费（每月 ~120 分钟，远低于 2000 分钟上限） |
| Vercel 部署 | 免费（个人项目足够） |
| 火山引擎 API | 约 ¥1-5/月（每天 ~100 条新闻 × 翻译+总结） |
| **合计** | **约 ¥1-5/月** |

---

## 10. 后续可扩展方向

- [ ] 增加新闻分类（如娱乐、体育、健康等）
- [ ] 接入更多 RSS 源
- [ ] 添加全文搜索功能
- [ ] RSS 输出（让别人订阅你的聚合新闻）
- [ ] 邮件订阅 / 每日摘要推送
- [ ] 新闻热度排序
