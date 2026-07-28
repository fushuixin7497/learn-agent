#!/usr/bin/env python3
"""
模块 6：Graceful Degradation —— 健壮工具 + 不可用模拟
========================================================
在 lesson-04 工具基础上：
1. 保留所有基础工具与幂等设计。
2. 新增 external_api 工具，用于演示"工具不可用时代理换路径"。
3. execute_tool 继续遵循"错误作为正常返回值"原则，保证 loop 不 crash。
"""

import os
import json
import random
from typing import Callable, Dict, List, Any


def calculator(expression: str) -> str:
    """执行数学计算。支持 + - * / 和括号。"""
    if not isinstance(expression, str):
        return "错误：expression 必须是字符串"
    if len(expression) > 200:
        return "错误：表达式过长（限制 200 字符）"

    allowed = set("0123456789.+-*/() ")
    if not all(c in allowed for c in expression):
        invalid = [c for c in expression if c not in allowed]
        return f"错误：表达式包含非法字符 {set(invalid)}，只允许数字和 +-*/()"

    try:
        result = eval(expression, {"__builtins__": {}}, {})
    except ZeroDivisionError:
        return "错误：除零"
    except Exception as e:
        return f"计算错误: {type(e).__name__}: {e}"

    return f"计算结果：{expression} = {result}"


def read_file(filepath: str, offset: int = 1, limit: int = 20) -> str:
    """读取本地文本文件内容。可指定 offset 和 limit（最大 30）。"""
    if not isinstance(filepath, str) or not filepath:
        return "错误：filepath 必须是非空字符串"
    if not isinstance(offset, int) or offset < 1:
        return "错误：offset 必须是 >=1 的整数"
    if not isinstance(limit, int) or limit < 1 or limit > 500:
        return "错误：limit 必须是 1~500 的整数"

    if limit > 30:
        limit = 30

    abs_path = os.path.abspath(os.path.expanduser(filepath))
    home = os.path.expanduser("~")

    if not abs_path.startswith(home):
        return f"错误：路径 '{filepath}' 超出允许范围（只能读取用户主目录下的文件）"

    if not os.path.exists(abs_path):
        return f"错误：文件不存在 '{abs_path}'"
    if not os.path.isfile(abs_path):
        return f"错误：路径不是文件 '{abs_path}'"

    size = os.path.getsize(abs_path)
    if size > 5 * 1024 * 1024:
        return f"错误：文件过大（{size} 字节，限制 5MB）"

    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"读取错误: {type(e).__name__}: {e}"

    total_lines = len(lines)
    start = offset - 1
    end = start + limit

    if start >= total_lines:
        return f"提示：文件共 {total_lines} 行，offset={offset} 超出范围"

    selected = lines[start:end]
    content = "".join(selected)
    indicator = f"（第 {offset}~{min(end, total_lines)} 行 / 共 {total_lines} 行）"
    return f"{indicator}\n```\n{content}\n```"


def write_file(filepath: str, content: str, overwrite: bool = False) -> str:
    """写入文本到本地文件，支持幂等和覆盖控制。"""
    if not isinstance(filepath, str) or not filepath:
        return "错误：filepath 必须是非空字符串"
    if not isinstance(content, str):
        return "错误：content 必须是字符串"

    abs_path = os.path.abspath(os.path.expanduser(filepath))
    home = os.path.expanduser("~")

    if not abs_path.startswith(home):
        return f"错误：路径 '{filepath}' 超出允许范围"

    if os.path.exists(abs_path):
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                existing = f.read()
        except Exception as e:
            return f"错误：无法读取已存在文件: {e}"

        if existing == content:
            return f"成功（幂等）：文件 '{filepath}' 已存在且内容完全一致，无需重复写入。"

        if not overwrite:
            return (
                f"错误：文件 '{filepath}' 已存在且内容不同（现有 {len(existing)} 字符，"
                f"欲写入 {len(content)} 字符）。如需覆盖，请设置 overwrite=true 再调用一次。"
            )

    try:
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return f"写入错误: {type(e).__name__}: {e}"

    return f"成功：已写入文件 '{filepath}'，共 {len(content)} 字符。"


def generate_long_text(paragraphs: int = 50) -> str:
    """生成超长文本，用于测试 context 管理能力。"""
    if not isinstance(paragraphs, int):
        return "错误：paragraphs 必须是整数"
    if paragraphs < 1 or paragraphs > 200:
        return "错误：paragraphs 必须在 1~200 之间"

    topics = [
        "人工智能在医疗诊断中的应用", "区块链技术如何保障数据安全",
        "量子计算的基本原理与挑战", "自动驾驶汽车的环境感知系统",
        "机器学习模型的过拟合问题", "云计算架构的弹性伸缩设计",
    ]

    lines = []
    for i in range(1, paragraphs + 1):
        topic = topics[(i - 1) % len(topics)]
        lines.append(
            f"第 {i:03d} 段：{topic}。"
            f"本段编号为第 {i} 段，用于测试 context 窗口管理。"
        )
    return "\n\n".join(lines)


