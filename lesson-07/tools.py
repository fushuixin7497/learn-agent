#!/usr/bin/env python3
"""
模块 7：Least-Privilege Tooling —— 受控工具集合
================================================
在 lesson-06 工具基础上，为每个工具标注权限类别，并提供沙箱执行能力：

1. 文件系统沙箱：所有文件操作限定在用户主目录下的指定子目录。
2. 网络隔离：external_api 在教学模式下仅返回模拟结果，不发起真实请求。
3. 只读拦截：read_file / search_files 默认放行；write_file 需显式授权。
4. 危险工具：delete_file / execute_shell / send_email 注册但默认被禁止。
"""

import os
import json
import random
from typing import Callable, Dict, List, Any, Optional

from permissions import (
    PERMISSION_CATALOG,
    DangerLevel,
    PermissionContext,
    PermissionMode,
    check_permission,
    confirm_tool,
    is_path_inside_sandbox,
)


# ============================================================
# 沙箱根目录
# ============================================================
SANDBOX_ROOT = os.path.expanduser("~/learn-agent-sandbox")


def _ensure_sandbox(path: str) -> str:
    """确保路径在沙箱内，返回绝对路径；若越界返回空字符串。"""
    abs_path = os.path.abspath(os.path.expanduser(path))
    if not is_path_inside_sandbox(abs_path, SANDBOX_ROOT):
        return ""
    return abs_path


