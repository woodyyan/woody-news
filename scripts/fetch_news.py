"""
Woody News — 新闻采集主脚本
从 RSS 源抓取新闻，调用 AI 翻译/摘要，生成每日 JSON 数据文件
"""

import json
import hashlib
import os
import sys
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import httpx

from translator import translate_and_summarize

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# 项目根目录
ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config" / "categories.json"
DATA_DIR = ROOT_DIR / "data"
INDEX_PATH = DATA_DIR / "index.json"

# 北京时间
BJT = timezone(timedelta(hours=8))
TODAY = datetime.now(BJT).strftime("%Y-%m-%d")
NOW_HOUR = datetime.now(BJT).hour

# 判断当前采集时段：14点前为早报，14点后为晚报
EDITION = "morning" if NOW_HOUR < 14 else "evening"

# 每个分类最多抓取的新闻数
MAX_PER_SOURCE = 10
# 总新闻上限
MAX_TOTAL = 60

# HTTP 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) WoodyNewsBot/1.0",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def load_config() -> dict:
    """读取分类和 RSS 源配置"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def generate_id(link: str) -> str:
    """基于链接生成唯一 ID"""
    return hashlib.md5(link.encode()).hexdigest()[:8]


def _resolve_google_news_link(link: str) -> str:
    """解析 Google News 跳转链接，提取真实 URL"""
    if "news.google.com" not in link:
        return link

    try:
        # Google News RSS 链接中有时包含真实 URL 参数
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(link)
        # 有些 Google News 链接的真实 URL 在 query 参数中
        qs = parse_qs(parsed.query)
        if "url" in qs:
            return qs["url"][0]

        # 尝试通过 HTTP HEAD 请求获取重定向后的真实 URL
        with httpx.Client(timeout=10, follow_redirects=True, headers=HEADERS) as client:
            resp = client.head(link)
            if resp.url and str(resp.url) != link:
                return str(resp.url)
    except Exception as e:
        logger.warning(f"  ⚠️ 解析 Google News 链接失败: {e}")

    return link


def fetch_rss(source: dict, category_id: str) -> list[dict]:
    """抓取单个 RSS 源的新闻"""
    url = source["url"]
    name = source["name"]
    lang = source.get("lang", "en")

    logger.info(f"  正在抓取: {name} ({url[:60]}...)")

    try:
        # 使用 httpx 获取 RSS 内容（更好的超时和错误处理）
        with httpx.Client(timeout=30, follow_redirects=True, headers=HEADERS) as client:
            response = client.get(url)
            response.raise_for_status()

        feed = feedparser.parse(response.text)

        articles = []
        for entry in feed.entries[:MAX_PER_SOURCE]:
            link = entry.get("link", "")
            if not link:
                continue

            # 解析 Google News 跳转链接
            link = _resolve_google_news_link(link)

            # 提取图片
            image = _extract_image(entry)

            # 提取描述
            description = entry.get("summary", "") or entry.get("description", "")
            # 清理 HTML 标签
            description = _strip_html(description)

            title = entry.get("title", "").strip()
            if not title:
                continue

            # 提取发布时间
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published:
                pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                published_at = pub_dt.isoformat()
            else:
                published_at = datetime.now(timezone.utc).isoformat()

            articles.append({
                "id": generate_id(link),
                "title_raw": title,
                "description_raw": description,
                "image": image,
                "link": link,
                "category": category_id,
                "source": name,
                "lang": lang,
                "published_at": published_at,
            })

        logger.info(f"  ✅ {name}: 获取到 {len(articles)} 条")
        return articles

    except Exception as e:
        logger.error(f"  ❌ {name}: 抓取失败 - {e}")
        return []


def _extract_image(entry: dict) -> str | None:
    """从 RSS 条目中提取图片 URL"""
    # 方式1：media_content
    media = entry.get("media_content", [])
    if media and isinstance(media, list):
        for m in media:
            url = m.get("url", "")
            if url and ("image" in m.get("type", "image") or url.endswith((".jpg", ".png", ".webp"))):
                return url

    # 方式2：media_thumbnail
    thumbnails = entry.get("media_thumbnail", [])
    if thumbnails and isinstance(thumbnails, list):
        return thumbnails[0].get("url")

    # 方式3：enclosures
    enclosures = entry.get("enclosures", [])
    if enclosures:
        for enc in enclosures:
            if "image" in enc.get("type", ""):
                return enc.get("href") or enc.get("url")

    # 方式4：从 summary/content HTML 中提取 img src
    content = entry.get("summary", "") or entry.get("description", "")
    if "<img" in content:
        import re
        match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content)
        if match:
            return match.group(1)

    return None


def _strip_html(text: str) -> str:
    """简单清除 HTML 标签"""
    import re
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def process_articles(raw_articles: list[dict]) -> list[dict]:
    """调用 AI 翻译和摘要处理"""
    processed = []
    total = len(raw_articles)

    for i, raw in enumerate(raw_articles, 1):
        logger.info(f"  🤖 AI 处理 ({i}/{total}): {raw['title_raw'][:40]}...")

        result = translate_and_summarize(
            title=raw["title_raw"],
            description=raw["description_raw"],
            lang=raw["lang"],
        )

        article = {
            "id": raw["id"],
            "title": result["title"],
            "title_original": result["title_original"],
            "summary": result["summary"],
            "summary_original": result["summary_original"],
            "image": raw["image"],
            "link": raw["link"],
            "category": raw["category"],
            "source": raw["source"],
            "lang": raw["lang"],
            "published_at": raw["published_at"],
            "edition": EDITION,
        }
        processed.append(article)

    return processed


def load_existing_ids() -> set:
    """读取今天已有的新闻 ID，用于去重"""
    data_path = DATA_DIR / f"{TODAY}.json"
    if data_path.exists():
        with open(data_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {a["id"] for a in data.get("articles", [])}
    return set()


def save_data(articles: list[dict]):
    """保存新闻数据到 JSON 文件"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data_path = DATA_DIR / f"{TODAY}.json"

    # 如果今天已有数据，合并
    existing = []
    if data_path.exists():
        with open(data_path, "r", encoding="utf-8") as f:
            existing_data = json.load(f)
            existing = existing_data.get("articles", [])

    # 合并去重
    existing_ids = {a["id"] for a in existing}
    new_articles = [a for a in articles if a["id"] not in existing_ids]
    all_articles = existing + new_articles

    # 按发布时间倒序排列
    all_articles.sort(key=lambda x: x["published_at"], reverse=True)

    # 限制总数
    all_articles = all_articles[:MAX_TOTAL]

    output = {
        "date": TODAY,
        "updated_at": datetime.now(BJT).isoformat(),
        "articles": all_articles,
    }

    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info(f"💾 已保存 {len(all_articles)} 条新闻到 {data_path.name}（新增 {len(new_articles)} 条）")