def search_files(directory: str, keyword: str) -> str:
    """在指定目录下递归搜索文件名包含关键字的文件。"""
    if not isinstance(directory, str) or not directory:
        return "错误：directory 必须是非空字符串"
    if not isinstance(keyword, str) or not keyword:
        return "错误：keyword 必须是非空字符串"

    abs_dir = os.path.abspath(os.path.expanduser(directory))
    home = os.path.expanduser("~")
    if not abs_dir.startswith(home):
        return f"错误：目录 '{directory}' 超出允许范围"

    if not os.path.isdir(abs_dir):
        return f"错误：目录不存在 '{abs_dir}'"

    matches = []
    try:
        for root, _, files in os.walk(abs_dir):
            for fname in files:
                if keyword.lower() in fname.lower():
                    matches.append(os.path.join(root, fname))
                if len(matches) >= 20:
                    break
            if len(matches) >= 20:
                break
    except Exception as e:
        return f"搜索错误: {type(e).__name__}: {e}"

    if not matches:
        return f"未找到：在 '{directory}' 中没有文件名包含 '{keyword}' 的文件"

    lines = "\n".join(matches)
    return f"找到 {len(matches)} 个结果：\n{lines}"


def external_api(query: str) -> str:
    """
    【教学工具】模拟一个不稳定的外部 API。
    默认 50% 概率返回 503 不可用，用于演示 Agent 在工具不可用时的换路径能力。
    当不可用返回时，Agent 应改用本地工具（如 search_files / read_file）完成任务。
    """
    if not isinstance(query, str) or not query:
        return "错误：query 必须是非空字符串"

    # 教学演示：随机不可用
    if random.random() < 0.5:
        return (
            f"错误：外部 API 服务暂时不可用（503 Service Unavailable），"
            f"无法查询 '{query}'。请改用本地工具完成。"
        )

    return f"✅ 外部 API 返回结果：关于 '{query}' 的查询成功（模拟数据）。"


# ============================================================
# 工具注册表 + Schema 自动生成
# ============================================================
ToolFunc = Callable[..., str]

TOOL_REGISTRY: Dict[str, ToolFunc] = {
    "calculator": calculator,
    "read_file": read_file,
    "write_file": write_file,
    "generate_long_text": generate_long_text,
    "search_files": search_files,
    "external_api": external_api,
}

ENABLE_MISLEADING_TOOL = False


def get_tools() -> List[Dict[str, Any]]:
    """返回符合 OpenAI Function Calling 协议的 tools 数组。"""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "calculator",
                "description": "执行数学计算。支持 + - * / 和括号，例如 '365*24'。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "数学表达式，例如 '365 * 24'",
                        }
                    },
                    "required": ["expression"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取本地文本文件的内容。可指定 offset 和 limit（最大 30）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filepath": {
                            "type": "string",
                            "description": "文件路径",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "起始行号，从 1 开始，默认 1",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "读取行数，默认 20，最大 30",
                        },
                    },
                    "required": ["filepath"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": (
                    "写入文本到本地文件。如果文件已存在且内容一致，不会重复写入。"
                    "如需覆盖，设置 overwrite=true。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filepath": {"type": "string", "description": "文件路径"},
                        "content": {"type": "string", "description": "要写入的内容"},
                        "overwrite": {"type": "boolean", "description": "是否覆盖"},
                    },
                    "required": ["filepath", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "generate_long_text",
                "description": "生成超长文本，默认 50 段，最大 200。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paragraphs": {
                            "type": "integer",
                            "description": "生成段数",
                        }
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_files",
                "description": "在指定目录下递归搜索文件名包含关键字的文件，最多 20 个结果。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "directory": {"type": "string", "description": "搜索目录"},
                        "keyword": {"type": "string", "description": "文件名关键字"},
                    },
                    "required": ["directory", "keyword"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "external_api",
                "description": (
                    "调用外部 API 查询信息。注意：该服务可能不可用，"
                    "如果返回 503 错误，请改用本地工具（search_files/read_file）完成。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "查询内容"},
                    },
                    "required": ["query"],
                },
            },
        },
    ]

    if ENABLE_MISLEADING_TOOL:
        tools.append({
            "type": "function",
            "function": {
                "name": "do_something",
                "description": "处理某个路径。",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        })

    return tools


def execute_tool(name: str, arguments: dict) -> str:
    """
    规范：错误作为正常返回值，避免 loop 崩溃。
    """
    if name not in TOOL_REGISTRY:
        return f"错误：未知工具 '{name}'。可用工具: {list(TOOL_REGISTRY.keys())}"

    func = TOOL_REGISTRY[name]
    try:
        result = func(**arguments)
    except Exception as e:
        return f"工具执行异常 ({name}): {type(e).__name__}: {e}"

    if not isinstance(result, str):
        result = str(result)

    return result
