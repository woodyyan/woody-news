# Woody News 项目记忆

## 项目概况
- **项目名称**：Woody News（新闻聚合网站）
- **用户**：Woody
- **创建日期**：2026-03-26

## 核心决策

### 技术栈
- 前端：**Astro** + Tailwind CSS
- 采集脚本：**Python**（feedparser + httpx）
- 部署：**Vercel**（自动部署）
- 数据存储：**GitHub 仓库 JSON 文件**（按日期命名 data/YYYY-MM-DD.json）
- 定时任务：**GitHub Actions**（每天 08:00 和 20:00 北京时间，2 次）
- AI 翻译/摘要：**火山引擎大模型 API**（用户已有 Key）

### 新闻分类（可配置，存于 config/categories.json）
- world — 国际要闻
- china — 国内要闻
- tech — 科技新闻
- business — 商业新闻
- 分类未来会增加/调整，设计上不写死

### 新闻来源
- 海外：Google News RSS、TechCrunch、NYTimes、BBC 等（英文）
- 国内：36氪、澎湃新闻等（中文）

### AI 处理逻辑
- 英文新闻：翻译标题 + 生成中文总结摘要
- 中文新闻：生成精炼总结摘要
- 摘要是原文总结（80-120字），不是开头截断

### 前端设计要求
- 简洁精美，适合阅读，适配手机
- 首页只展示当天新闻，无日期选择器
- 底部有往期新闻入口按日期浏览
- 支持深色模式
- 分类 Tab 前端过滤，不刷新页面
- 英文来源显示「译」标签，可展开查看原文

### 数据结构
- 每条新闻字段：id, title, title_original, summary, summary_original, image, link, category, source, lang, published_at
- 保留 _original 字段用于展示原文

## 方案文档
- 完整设计方案：`docs/design.md`（v1.0，2026-03-26）
