"""
Woody News — 新闻采集主脚本
从 RSS 源抓取新闻，调用 AI 翻译/摘要，生成每日 JSON 数据文件
"""

import argparse
import hashlib
import ipaddress
import json
import logging
import re
import socket
import struct
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

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
PAGE_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}
IMAGE_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "image/webp,image/png,image/jpeg,image/gif;q=0.9,*/*;q=0.5",
    "Range": "bytes=0-131071",
}
IMAGE_MIME_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
}
IMAGE_EXTENSIONS = (".gif", ".jpeg", ".jpg", ".png", ".webp")
MAX_RSS_IMAGE_CANDIDATES = 2
MAX_PAGE_IMAGE_CANDIDATES = 2
MAX_REDIRECTS = 5
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_HEADER_BYTES = 128 * 1024
MAX_ARTICLE_HTML_BYTES = 2 * 1024 * 1024
MIN_IMAGE_WIDTH = 300
MIN_IMAGE_HEIGHT = 160

NEWS_SITEMAP_NS = {"news": "http://www.google.com/schemas/sitemap-news/0.9"}
AI_PATH_HINTS = ("/newsletters/ai-agenda/", "/newsletters/applied-ai/")
BUSINESS_PATH_HINTS = ("/newsletters/dealmaker/", "/newsletters/the-information-finance/")
AI_ROUTE_KEYWORDS = {
    "ai", "artificial intelligence", "agent", "agents", "anthropic", "chatgpt", "claude",
    "copilot", "foundation model", "genai", "gemini", "gpt", "inference", "llm",
    "machine learning", "model", "models", "openai", "seedance", "siri",
}
TECH_ROUTE_KEYWORDS = {
    "android", "app", "apps", "chip", "chips", "cloud", "coding", "code", "data center",
    "developer", "developers", "device", "devices", "gpu", "hardware", "infra",
    "infrastructure", "iphone", "platform", "robotaxi", "semiconductor", "server",
    "software", "tool", "tools",
}
BUSINESS_ROUTE_KEYWORDS = {
    "acquire", "acquires", "acquisition", "ads", "board", "ceo", "deal", "deals",
    "finance", "financing", "fund", "funding", "funds", "investment", "investments",
    "ipo", "ipos", "layoff", "layoffs", "market", "markets", "merger", "pricing",
    "profit", "purchase", "raises", "raised", "raise", "revenue", "sale", "sales",
    "stake", "valuation", "valued",
}


