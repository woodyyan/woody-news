import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fetch_news


class FakeStreamResponse:
    def __init__(self, url, content_type, data, status_code=200, content_length=None):
        self.url = httpx.URL(url)
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self._data = data
        self.encoding = "utf-8"
        self.is_redirect = 300 <= status_code < 400

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def raise_for_status(self):
        request = httpx.Request("GET", self.url)
        response = httpx.Response(self.status_code, request=request)
        response.raise_for_status()

    def iter_bytes(self, chunk_size=None):
        yield self._data


class ImageExtractionTests(unittest.TestCase):
    def test_extracts_largest_srcset_from_content_encoded(self):
        entry = {
            "content": [
                {
                    "value": (
                        '<p>正文</p><img src="small.jpg" '
                        'srcset="small.jpg 320w, /large.jpg 1280w">'
                    )
                }
            ]
        }

        candidates = fetch_news._extract_image_candidates(entry)

        self.assertEqual(candidates[0], "/large.jpg")
        self.assertIn("small.jpg", candidates)

    def test_article_metadata_priority_is_open_graph_then_twitter_then_json_ld(self):
        html = """
        <meta property="og:image" content="/og.jpg">
        <meta name="twitter:image" content="https://cdn.example.com/twitter.jpg">
        <script type="application/ld+json">
        {"@type":"NewsArticle","image":{"url":"https://cdn.example.com/json.jpg"}}
        </script>
        <img src="body.jpg">
        """

        candidates = fetch_news._parse_html_image_candidates(html)

        self.assertEqual(
            candidates,
            [
                "/og.jpg",
                "https://cdn.example.com/twitter.jpg",
                "https://cdn.example.com/json.jpg",
                "body.jpg",
            ],
        )

    def test_invalid_json_ld_does_not_break_html_image_extraction(self):
        html = '<script type="application/ld+json">{invalid}</script><img src="body.jpg">'

        self.assertEqual(fetch_news._parse_html_image_candidates(html), ["body.jpg"])

    def test_extracts_base_href_and_picture_source(self):
        html = '<base href="https://cdn.example.com/assets/"><picture><source srcset="hero.webp 2x"></picture>'

        candidates, base_href = fetch_news._parse_html_image_metadata(html)

        self.assertEqual(base_href, "https://cdn.example.com/assets/")
        self.assertEqual(candidates, ["hero.webp"])

    def test_non_numeric_media_dimensions_are_tolerated(self):
        entry = {
            "media_content": [
                {"url": "https://cdn.example.com/news.jpg", "type": "image/jpeg", "width": "auto"}
            ]
        }

        self.assertEqual(
            fetch_news._extract_image_candidates(entry),
            ["https://cdn.example.com/news.jpg"],
        )


class ImageValidationTests(unittest.TestCase):
    def setUp(self):
        self.original_safe_request_url = fetch_news._safe_request_url
        fetch_news._safe_request_url = lambda url: url

    def tearDown(self):
        fetch_news._safe_request_url = self.original_safe_request_url

    @staticmethod
    def png_header(width, height):
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", width, height)

    @staticmethod
    def jpeg_header(width, height):
        sof = b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", height, width) + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
        return b"\xff\xd8" + sof + b"\xff\xd9"

    def test_accepts_valid_large_image(self):
        client = Mock()
        client.stream.return_value = FakeStreamResponse(
            "https://cdn.example.com/news.png",
            "image/png",
            self.png_header(1200, 675),
        )

        result = fetch_news._validate_image_url(client, "https://cdn.example.com/news.png")

        self.assertEqual(result, "https://cdn.example.com/news.png")

    def test_accepts_image_jpg_mime_alias(self):
        client = Mock()
        client.stream.return_value = FakeStreamResponse(
            "https://cdn.example.com/news.jpg",
            "image/jpg",
            self.jpeg_header(1200, 675),
        )

        result = fetch_news._validate_image_url(client, "https://cdn.example.com/news.jpg")

        self.assertEqual(result, "https://cdn.example.com/news.jpg")

    def test_rejects_small_image(self):
        client = Mock()
        client.stream.return_value = FakeStreamResponse(
            "https://cdn.example.com/icon.png",
            "image/png",
            self.png_header(100, 100),
        )

        result = fetch_news._validate_image_url(client, "https://cdn.example.com/icon.png")

        self.assertIsNone(result)

    def test_rejects_non_image_content_type(self):
        client = Mock()
        client.stream.return_value = FakeStreamResponse(
            "https://cdn.example.com/login.jpg",
            "text/html",
            b"<html>login</html>",
        )

        result = fetch_news._validate_image_url(client, "https://cdn.example.com/login.jpg")

        self.assertIsNone(result)

    def test_rejects_oversized_image_from_content_length(self):
        client = Mock()
        client.stream.return_value = FakeStreamResponse(
            "https://cdn.example.com/huge.jpg",
            "image/jpeg",
            b"\xff\xd8",
            content_length=fetch_news.MAX_IMAGE_BYTES + 1,
        )

        result = fetch_news._validate_image_url(client, "https://cdn.example.com/huge.jpg")

        self.assertIsNone(result)

    def test_network_error_is_non_fatal(self):
        client = Mock()
        client.stream.side_effect = httpx.ConnectError("offline")

        result = fetch_news._validate_image_url(client, "https://cdn.example.com/news.jpg")

        self.assertIsNone(result)

    def test_rejects_private_network_url(self):
        fetch_news._safe_request_url = self.original_safe_request_url
        client = Mock()

        result = fetch_news._validate_image_url(client, "http://127.0.0.1/internal.png")

        self.assertIsNone(result)
        client.stream.assert_not_called()

    def test_rejects_malformed_image_with_image_mime(self):
        client = Mock()
        client.stream.return_value = FakeStreamResponse(
            "https://cdn.example.com/fake.jpg",
            "image/jpeg",
            b"not-a-real-image",
        )

        result = fetch_news._validate_image_url(client, "https://cdn.example.com/fake.jpg")

        self.assertIsNone(result)


