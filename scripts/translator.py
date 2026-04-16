"""
Woody News — AI 翻译 & 摘要模块
使用火山引擎大模型 API（兼容 OpenAI 接口）
"""

import json
import os
import logging
from openai import OpenAI

logger = logging.getLogger(__name__)

# 初始化火山引擎 API 客户端
API_KEY = os.environ.get("VOLCENGINE_API_KEY", "")

client = OpenAI(
    api_key=API_KEY,
    base_url=os.environ.get("VOLCENGINE_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
)

MODEL = os.environ.get("VOLCENGINE_MODEL", "doubao-pro-256k")
HAS_MODEL_ACCESS = bool(API_KEY)


def _call_json_model(prompt: str, max_tokens: int = 500, temperature: float = 0.3) -> dict:
    """调用模型并解析 JSON 返回"""
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    content = (response.choices[0].message.content or "").strip()
    return _extract_json(content)


def translate_and_summarize(title: str, description: str, lang: str) -> dict:
    """
    根据原始语言进行翻译和摘要。
    - 英文新闻：翻译标题 + 生成中文总结
    - 中文新闻：生成精炼总结
    - 标题型来源：在正文缺失时仅基于标题生成保守摘要

    返回:
        {
            "title": "中文标题",
            "title_original": "原标题（仅英文）",
            "summary": "中文摘要总结",
            "summary_original": "原摘要（仅英文）"
        }
    """
    description = (description or "").strip()
    if lang == "en":
        if not description:
            return _process_english_title_only(title)
        return _process_english(title, description)
    if not description:
        return {
            "title": title,
            "title_original": None,
            "summary": title,
            "summary_original": None,
        }
    return _process_chinese(title, description)


def _process_english(title: str, description: str) -> dict:
    """处理英文新闻：翻译 + 总结"""
    prompt = f"""你是一个专业的新闻编辑。请完成以下任务：
1. 将英文标题翻译成简洁准确的中文
2. 基于原文内容，用中文写一段 80-120 字的新闻摘要总结，要求涵盖核心事实，语言精炼流畅。不要使用"本文"、"该文"等指代词。

标题：{title}
内容：{description}

请严格以 JSON 格式返回，不要包含其他内容：
{{"title": "中文标题", "summary": "中文摘要总结"}}"""

    try:
        result = _call_json_model(prompt, max_tokens=500, temperature=0.3)
        return {
            "title": result.get("title", title),
            "title_original": title,
            "summary": result.get("summary", description[:200]),
            "summary_original": description[:500] if description else None,
        }
    except Exception as e:
        logger.error(f"翻译英文新闻失败: {e}")
        return {
            "title": title,
            "title_original": title,
            "summary": description[:200] if description else "",
            "summary_original": description[:500] if description else None,
        }


def _process_english_title_only(title: str) -> dict:
    """处理仅有标题的英文新闻：保守翻译并生成简短摘要"""
    prompt = f"""你是一个专业的新闻编辑。当前只有新闻标题，没有正文或摘要。
请完成以下任务：
1. 将英文标题翻译成简洁准确的中文。
2. 仅基于标题中明确出现的信息，用中文写一段 50-80 字的保守摘要。
3. 不要补充标题中没有出现的细节，不要猜测原因、影响或背景。

标题：{title}

请严格以 JSON 格式返回，不要包含其他内容：
{{"title": "中文标题", "summary": "中文摘要总结"}}"""

    try:
        result = _call_json_model(prompt, max_tokens=260, temperature=0.2)
        summary = result.get("summary") or result.get("title") or title
        return {
            "title": result.get("title", title),
            "title_original": title,
            "summary": summary,
            "summary_original": None,
        }
    except Exception as e:
        logger.error(f"处理标题型英文新闻失败: {e}")
        return {
            "title": title,
            "title_original": title,
            "summary": title,
            "summary_original": None,
        }



def _process_chinese(title: str, description: str) -> dict:
    """处理中文新闻：生成总结"""
    prompt = f"""你是一个专业的新闻编辑。请基于以下新闻内容，写一段 80-120 字的摘要总结，涵盖核心事实，语言精炼。不要使用"本文"、"该文"等指代词。

标题：{title}
内容：{description}

请严格以 JSON 格式返回，不要包含其他内容：
{{"summary": "摘要总结"}}"""

    try:
        result = _call_json_model(prompt, max_tokens=300, temperature=0.3)
        return {
            "title": title,
            "title_original": None,
            "summary": result.get("summary", description[:200]),
            "summary_original": None,
        }
    except Exception as e:
        logger.error(f"生成中文摘要失败: {e}")
        return {
            "title": title,
            "title_original": None,
            "summary": description[:200] if description else "",
            "summary_original": None,
        }


def judge_same_topic(article_a: dict, article_b: dict) -> dict:
    """判断两条新闻是否为同一主题"""
    if not HAS_MODEL_ACCESS:
        return {"same_topic": False, "confidence": 0.0, "reason": "missing_api_key"}

    prompt = f"""你是新闻编辑，判断下面两条新闻是否在报道同一件事情/同一主题事件。

判断标准：
1. 必须是同一事件、同一产品发布、同一公司动作、同一政策或同一事故，才算同主题。
2. 仅仅属于同一大领域（比如都讲 AI、都讲苹果、都讲特朗普）不算同主题。
3. 如果一条是后续解读、另一条是原始事件，但核心对象和核心事件完全一致，可以算同主题。
4. 请严格保守，拿不准就返回 false。

新闻 A：
- 标题：{article_a.get("title")}
- 原标题：{article_a.get("title_original")}
- 摘要：{article_a.get("summary")}
- 来源：{article_a.get("source")}
- 分类：{article_a.get("category")}
- 时间：{article_a.get("published_at")}

新闻 B：
- 标题：{article_b.get("title")}
- 原标题：{article_b.get("title_original")}
- 摘要：{article_b.get("summary")}
- 来源：{article_b.get("source")}
- 分类：{article_b.get("category")}
- 时间：{article_b.get("published_at")}

请只返回 JSON：
{{"same_topic": true, "confidence": 0.92, "reason": "简短原因"}}"""

    try:
        result = _call_json_model(prompt, max_tokens=220, temperature=0.1)
        return {
            "same_topic": bool(result.get("same_topic", False)),
            "confidence": float(result.get("confidence", 0) or 0),
            "reason": result.get("reason", ""),
        }
    except Exception as e:
        logger.error(f"判断新闻是否同主题失败: {e}")
        return {"same_topic": False, "confidence": 0.0, "reason": "model_error"}


def summarize_cluster(articles: list[dict]) -> dict:
    """为同主题聚合新闻生成统一标题和摘要"""
    if not articles:
        return {"title": "", "summary": ""}

    if len(articles) == 1 or not HAS_MODEL_ACCESS:
        article = articles[0]
        return {"title": article.get("title", ""), "summary": article.get("summary", "")}

    bullet_list = []
    for idx, article in enumerate(articles[:5], 1):
        bullet_list.append(
            f"{idx}. 标题：{article.get('title')}\n"
            f"   原标题：{article.get('title_original')}\n"
            f"   摘要：{article.get('summary')}\n"
            f"   来源：{article.get('source')}"
        )

    prompt = """你是新闻编辑，现在需要把多家媒体对同一主题的报道合并成一个主题卡片。
请输出：
1. 一个统一的中文标题，20-34 字，简洁、信息密度高，不要写成标题党。
2. 一段统一中文摘要，80-120 字，概括多家媒体共同关注的核心事实，不要写“多家媒体报道”这类空话。

相关新闻：
""" + "\n\n".join(bullet_list) + """

请只返回 JSON：
{"title": "统一标题", "summary": "统一摘要"}"""

    try:
        result = _call_json_model(prompt, max_tokens=260, temperature=0.2)
        latest = articles[0]
        return {
            "title": result.get("title", latest.get("title", "")),
            "summary": result.get("summary", latest.get("summary", "")),
        }
    except Exception as e:
        logger.error(f"生成聚合新闻摘要失败: {e}")
        latest = articles[0]
        return {
            "title": latest.get("title", ""),
            "summary": latest.get("summary", ""),
        }


def _extract_json(text: str) -> dict:
    """从大模型返回的文本中提取 JSON"""
    # 先尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试从 markdown 代码块中提取
    if "```" in text:
        start = text.find("```")
        end = text.rfind("```")
        if start != end:
            code = text[start:end].split("\n", 1)[-1]
            try:
                return json.loads(code)
            except json.JSONDecodeError:
                pass

    # 尝试查找 { } 包裹的内容
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    return {}