def load_config() -> dict:
    """读取分类和 RSS 源配置"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def generate_id(link: str) -> str:
    """基于链接生成唯一 ID"""
    return hashlib.md5(link.encode()).hexdigest()[:8]



def _build_request_headers(source: dict | None = None) -> dict:
    headers = dict(HEADERS)
    if source and source.get("user_agent"):
        headers["User-Agent"] = source["user_agent"]
    if source and source.get("accept"):
        headers["Accept"] = source["accept"]
    return headers



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


def _match_keywords(text: str, keywords: set[str]) -> int:
    normalized_text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    padded_text = f" {normalized_text} "
    score = 0
    for keyword in keywords:
        normalized_keyword = re.sub(r"[^a-z0-9]+", " ", keyword.lower()).strip()
        if normalized_keyword and f" {normalized_keyword} " in padded_text:
            score += 1
    return score



def _route_category(source: dict, default_category_id: str, title: str, description: str, link: str) -> str:
    route_categories = set(source.get("route_categories", []))
    if not route_categories:
        return default_category_id

    profile = source.get("routing_profile")
    if profile != "the_information":
        return default_category_id

    path = urlparse(link).path.lower()
    text = " ".join(filter(None, [title.lower(), description.lower(), path]))

    if any(hint in path for hint in AI_PATH_HINTS):
        return "ai"
    if any(hint in path for hint in BUSINESS_PATH_HINTS):
        return "business"

    ai_score = _match_keywords(text, AI_ROUTE_KEYWORDS)
    business_score = _match_keywords(text, BUSINESS_ROUTE_KEYWORDS)
    tech_score = _match_keywords(text, TECH_ROUTE_KEYWORDS)

    if "ai" in route_categories and ai_score >= 1:
        return "ai"
    if "business" in route_categories and business_score >= 2:
        return "business"
    if "tech" in route_categories and tech_score >= 1:
        return "tech"
    if "business" in route_categories and business_score >= 1:
        return "business"

    return default_category_id



def _build_raw_article(
    source: dict,
    default_category_id: str,
    title: str,
    description: str,
    image: str | None,
    link: str,
    published_at: str,
    image_validated: bool = False,
) -> dict:
    category_id = _route_category(source, default_category_id, title, description, link)
    return {
        "id": generate_id(link),
        "title_raw": title,
        "description_raw": description,
        "image": image,
        "image_validated": image_validated,
        "link": link,
        "category": category_id,
        "source": source["name"],
        "lang": source.get("lang", "en"),
        "published_at": published_at,
    }



def fetch_rss(source: dict, category_id: str, excluded_ids: set[str] | None = None) -> list[dict]:
    """抓取单个 RSS 源的新闻"""
    url = source["url"]
    name = source["name"]

    logger.info(f"  正在抓取: {name} ({url[:60]}...)")

    try:
        with httpx.Client(timeout=30, follow_redirects=True, headers=_build_request_headers(source)) as client:
            response = client.get(url)
            response.raise_for_status()

        feed = feedparser.parse(response.text)

        articles = []
        with httpx.Client(timeout=httpx.Timeout(8, connect=5), follow_redirects=False) as image_client:
            for entry in feed.entries[:MAX_PER_SOURCE]:
                link = entry.get("link", "")
                if not link:
                    continue

                link = _resolve_google_news_link(link)
                if excluded_ids and generate_id(link) in excluded_ids:
                    continue
                image, image_validated = _resolve_article_image(
                    client=image_client,
                    entry=entry,
                    article_url=link,
                    feed_url=source["url"],
                )
                description = _strip_html(entry.get("summary", "") or entry.get("description", ""))
                title = entry.get("title", "").strip()
                if not title:
                    continue

                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if published:
                    pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                    published_at = pub_dt.isoformat()
                else:
                    published_at = datetime.now(timezone.utc).isoformat()

                articles.append(
                    _build_raw_article(
                        source=source,
                        default_category_id=category_id,
                        title=title,
                        description=description,
                        image=image,
                        link=link,
                        published_at=published_at,
                        image_validated=image_validated,
                    )
                )

        logger.info(f"  ✅ {name}: 获取到 {len(articles)} 条")
        return articles

    except Exception as e:
        logger.error(f"  ❌ {name}: 抓取失败 - {e}")
        return []



def fetch_news_sitemap(source: dict, category_id: str, excluded_ids: set[str] | None = None) -> list[dict]:
    """抓取 Google News Sitemap 格式的新闻源"""
    url = source["url"]
    name = source["name"]

    logger.info(f"  正在抓取: {name} ({url[:60]}...)")

    try:
        with httpx.Client(timeout=30, follow_redirects=True, headers=_build_request_headers(source)) as client:
            response = client.get(url)
            response.raise_for_status()

        root = ET.fromstring(response.text)
        articles = []
        with httpx.Client(timeout=httpx.Timeout(8, connect=5), follow_redirects=False) as image_client:
            for url_node in root.findall("{*}url"):
                link = (url_node.findtext("{*}loc") or "").strip()
                title = (url_node.findtext("news:news/news:title", namespaces=NEWS_SITEMAP_NS) or "").strip()
                published_at = (
                    url_node.findtext("news:news/news:publication_date", namespaces=NEWS_SITEMAP_NS) or ""
                ).strip()
                if not link or not title:
                    continue
                if excluded_ids and generate_id(link) in excluded_ids:
                    continue

                image, image_validated = _resolve_article_image(
                    client=image_client,
                    entry=None,
                    article_url=link,
                    feed_url=source["url"],
                )
                articles.append(
                    _build_raw_article(
                        source=source,
                        default_category_id=category_id,
                        title=title,
                        description="",
                        image=image,
                        link=link,
                        published_at=published_at or datetime.now(timezone.utc).isoformat(),
                        image_validated=image_validated,
                    )
                )

                if len(articles) >= MAX_PER_SOURCE:
                    break

        logger.info(f"  ✅ {name}: 获取到 {len(articles)} 条")
        return articles

    except Exception as e:
        logger.error(f"  ❌ {name}: 抓取失败 - {e}")
        return []



def fetch_source(source: dict, category_id: str, excluded_ids: set[str] | None = None) -> list[dict]:
    source_type = source.get("type", "rss")
    if source_type == "rss":
        return fetch_rss(source, category_id, excluded_ids=excluded_ids)
    if source_type == "news_sitemap":
        return fetch_news_sitemap(source, category_id, excluded_ids=excluded_ids)

    logger.warning(f"  ⚠️ {source['name']}: 不支持的来源类型 {source_type}，已跳过")
    return []


def _append_unique(values: list[str], value: str | None):
    value = (value or "").strip()
    if value and value not in values:
        values.append(value)


def _srcset_candidates(srcset: str) -> list[str]:
    """按清晰度从高到低解析 srcset。"""
    parsed = []
    for item in srcset.split(","):
        parts = item.strip().rsplit(maxsplit=1)
        if not parts:
            continue
        url = parts[0]
        score = 0.0
        if len(parts) == 2:
            descriptor = parts[1].lower()
            try:
                if descriptor.endswith("w"):
                    score = float(descriptor[:-1])
                elif descriptor.endswith("x"):
                    score = float(descriptor[:-1]) * 1000
            except ValueError:
                score = 0.0
        parsed.append((score, url))
    parsed.sort(key=lambda item: item[0], reverse=True)
    return [url for _, url in parsed]


class _ImageMetadataParser(HTMLParser):
    """从 RSS HTML 或文章页提取图片候选，不执行脚本。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.open_graph: list[str] = []
        self.twitter: list[str] = []
        self.images: list[str] = []
        self.json_ld_blocks: list[str] = []
        self.base_href: str | None = None
        self._in_json_ld = False
        self._json_ld_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            content = attributes.get("content")
            if key in {"og:image", "og:image:url", "og:image:secure_url"}:
                _append_unique(self.open_graph, content)
            elif key in {"twitter:image", "twitter:image:src"}:
                _append_unique(self.twitter, content)
        elif tag.lower() in {"img", "source"}:
            srcset = attributes.get("srcset") or attributes.get("data-srcset") or ""
            for candidate in _srcset_candidates(srcset):
                _append_unique(self.images, candidate)
            if tag.lower() == "img":
                for key in ("src", "data-src", "data-original", "data-lazy-src"):
                    _append_unique(self.images, attributes.get(key))
        elif tag.lower() == "base" and not self.base_href:
            self.base_href = (attributes.get("href") or "").strip() or None
        elif tag.lower() == "script" and "ld+json" in attributes.get("type", "").lower():
            self._in_json_ld = True
            self._json_ld_parts = []

    def handle_data(self, data: str):
        if self._in_json_ld:
            self._json_ld_parts.append(data)

    def handle_endtag(self, tag: str):
        if tag.lower() == "script" and self._in_json_ld:
            self.json_ld_blocks.append("".join(self._json_ld_parts))
            self._in_json_ld = False
            self._json_ld_parts = []


