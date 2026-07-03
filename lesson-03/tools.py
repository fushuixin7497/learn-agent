#!/usr/bin/env python3
"""
模块 2：工具设计 —— 健壮工具规范与实现
=======================================
设计原则（每一条都在代码中体现）：

1. 工具 description 本身就是 prompt
   → 写得越具体、越带示例，模型调用越准确。

2. 参数校验必须在工具内进行
   → 不能信任模型传来的参数，模型可能传错类型、越界值、恶意输入。

3. 返回值要利于模型继续决策
   → 返回结果要包含"成功/失败"状态、关键数据、下一步提示。

4. 错误作为正常返回值，避免 loop 崩溃
   → 永远不要抛异常让 Agent Loop 中断。所有错误转成字符串返回。

5. 有副作用工具要考虑幂等
   → 同样参数的重复调用，不应该产生不同的副作用。
     例如：write_file 发现文件已存在且内容一致，应返回"无需重复写入"。
"""

import os
import json
import subprocess
from typing import Callable, Dict, List, Any


# ============================================================
# 工具规范 1：description 本身就是 prompt
# ------------------------------------------------------------
# 好的 description = 角色定义 + 使用场景 + 参数示例 + 边界说明
# 模糊的 description = 模型乱调用 + 参数传错 + 无限循环
# ============================================================


def calculator(expression: str) -> str:
    """
    【规范示例】清晰、具体的 description：
    "执行数学计算。支持 + - * / 和括号，例如 '365*24' 或 '(1+2)*3'。
     注意：不支持变量、函数调用、字符串操作。"
    """
    # 规范 2：参数校验必须在工具内
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

    # 规范 3：返回值利于模型继续决策
    # 不仅返回数字，还返回计算式，方便模型确认自己没调错
    return f"计算结果：{expression} = {result}"


def read_file(filepath: str, offset: int = 1, limit: int = 100) -> str:
    """
    【规范示例】参数带约束说明：
    "读取本地文本文件内容。可指定起始行 offset 和读取行数 limit（最大 500 行）。
     适用于查看代码、日志、配置文件。不支持读取二进制文件。"
    """
    # 参数校验
    if not isinstance(filepath, str) or not filepath:
        return "错误：filepath 必须是非空字符串"
    if not isinstance(offset, int) or offset < 1:
        return "错误：offset 必须是 >=1 的整数"
    if not isinstance(limit, int) or limit < 1 or limit > 500:
        return "错误：limit 必须是 1~500 的整数"

    abs_path = os.path.abspath(os.path.expanduser(filepath))
    home = os.path.expanduser("~")

    # 路径安全：禁止读取主目录外的敏感路径
    if not abs_path.startswith(home):
        return f"错误：路径 '{filepath}' 超出允许范围（只能读取用户主目录下的文件）"

    if not os.path.exists(abs_path):
        return f"错误：文件不存在 '{abs_path}'"
    if not os.path.isfile(abs_path):
        return f"错误：路径不是文件 '{abs_path}'"

    # 文件大小检查：拒绝读取过大文件（如视频、二进制）
    size = os.path.getsize(abs_path)
    if size > 5 * 1024 * 1024:  # 5MB
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

    # 返回值包含元信息，帮助模型决策
    indicator = f"（第 {offset}~{min(end, total_lines)} 行 / 共 {total_lines} 行）"
    return f"{indicator}\n```\n{content}\n```"


def write_file(filepath: str, content: str, overwrite: bool = False) -> str:
    """
    【规范示例】有副作用工具的幂等设计 + 参数说明：
    "写入文本到本地文件。如果文件已存在且内容完全一致，不会重复写入（幂等）。
     如果文件已存在但内容不同，默认拒绝写入，可设置 overwrite=true 强制覆盖。
     会自动创建不存在的父目录。"
    """
    if not isinstance(filepath, str) or not filepath:
        return "错误：filepath 必须是非空字符串"
    if not isinstance(content, str):
        return "错误：content 必须是字符串"

    abs_path = os.path.abspath(os.path.expanduser(filepath))
    home = os.path.expanduser("~")

    if not abs_path.startswith(home):
        return f"错误：路径 '{filepath}' 超出允许范围"

    # 规范 5：幂等性检查
    if os.path.exists(abs_path):
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                existing = f.read()
        except Exception as e:
            return f"错误：无法读取已存在文件: {e}"

        if existing == content:
            # 幂等：同样的输入，同样的结果，不产生新副作用
            return f"成功（幂等）：文件 '{filepath}' 已存在且内容完全一致，无需重复写入。"

        if not overwrite:
            # 给模型明确的下一步提示
            return (
                f"错误：文件 '{filepath}' 已存在且内容不同（现有 {len(existing)} 字符，"
                f"欲写入 {len(content)} 字符）。"
                f"如需覆盖，请设置 overwrite=true 再调用一次。"
            )

    # 执行写入
    try:
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return f"写入错误: {type(e).__name__}: {e}"

    return f"成功：已写入文件 '{filepath}'，共 {len(content)} 字符。"