# ============================================================
# 基础工具实现
# ============================================================
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
    """读取沙箱内文本文件内容。可指定 offset 和 limit（最大 30）。"""
    if not isinstance(filepath, str) or not filepath:
        return "错误：filepath 必须是非空字符串"
    if not isinstance(offset, int) or offset < 1:
        return "错误：offset 必须是 >=1 的整数"
    if not isinstance(limit, int) or limit < 1 or limit > 500:
        return "错误：limit 必须是 1~500 的整数"

    if limit > 30:
        limit = 30

    abs_path = _ensure_sandbox(filepath)
    if not abs_path:
        return f"错误：路径 '{filepath}' 超出沙箱范围（{SANDBOX_ROOT}）"

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
    """写入文本到沙箱内文件，支持幂等和覆盖控制。"""
    if not isinstance(filepath, str) or not filepath:
        return "错误：filepath 必须是非空字符串"
    if not isinstance(content, str):
        return "错误：content 必须是字符串"

    abs_path = _ensure_sandbox(filepath)
    if not abs_path:
        return f"错误：路径 '{filepath}' 超出沙箱范围（{SANDBOX_ROOT}）"

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
    """在沙箱内指定目录下递归搜索文件名包含关键字的文件。"""
    if not isinstance(directory, str) or not directory:
        return "错误：directory 必须是非空字符串"
    if not isinstance(keyword, str) or not keyword:
        return "错误：keyword 必须是非空字符串"

    abs_dir = _ensure_sandbox(directory)
    if not abs_dir:
        return f"错误：目录 '{directory}' 超出沙箱范围（{SANDBOX_ROOT}）"

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
    【教学工具】模拟外部 API。
    在最小权限课程中，默认不发起真实网络请求，仅返回模拟结果，
    以演示"网络访问属于需确认副作用"。
    """
    if not isinstance(query, str) or not query:
        return "错误：query 必须是非空字符串"

    # 教学演示：随机不可用，模拟网络不可靠性
    if random.random() < 0.3:
        return (
            f"错误：外部 API 服务暂时不可用（503 Service Unavailable），"
            f"无法查询 '{query}'。请改用本地工具完成。"
        )

    return f"✅ 外部 API 返回结果：关于 '{query}' 的查询成功（模拟数据）。"


def delete_file(filepath: str) -> str:
    """【危险工具】删除文件，默认被权限系统禁止。"""
    return "错误：delete_file 未执行（应被权限系统拦截）"


def execute_shell(command: str) -> str:
    """【危险工具】执行 shell 命令，默认被权限系统禁止。"""
    return "错误：execute_shell 未执行（应被权限系统拦截）"


def send_email(to: str, subject: str, body: str) -> str:
    """【危险工具】发送邮件，默认被权限系统禁止。"""
    return "错误：send_email 未执行（应被权限系统拦截）"


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
    "delete_file": delete_file,
    "execute_shell": execute_shell,
    "send_email": send_email,
}


def get_tool_danger_label(tool_name: str) -> str:
    """返回工具的危险等级中文标签，用于展示。"""
    perm = PERMISSION_CATALOG.get(tool_name)
    if not perm:
        return "未知（默认拒绝）"
    return {
        DangerLevel.AUTO: "自动放行",
        DangerLevel.CONFIRM: "需确认",
        DangerLevel.FORBIDDEN: "永远禁止",
    }.get(perm.danger, "未知")


def get_tools() -> List[Dict[str, Any]]:
    """返回符合 OpenAI Function Calling 协议的 tools 数组。"""
    schemas = [
        ("calculator", "执行数学计算。支持 + - * / 和括号，例如 '365*24'。"),
        ("read_file", "读取沙箱内文本文件的内容。可指定 offset 和 limit（最大 30）。"),
        ("write_file", (
            "写入文本到沙箱内文件。如果文件已存在且内容一致，不会重复写入。"
            "如需覆盖，设置 overwrite=true。该操作属于副作用，需要权限确认。"
        )),
        ("generate_long_text", "生成超长文本，默认 50 段，最大 200。"),
        ("search_files", "在沙箱内指定目录下递归搜索文件名包含关键字的文件，最多 20 个结果。"),
        ("external_api", (
            "调用外部 API 查询信息（教学模拟，不发起真实请求）。"
            "网络访问属于副作用，需要权限确认。"
        )),
        ("delete_file", "删除文件（危险操作，默认禁止）。"),
        ("execute_shell", "执行 shell 命令（危险操作，默认禁止）。"),
        ("send_email", "发送邮件（危险操作，默认禁止）。"),
    ]

    tools = []
    for name, desc in schemas:
        schema = {
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
        if name == "calculator":
            schema["function"]["parameters"] = {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "数学表达式，例如 '365 * 24'"},
                },
                "required": ["expression"],
            }
        elif name == "read_file":
            schema["function"]["parameters"] = {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "文件路径"},
                    "offset": {"type": "integer", "description": "起始行号，从 1 开始，默认 1"},
                    "limit": {"type": "integer", "description": "读取行数，默认 20，最大 30"},
                },
                "required": ["filepath"],
            }
        elif name == "write_file":
            schema["function"]["parameters"] = {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "要写入的内容"},
                    "overwrite": {"type": "boolean", "description": "是否覆盖"},
                },
                "required": ["filepath", "content"],
            }
        elif name == "generate_long_text":
            schema["function"]["parameters"] = {
                "type": "object",
                "properties": {
                    "paragraphs": {"type": "integer", "description": "生成段数"},
                },
                "required": [],
            }
        elif name in ("search_files",):
            schema["function"]["parameters"] = {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "搜索目录"},
                    "keyword": {"type": "string", "description": "文件名关键字"},
                },
                "required": ["directory", "keyword"],
            }
        elif name == "external_api":
            schema["function"]["parameters"] = {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "查询内容"},
                },
                "required": ["query"],
            }
        elif name == "delete_file":
            schema["function"]["parameters"] = {
                "type": "object",
                "properties": {"filepath": {"type": "string", "description": "要删除的文件路径"}},
                "required": ["filepath"],
            }
        elif name == "execute_shell":
            schema["function"]["parameters"] = {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "要执行的命令"}},
                "required": ["command"],
            }
        elif name == "send_email":
            schema["function"]["parameters"] = {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "收件人"},
                    "subject": {"type": "string", "description": "主题"},
                    "body": {"type": "string", "description": "正文"},
                },
                "required": ["to", "subject", "body"],
            }
        tools.append(schema)
    return tools


# ============================================================
# 受控执行入口
# ============================================================
def execute_tool(
    name: str,
    arguments: Dict[str, Any],
    ctx: PermissionContext,
    confirm_callback: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
) -> str:
    """
    在权限上下文下执行工具。

    流程：
    1. 检查工具是否在白名单及危险等级。
    2. 只读模式下拦截写入类工具。
    3. 副作用工具需用户确认（confirm_callback 返回 True）。
    4. 最后调用真实工具实现。
    """
    # 1. 权限检查
    decision = check_permission(ctx, name, arguments)
    if not decision["allowed"]:
        return f"❌ 权限拒绝：{decision['reason']}"

    # 2. 需要确认时，调用 confirm_callback
    if decision["needs_confirm"]:
        if confirm_callback is None:
            return (
                f"❌ 权限拒绝：工具 '{name}' 属于副作用操作，需要用户确认，"
                f"但当前环境未提供确认回调。请在 {ctx.mode.value} 模式下运行并授权。"
            )
        prompt = (
            f"⚠️ 工具 '{name}' 请求执行以下操作，可能改变系统状态或访问外部网络：\n"
            f"   参数：{json.dumps(arguments, ensure_ascii=False, indent=2)}\n"
            f"   是否允许？(y/n) "
        )
        approved = confirm_callback(prompt, arguments)
        if not confirm_tool(ctx, name, arguments, approved):
            return f"❌ 权限拒绝：用户未授权工具 '{name}'。"

    # 3. 执行真实工具
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