def _json_ld_image_candidates(value) -> list[str]:
    candidates: list[str] = []

    def visit(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if key.lower() == "image":
                    collect_image(child)
                else:
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    def collect_image(node):
        if isinstance(node, str):
            _append_unique(candidates, node)
        elif isinstance(node, dict):
            for key in ("url", "contentUrl"):
                if isinstance(node.get(key), str):
                    _append_unique(candidates, node[key])
            for child in node.values():
                if isinstance(child, (dict, list)):
                    collect_image(child)
        elif isinstance(node, list):
            for child in node:
                collect_image(child)

    visit(value)
    return candidates


def _parse_html_image_metadata(html: str) -> tuple[list[str], str | None]:
    parser = _ImageMetadataParser()
    try:
        parser.feed(html)
    except Exception:
        logger.debug("HTML 图片元数据解析未完整结束", exc_info=True)

    candidates = [*parser.open_graph, *parser.twitter]
    for block in parser.json_ld_blocks:
        try:
            payload = json.loads(block)
        except (TypeError, json.JSONDecodeError):
            continue
        for candidate in _json_ld_image_candidates(payload):
            _append_unique(candidates, candidate)
    for candidate in parser.images:
        _append_unique(candidates, candidate)
    return candidates, parser.base_href


def _parse_html_image_candidates(html: str) -> list[str]:
    candidates, _ = _parse_html_image_metadata(html)
    return candidates


def _dimension_value(value) -> int:
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else 0


def _extract_image_candidates(entry: dict) -> list[str]:
    """按可信度从高到低提取 RSS 条目中的全部图片候选。"""
    candidates: list[str] = []

    media = entry.get("media_content", [])
    if isinstance(media, list):
        ranked_media = sorted(
            media,
            key=lambda item: _dimension_value(item.get("width")) * _dimension_value(item.get("height")),
            reverse=True,
        )
        for item in ranked_media:
            url = item.get("url", "")
            mime = item.get("type", "image")
            if url and ("image" in mime or urlparse(url).path.lower().endswith(IMAGE_EXTENSIONS)):
                _append_unique(candidates, url)

    thumbnails = entry.get("media_thumbnail", [])
    if isinstance(thumbnails, list):
        ranked_thumbnails = sorted(
            thumbnails,
            key=lambda item: _dimension_value(item.get("width")) * _dimension_value(item.get("height")),
            reverse=True,
        )
        for item in ranked_thumbnails:
            _append_unique(candidates, item.get("url"))

    enclosures = entry.get("enclosures", [])
    if isinstance(enclosures, list):
        for enclosure in enclosures:
            url = enclosure.get("href") or enclosure.get("url")
            mime = enclosure.get("type", "")
            if url and ("image" in mime or urlparse(url).path.lower().endswith(IMAGE_EXTENSIONS)):
                _append_unique(candidates, url)

    html_blocks = []
    content = entry.get("content", [])
    if isinstance(content, list):
        html_blocks.extend(item.get("value", "") for item in content if isinstance(item, dict))
    html_blocks.extend([entry.get("summary", ""), entry.get("description", "")])
    for html in html_blocks:
        if not html:
            continue
        for candidate in _parse_html_image_candidates(html):
            _append_unique(candidates, candidate)

    return candidates


def _extract_image(entry: dict) -> str | None:
    """兼容旧调用：返回 RSS 条目中的首个图片候选。"""
    candidates = _extract_image_candidates(entry)
    return candidates[0] if candidates else None


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _is_public_http_url(url: str) -> bool:
    """拒绝本机、内网、保留地址及非标准 Web 端口，避免被新闻源诱导访问内网。"""
    if not _is_http_url(url):
        return False
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        return False
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return False
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        return False
    if not addresses:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            return False
    return True


def _safe_request_url(url: str) -> str | None:
    return url if _is_public_http_url(url) else None


@contextmanager
def _safe_stream_get(client: httpx.Client, url: str, headers: dict):
    """逐跳校验重定向，防止重定向到内网地址。"""
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        safe_url = _safe_request_url(current_url)
        if not safe_url:
            raise ValueError(f"不安全的 URL: {current_url}")
        with client.stream("GET", safe_url, headers=headers, follow_redirects=False) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    response.raise_for_status()
                current_url = urljoin(str(response.url), location)
                continue
            yield response
            return
    raise httpx.TooManyRedirects(
        f"重定向超过 {MAX_REDIRECTS} 次",
        request=httpx.Request("GET", current_url),
    )


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    offset = 2
    while offset + 9 < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(data):
            break
        segment_length = int.from_bytes(data[offset:offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            return width, height
        offset += segment_length
    return None


def _image_dimensions(data: bytes, content_type: str) -> tuple[int, int] | None:
    if content_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if content_type == "image/gif" and data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if content_type in {"image/jpeg", "image/jpg"}:
        return _jpeg_dimensions(data)
    if content_type == "image/webp" and len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk_type = data[12:16]
        if chunk_type == b"VP8X":
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height
        if chunk_type == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        frame_header = data.find(b"\x9d\x01\x2a", 20)
        if frame_header >= 0 and frame_header + 7 <= len(data):
            width = int.from_bytes(data[frame_header + 3:frame_header + 5], "little") & 0x3FFF
            height = int.from_bytes(data[frame_header + 5:frame_header + 7], "little") & 0x3FFF
            return width, height
    return None


def _validate_image_url(client: httpx.Client, url: str) -> str | None:
    """按浏览器无 Referer 的方式验证图片 MIME、签名、体积和最小尺寸。"""
    try:
        with _safe_stream_get(client, url, IMAGE_HEADERS) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type not in IMAGE_MIME_TYPES:
                return None

            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_IMAGE_BYTES:
                return None
            content_range = response.headers.get("content-range", "")
            total_match = re.search(r"/(\d+)$", content_range)
            if total_match and int(total_match.group(1)) > MAX_IMAGE_BYTES:
                return None

            prefix = bytearray()
            for chunk in response.iter_bytes(chunk_size=16 * 1024):
                remaining = MAX_IMAGE_HEADER_BYTES - len(prefix)
                if remaining <= 0:
                    break
                prefix.extend(chunk[:remaining])
                if len(prefix) >= MAX_IMAGE_HEADER_BYTES:
                    break

            dimensions = _image_dimensions(bytes(prefix), content_type)
            if not dimensions:
                return None
            if dimensions[0] < MIN_IMAGE_WIDTH or dimensions[1] < MIN_IMAGE_HEIGHT:
                return None
            return str(response.url)
    except (httpx.HTTPError, OSError, ValueError):
        logger.debug(f"图片校验失败: {url}", exc_info=True)
        return None


def _fetch_article_html(client: httpx.Client, url: str) -> tuple[str, str] | None:
    if not _is_http_url(url):
        return None
    try:
        with _safe_stream_get(client, url, PAGE_HEADERS) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if "html" not in content_type:
                return None

            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                remaining = MAX_ARTICLE_HTML_BYTES - len(data)
                if remaining <= 0:
                    break
                data.extend(chunk[:remaining])
                if len(data) >= MAX_ARTICLE_HTML_BYTES:
                    break
            encoding = response.encoding or "utf-8"
            return data.decode(encoding, errors="replace"), str(response.url)
    except (httpx.HTTPError, OSError, ValueError, LookupError):
        logger.debug(f"文章页面抓取失败: {url}", exc_info=True)
        return None


def _resolve_article_image(
    client: httpx.Client,
    entry: dict | None,
    article_url: str,
    feed_url: str,
) -> tuple[str | None, bool]:
    """先验证 RSS 图片；无可用图时再从文章页元数据补图。"""
    rss_candidates = _extract_image_candidates(entry or {})
    rss_base_url = (entry or {}).get("base") or (entry or {}).get("href") or feed_url
    for candidate in rss_candidates[:MAX_RSS_IMAGE_CANDIDATES]:
        absolute_url = urljoin(rss_base_url, candidate)
        validated = _validate_image_url(client, absolute_url)
        if validated:
            return validated, True

    page = _fetch_article_html(client, article_url)
    if not page:
        return None, False

    html, final_article_url = page
    page_candidates, base_href = _parse_html_image_metadata(html)
    image_base_url = urljoin(final_article_url, base_href) if base_href else final_article_url
    for candidate in page_candidates[:MAX_PAGE_IMAGE_CANDIDATES]:
        absolute_url = urljoin(image_base_url, candidate)
        validated = _validate_image_url(client, absolute_url)
        if validated:
            return validated, True
    return None, False


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


def _select_cluster_image(grouped_articles: list[dict]) -> str | None:
    """优先选择已在采集阶段验证过的图片，再兼容历史数据中的未标记图片。"""
    for article in grouped_articles:
        if article.get("image") and article.get("image_validated"):
            return article["image"]
    for article in grouped_articles:
        if article.get("image"):
            return article["image"]
    return None


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

        image = _select_cluster_image(grouped_articles)
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
            "image_validated": raw.get("image_validated", False),
            "link": raw["link"],
            "category": raw["category"],
            "source": raw["source"],
            "lang": raw["lang"],
            "published_at": raw["published_at"],
            "edition": EDITION,
        }
        processed.append(article)

    return processed


def _latest_data_path() -> Path | None:
    """从索引定位最新一期，索引不可用时按文件名回退。"""
    if INDEX_PATH.exists():
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                latest = json.load(f).get("latest")
            if latest:
                indexed_path = DATA_DIR / f"{latest}.json"
                if indexed_path.exists():
                    return indexed_path
        except (OSError, ValueError, TypeError):
            logger.warning("⚠️ 无法读取数据索引，将按文件名定位最新一期")

    candidates = sorted(
        path for path in DATA_DIR.glob("*.json") if path.name != INDEX_PATH.name
    )
    return candidates[-1] if candidates else None


def backfill_latest_images() -> dict:
    """只回填最新一期图片，不重新翻译、摘要或主题聚类。"""
    data_path = _latest_data_path()
    if not data_path:
        raise FileNotFoundError("没有可回填的新闻数据文件")

    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    articles = data.get("articles", [])
    stats = {"total": len(articles), "kept": 0, "added": 0, "replaced": 0, "missing": 0}
    article_by_id = {}

    logger.info(f"🖼️ 开始回填最新一期图片: {data_path.name}（{len(articles)} 条）")
    with httpx.Client(timeout=httpx.Timeout(8, connect=5), follow_redirects=False) as image_client:
        for index, article in enumerate(articles, 1):
            article_id = article.get("id")
            if article_id:
                article_by_id[article_id] = article

            current_image = article.get("image")
            if current_image and article.get("image_validated"):
                stats["kept"] += 1
                continue

            validated_image = None
            if current_image:
                validated_image = _validate_image_url(image_client, current_image)

            if validated_image:
                article["image"] = validated_image
                article["image_validated"] = True
                stats["kept"] += 1
                continue

            article_url = article.get("link", "")
            replacement, image_validated = _resolve_article_image(
                client=image_client,
                entry=None,
                article_url=article_url,
                feed_url=article_url,
            )
            if replacement:
                article["image"] = replacement
                article["image_validated"] = image_validated
                stats["replaced" if current_image else "added"] += 1
            else:
                article["image"] = None
                article["image_validated"] = False
                stats["missing"] += 1

            logger.info(
                f"  图片回填 ({index}/{len(articles)}): "
                f"{article.get('source', '未知来源')} - {'成功' if article.get('image') else '无可用图'}"
            )

    for cluster in data.get("clusters", []):
        grouped_articles = [
            article_by_id[article_id]
            for article_id in cluster.get("article_ids", [])
            if article_id in article_by_id
        ]
        if grouped_articles:
            cluster["image"] = _select_cluster_image(grouped_articles)

    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.info(
        "✅ 图片回填完成：保留/验证 %(kept)s，新增 %(added)s，替换 %(replaced)s，仍缺 %(missing)s",
        stats,
    )
    return {"file": data_path.name, **stats}


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

    # 3. 抓取所有新闻源
    all_raw = []
    for cat in categories:
        logger.info(f"\n📰 分类: {cat['name']} ({cat['id']})")
        for source in cat.get("sources", []):
            articles = fetch_source(source, cat["id"], excluded_ids=existing_ids)
            # 双重去重，兼容特殊来源在解析后改变链接的情况
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
    parser = argparse.ArgumentParser(description="Woody News 新闻采集与图片维护")
    parser.add_argument(
        "--backfill-images",
        choices=["latest"],
        help="只回填指定范围的图片，不调用翻译、摘要或主题聚类模型",
    )
    args = parser.parse_args()
    if args.backfill_images == "latest":
        backfill_latest_images()
    else:
        main()