def search_files(directory: str, keyword: str) -> str:
    """
    在指定目录下递归搜索文件名包含关键字的文件。
    返回最多 20 个匹配结果的路径列表。
    """
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


# ============================================================
# 反面教材：模糊 description 导致误用
# ------------------------------------------------------------
# 这个工具故意写得含糊不清，模型很难判断该什么时候用它。
# 结果：模型可能把 "读取文件"、"搜索文件"、"执行命令" 的任务都丢给它。
# ============================================================

def do_something(path: str) -> str:
    """
    【反面教材】模糊的 description：
    "处理某个路径。"
    
    问题：
    - "处理" 是什么意思？读取？写入？删除？执行？
    - 返回值是什么？模型不知道该怎么用它。
    - 没有参数说明，模型可能传目录、传 URL、传任意字符串。
    """
    return f"对 '{path}' 执行了某种操作（但你永远不知道具体是什么）。"


# ============================================================
# 工具注册表 + Schema 自动生成
# ------------------------------------------------------------
# 生产环境中可以用 inspect + typing 自动生成 schema，
# 这里为了教学清晰，手写 schema，让你看到每个字段怎么影响模型行为。
# ============================================================

ToolFunc = Callable[..., str]

TOOL_REGISTRY: Dict[str, ToolFunc] = {
    "calculator": calculator,
    "read_file": read_file,
    "write_file": write_file,
    "search_files": search_files,
    # 反面教材默认不注册，需要手动开启才能观察误用
    # "do_something": do_something,
}

# 为教学演示，提供一个"误用模式"开关
ENABLE_MISLEADING_TOOL = True


def get_tools() -> List[Dict[str, Any]]:
    """返回符合 OpenAI Function Calling 协议的 tools 数组。"""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "calculator",
                "description": (
                    "执行数学计算。支持 + - * / 和括号，例如 '365*24' 或 '(1+2)*3'。"
                    "注意：不支持变量、函数调用、字符串操作。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "数学表达式，例如 '365 * 24' 或 '(1+2)*3'",
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
                "description": (
                    "读取本地文本文件的内容。可指定起始行 offset 和读取行数 limit（最大 500 行）。"
                    "适用于查看代码、日志、配置文件。不支持读取二进制文件。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filepath": {
                            "type": "string",
                            "description": "文件路径，例如 '/Users/albert/learn-agent/lesson-01/chat.py' 或 '~/Downloads/file.txt'",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "起始行号，从 1 开始，默认 1",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "读取行数，默认 100，最大 500",
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
                    "写入文本到本地文件。如果文件已存在且内容完全一致，不会重复写入（幂等）。"
                    "如果文件已存在但内容不同，默认拒绝写入，可设置 overwrite=true 强制覆盖。"
                    "会自动创建不存在的父目录。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filepath": {
                            "type": "string",
                            "description": "文件路径，例如 '/Users/albert/notes.txt' 或 '~/todo.md'",
                        },
                        "content": {
                            "type": "string",
                            "description": "要写入的完整文本内容",
                        },
                        "overwrite": {
                            "type": "boolean",
                            "description": "是否覆盖已存在的文件，默认 false",
                        },
                    },
                    "required": ["filepath", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_files",
                "description": (
                    "在指定目录下递归搜索文件名包含关键字的文件。"
                    "返回最多 20 个匹配结果的路径列表。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "directory": {
                            "type": "string",
                            "description": "搜索目录，例如 '/Users/albert/learn-agent' 或 '~/'",
                        },
                        "keyword": {
                            "type": "string",
                            "description": "文件名关键字，例如 '.py' 或 'README'",
                        },
                    },
                    "required": ["directory", "keyword"],
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
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "路径",
                        }
                    },
                    "required": ["path"],
                },
            },
        })

    return tools


def execute_tool(name: str, arguments: dict) -> str:
    """
    规范 4：错误作为正常返回值，避免 loop 崩溃。
    这个函数封装了所有异常处理，确保 Agent Loop 永远不会因为工具抛异常而中断。
    """
    if name not in TOOL_REGISTRY:
        return f"错误：未知工具 '{name}'。可用工具: {list(TOOL_REGISTRY.keys())}"

    func = TOOL_REGISTRY[name]

    try:
        result = func(**arguments)
    except Exception as e:
        # 捕获所有异常，转成字符串返回
        return f"工具执行异常 ({name}): {type(e).__name__}: {e}"

    # 确保返回值是字符串
    if not isinstance(result, str):
        result = str(result)

    return result