class LatestImageBackfillTests(unittest.TestCase):
    def test_backfill_latest_updates_articles_and_cluster_without_ai_calls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            data_path = data_dir / "2026-04-30-morning.json"
            index_path = data_dir / "index.json"
            index_path.write_text(json.dumps({"latest": "2026-04-30-morning"}), encoding="utf-8")
            data_path.write_text(
                json.dumps(
                    {
                        "articles": [
                            {
                                "id": "article-1",
                                "source": "Example",
                                "link": "https://example.com/story",
                                "image": None,
                            }
                        ],
                        "clusters": [{"article_ids": ["article-1"], "image": None}],
                    }
                ),
                encoding="utf-8",
            )

            original_data_dir = fetch_news.DATA_DIR
            original_index_path = fetch_news.INDEX_PATH
            original_resolve = fetch_news._resolve_article_image
            original_client = fetch_news.httpx.Client
            try:
                fetch_news.DATA_DIR = data_dir
                fetch_news.INDEX_PATH = index_path
                fetch_news._resolve_article_image = Mock(
                    return_value=("https://cdn.example.com/hero.jpg", True)
                )
                mock_client = Mock()
                mock_client.__enter__ = Mock(return_value=mock_client)
                mock_client.__exit__ = Mock(return_value=False)
                fetch_news.httpx.Client = Mock(return_value=mock_client)

                stats = fetch_news.backfill_latest_images()
            finally:
                fetch_news.DATA_DIR = original_data_dir
                fetch_news.INDEX_PATH = original_index_path
                fetch_news._resolve_article_image = original_resolve
                fetch_news.httpx.Client = original_client

            updated = json.loads(data_path.read_text(encoding="utf-8"))
            self.assertEqual(stats["added"], 1)
            self.assertEqual(updated["articles"][0]["image"], "https://cdn.example.com/hero.jpg")
            self.assertTrue(updated["articles"][0]["image_validated"])
            self.assertEqual(updated["clusters"][0]["image"], "https://cdn.example.com/hero.jpg")


class ArticleImageResolutionTests(unittest.TestCase):
    def test_falls_back_to_article_open_graph_image(self):
        client = Mock()
        entry = {"summary": "No image here"}
        original_fetch = fetch_news._fetch_article_html
        original_validate = fetch_news._validate_image_url
        try:
            fetch_news._fetch_article_html = Mock(
                return_value=(
                    '<meta property="og:image" content="/hero.jpg">',
                    "https://example.com/story",
                )
            )
            fetch_news._validate_image_url = Mock(return_value="https://example.com/hero.jpg")

            image, validated = fetch_news._resolve_article_image(
                client,
                entry,
                "https://example.com/story",
                "https://example.com/feed.xml",
            )
        finally:
            fetch_news._fetch_article_html = original_fetch
            fetch_news._validate_image_url = original_validate

        self.assertEqual(image, "https://example.com/hero.jpg")
        self.assertTrue(validated)

    def test_cluster_prefers_validated_image(self):
        articles = [
            {"image": "https://example.com/old.jpg", "image_validated": False},
            {"image": "https://example.com/good.jpg", "image_validated": True},
        ]

        self.assertEqual(
            fetch_news._select_cluster_image(articles),
            "https://example.com/good.jpg",
        )


if __name__ == "__main__":
    unittest.main()
