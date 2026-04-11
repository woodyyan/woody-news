"""
Woody News — 新闻采集主脚本
从 RSS 源抓取新闻，调用 AI 翻译/摘要，生成每日 JSON 数据文件
"""

import json
import hashlib
import logging
import re
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import feedparser
import httpx

from translator import judge_same_topic, summarize_cluster, translate_and_summarize

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
# 同主题聚合：发布时间窗口（小时）
CLUSTER_TIME_WINDOW_HOURS = 36
# 模型判定阈值
CLUSTER_CONFIDENCE_THRESHOLD = 0.85

EN_STOPWORDS = {
    "about", "after", "amid", "analyst", "announces", "article", "because", "could",
    "from", "into", "latest", "launch", "launches", "over", "says", "said", "their",
    "them", "this", "that", "these", "those", "update", "updates", "with", "what",
    "when", "where", "will", "news", "china", "world", "business", "tech", "live",
}

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


def _normalize_text(text: str | None) -> str:
    """归一化文本，便于做相似度判断"""
    text = (text or "").lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _english_tokens(text: str) -> set[str]:
    tokens = {token for token in re.findall(r"[a-z0-9]{3,}", text)}
    return {token for token in tokens if token not in EN_STOPWORDS}


def _cjk_ngrams(text: str, n: int = 4) -> set[str]:
    cleaned = re.sub(r"[^\u4e00-\u9fff]", "", text)
    if len(cleaned) < n:
        return set()
    return {cleaned[i : i + n] for i in range(len(cleaned) - n + 1)}


def _combined_title(article: dict) -> str:
    return " ".join(filter(None, [article.get("title"), article.get("title_original")]))


def _combined_summary(article: dict) -> str:
    return " ".join(filter(None, [article.get("summary"), article.get("summary_original")]))


def _text_similarity(text_a: str, text_b: str) -> float:
    if not text_a or not text_b:
        return 0.0
    return SequenceMatcher(None, text_a, text_b).ratio()


def _keyword_overlap(article_a: dict, article_b: dict) -> int:
    title_a = _normalize_text(_combined_title(article_a))
    title_b = _normalize_text(_combined_title(article_b))
    english_overlap = len(_english_tokens(title_a) & _english_tokens(title_b))
    cjk_overlap = len(_cjk_ngrams(title_a) & _cjk_ngrams(title_b))
    return english_overlap if english_overlap else min(cjk_overlap, 4)


def _title_similarity(article_a: dict, article_b: dict) -> float:
    return _text_similarity(_normalize_text(_combined_title(article_a)), _normalize_text(_combined_title(article_b)))


def _hours_between(iso_a: str, iso_b: str) -> float:
    dt_a = datetime.fromisoformat(iso_a)
    dt_b = datetime.fromisoformat(iso_b)
    return abs((dt_a - dt_b).total_seconds()) / 3600


def _candidate_score(article_a: dict, article_b: dict) -> float:
    title_sim = _title_similarity(article_a, article_b)
    keyword_overlap = _keyword_overlap(article_a, article_b)
    return max(title_sim, min(keyword_overlap / 5, 0.8))


def _is_same_topic_candidate(article_a: dict, article_b: dict) -> bool:
    if article_a.get("category") != article_b.get("category"):
        return False

    if _hours_between(article_a["published_at"], article_b["published_at"]) > CLUSTER_TIME_WINDOW_HOURS:
        return False

    title_sim = _title_similarity(article_a, article_b)
    keyword_overlap = _keyword_overlap(article_a, article_b)

    return (
        title_sim >= 0.72
        or (title_sim >= 0.58 and keyword_overlap >= 2)
        or keyword_overlap >= 4
    )


def _find(parent: list[int], idx: int) -> int:
    if parent[idx] != idx:
        parent[idx] = _find(parent, parent[idx])
    return parent[idx]


def _union(parent: list[int], a: int, b: int):
    root_a = _find(parent, a)
    root_b = _find(parent, b)
    if root_a != root_b:
        parent[root_b] = root_a