def update_index():
    """更新日期索引文件"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 扫描所有数据文件
    dates = sorted(
        [f.stem for f in DATA_DIR.glob("????-??-??.json")],
        reverse=True,
    )

    index = {
        "dates": dates,
        "latest": dates[0] if dates else None,
    }

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    logger.info(f"📋 索引已更新，共 {len(dates)} 个日期")


def main():
    logger.info(f"🚀 Woody News 采集开始 — {TODAY}")
    logger.info(f"{'=' * 50}")

    # 1. 加载配置
    config = load_config()
    categories = config["categories"]
    logger.info(f"📂 加载了 {len(categories)} 个分类")

    # 2. 加载已有 ID
    existing_ids = load_existing_ids()
    logger.info(f"📄 今日已有 {len(existing_ids)} 条新闻")

    # 3. 抓取所有 RSS 源
    all_raw = []
    for cat in categories:
        logger.info(f"\n📰 分类: {cat['name']} ({cat['id']})")
        for source in cat.get("sources", []):
            articles = fetch_rss(source, cat["id"])
            # 去重
            articles = [a for a in articles if a["id"] not in existing_ids]
            all_raw.extend(articles)

    logger.info(f"\n{'=' * 50}")
    logger.info(f"📊 共获取 {len(all_raw)} 条新增新闻")

    if not all_raw:
        logger.info("✅ 没有新增新闻，跳过 AI 处理")
        update_index()
        return

    # 4. AI 翻译 & 摘要
    logger.info(f"\n🤖 开始 AI 翻译和摘要处理...")
    processed = process_articles(all_raw)

    # 5. 保存数据
    save_data(processed)

    # 6. 更新索引
    update_index()

    logger.info(f"\n{'=' * 50}")
    logger.info(f"✅ 采集完成！")


if __name__ == "__main__":
    main()
