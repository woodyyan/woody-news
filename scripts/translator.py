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
client = OpenAI(
    api_key=os.environ.get("VOLCENGINE_API_KEY", ""),
    base_url=os.environ.get("VOLCENGINE_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
)

MODEL = os.environ.get("VOLCENGINE_MODEL", "doubao-pro-256k")


def translate_and_summarize(title: str, description: str, lang: str) -> dict:
    """
    根据原始语言进行翻译和摘要。
    - 英文新闻：翻译标题 + 生成中文总结
    - 中文新闻：生成精炼总结

    返回:
        {
            "title": "中文标题",
            "title_original": "原标题（仅英文）",
            "summary": "中文摘要总结",
            "summary_original": "原摘要（仅英文）"
        }
    """
    if lang == "en":
        return _process_english(title, description)
    else:
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
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
        )
        content = response.choices[0].message.content.strip()
        # 尝试提取 JSON
        result = _extract_json(content)
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


def _process_chinese(title: str, description: str) -> dict:
    """处理中文新闻：生成总结"""
    prompt = f"""你是一个专业的新闻编辑。请基于以下新闻内容，写一段 80-120 字的摘要总结，涵盖核心事实，语言精炼。不要使用"本文"、"该文"等指代词。

标题：{title}
内容：{description}

请严格以 JSON 格式返回，不要包含其他内容：
{{"summary": "摘要总结"}}"""

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=300,
        )
        content = response.choices[0].message.content.strip()
        result = _extract_json(content)
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