def build_clusters(articles: list[dict]) -> list[dict]:
    """对同一期次新闻做同主题聚合，保留原始 articles 供回退使用"""
    if not articles:
        return []

    parent = list(range(len(articles)))
    candidates = []
    for i in range(len(articles)):
        for j in range(i + 1, len(articles)):
            article_a = articles[i]
            article_b = articles[j]
            if _is_same_topic_candidate(article_a, article_b):
                candidates.append((i, j, _candidate_score(article_a, article_b)))

    candidates.sort(key=lambda item: item[2], reverse=True)
    logger.info(f"🧩 主题聚合候选对：{len(candidates)}")

    for i, j, score in candidates:
        if _find(parent, i) == _find(parent, j):
            continue

        article_a = articles[i]
        article_b = articles[j]
        title_sim = _title_similarity(article_a, article_b)
        strong_keyword_overlap = _keyword_overlap(article_a, article_b) >= 3
        if title_sim >= 0.92 or (title_sim >= 0.84 and strong_keyword_overlap):
            _union(parent, i, j)
            logger.info(
                f"  🔗 规则合并主题: {article_a['source']} + {article_b['source']} "
                f"(title_sim={title_sim:.2f})"
            )
            continue

        result = judge_same_topic(article_a, article_b)
        if result["same_topic"] and result["confidence"] >= CLUSTER_CONFIDENCE_THRESHOLD:
            _union(parent, i, j)
            logger.info(
                f"  🔗 模型合并主题: {article_a['source']} + {article_b['source']} "
                f"(score={score:.2f}, confidence={result['confidence']:.2f})"
            )

    groups: dict[int, list[int]] = {}
    for idx in range(len(articles)):
        root = _find(parent, idx)
        groups.setdefault(root, []).append(idx)

    clusters = []
    for indexes in groups.values():
        grouped_articles = [articles[idx] for idx in indexes]
        grouped_articles.sort(key=lambda item: item["published_at"], reverse=True)

        merged = summarize_cluster(grouped_articles)
        latest = grouped_articles[0]
        source_order = []
        seen_sources = set()
        related_articles = []
        for article in grouped_articles:
            if article["source"] not in seen_sources:
                seen_sources.add(article["source"])
                source_order.append(article["source"])
            related_articles.append(
                {
                    "id": article["id"],
                    "source": article["source"],
                    "title": article["title"],
                    "link": article["link"],
                    "published_at": article["published_at"],
                }
            )

        image = next((article.get("image") for article in grouped_articles if article.get("image")), None)
        cluster_id_seed = "|".join(sorted(article["id"] for article in grouped_articles))
        clusters.append(
            {
                "id": generate_id(cluster_id_seed),
                "title": merged.get("title") or latest["title"],
                "summary": merged.get("summary") or latest["summary"],
                "category": latest["category"],
                "image": image,
                "article_ids": [article["id"] for article in grouped_articles],
                "article_count": len(grouped_articles),
                "source_count": len(source_order),
                "sources": source_order,
                "articles": related_articles,
                "published_at": latest["published_at"],
                "is_merged": len(grouped_articles) > 1,
            }
        )

    clusters.sort(key=lambda item: item["published_at"], reverse=True)
    logger.info(f"🧩 主题聚合完成：{len(articles)} 条报道 → {len(clusters)} 个主题")
    return clusters


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
    """读取当前期次已有的新闻 ID，用于去重"""
    data_path = DATA_DIR / f"{TODAY}-{EDITION}.json"
    if data_path.exists():
        with open(data_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {a["id"] for a in data.get("articles", [])}
    return set()


def save_data(articles: list[dict]):
    """保存新闻数据到 JSON 文件（每期独立文件）"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data_path = DATA_DIR / f"{TODAY}-{EDITION}.json"

    # 如果当前期次已有数据，合并
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

    clusters = build_clusters(all_articles)

    output = {
        "date": TODAY,
        "edition": EDITION,
        "updated_at": datetime.now(BJT).isoformat(),
        "article_count": len(all_articles),
        "cluster_count": len(clusters),
        "articles": all_articles,
        "clusters": clusters,
    }

    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info(
        f"💾 已保存 {len(all_articles)} 条新闻到 {data_path.name}（新增 {len(new_articles)} 条，聚合为 {len(clusters)} 个主题）"
    )


def update_index():
    """更新索引文件，记录所有可用的期次"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 扫描所有数据文件（新格式: YYYY-MM-DD-edition.json，旧格式: YYYY-MM-DD.json）
    editions = []
    for f in DATA_DIR.glob("*.json"):
        if f.name == "index.json":
            continue
        stem = f.stem  # e.g. "2026-03-27-evening" or "2026-03-26"
        parts = stem.rsplit("-", 1)
        if parts[-1] in ("morning", "evening"):
            date = stem[:-len(parts[-1])-1]
            edition = parts[-1]
        else:
            date = stem
            edition = "all"
        editions.append({"date": date, "edition": edition, "file": f.name})

    # 按日期倒序，同日 evening 在 morning 前
    edition_order = {"evening": 0, "morning": 1, "all": 2}
    editions.sort(key=lambda x: (-int(x["date"].replace("-", "")), edition_order.get(x["edition"], 9)))

    index = {
        "editions": editions,
        "latest": editions[0]["file"].replace(".json", "") if editions else None,
    }

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    logger.info(f"📋 索引已更新，共 {len(editions)} 期")


def main():
    edition_label = "早报" if EDITION == "morning" else "晚报"
    logger.info(f"🚀 Woody News 采集开始 — {TODAY} {edition_label}")
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
