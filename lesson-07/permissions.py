#!/usr/bin/env python3
"""
模块 7：Least-Privilege Tooling —— 权限控制核心
================================================
为 Agent 工具调用提供三层权限能力：

1. 工具白名单（Tool Whitelist）
   - 只有注册在 PERMISSION_CATALOG 中的工具才能被调用。
   - 默认最小权限：未分类的工具不会被授予写/网络/执行能力。

2. 危险操作分类（Danger Classification）
   - AUTO：只读、纯计算、无状态查询，自动放行。
   - CONFIRM：会改变系统状态的操作（写文件、网络请求），需要显式确认。
   - FORBIDDEN：破坏性、不可逆或超出沙箱的操作，永远禁止。

3. 上下文级权限控制（Context-Level Permission）
   - READ_ONLY：拦截所有写入/删除/网络/执行操作。
   - CONFIRM：对副作用工具弹出确认门。
   - UNRESTRICTED：显式授权后放行（教学用，生产不推荐）。

设计原则：
- 默认只读，写入必须显式授权。
- 所有拦截和确认都在工具执行前发生，不让错误进入真实系统。
- 结果以字符串形式返回给模型，保持 loop 不 crash。
"""

import os
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, List, Optional, Set, Any


# ============================================================
# 危险等级与操作分类
# ============================================================
class DangerLevel(Enum):
    """工具的危险等级。"""
    AUTO = auto()       # 自动放行：只读、纯计算
    CONFIRM = auto()    # 必须确认：副作用、写入、网络
    FORBIDDEN = auto()  # 永远禁止：破坏性、不可逆、越界


class PermissionMode(Enum):
    """Agent 运行的权限模式。"""
    READ_ONLY = "read_only"       # 只读模式，拦截写操作
    CONFIRM = "confirm"           # 确认模式，副作用需确认
    UNRESTRICTED = "unrestricted" # 显式授权模式（教学演示）


@dataclass
class ToolPermission:
    """单个工具的权限描述。"""
    danger: DangerLevel
    description: str
    requires_confirm: bool = False
    allowed_in_read_only: bool = False


# ============================================================
# 权限清单（Permission Catalog）
# ============================================================
PERMISSION_CATALOG: Dict[str, ToolPermission] = {
    "calculator": ToolPermission(
        danger=DangerLevel.AUTO,
        description="纯计算，无状态无副作用",
        allowed_in_read_only=True,
    ),
    "read_file": ToolPermission(
        danger=DangerLevel.AUTO,
        description="读取用户主目录下文件，只读",
        allowed_in_read_only=True,
    ),
    "search_files": ToolPermission(
        danger=DangerLevel.AUTO,
        description="搜索用户主目录下文件名，只读",
        allowed_in_read_only=True,
    ),
    "generate_long_text": ToolPermission(
        danger=DangerLevel.AUTO,
        description="生成内存中的文本，不接触外部系统",
        allowed_in_read_only=True,
    ),
    "write_file": ToolPermission(
        danger=DangerLevel.CONFIRM,
        description="写入或覆盖本地文件，会改变文件系统状态",
        requires_confirm=True,
        allowed_in_read_only=False,
    ),
    "external_api": ToolPermission(
        danger=DangerLevel.CONFIRM,
        description="访问外部网络服务，存在数据外发和依赖风险",
        requires_confirm=True,
        allowed_in_read_only=False,
    ),
    "delete_file": ToolPermission(
        danger=DangerLevel.FORBIDDEN,
        description="删除文件不可逆，默认永久禁止",
    ),
    "execute_shell": ToolPermission(
        danger=DangerLevel.FORBIDDEN,
        description="执行任意 shell 命令，破坏力和逃逸风险极高",
    ),
    "send_email": ToolPermission(
        danger=DangerLevel.FORBIDDEN,
        description="向外部发送邮件/消息，属于不可控外发",
    ),
}

# 在 READ_ONLY 模式下额外禁止的参数特征（即使工具本身允许）
READ_ONLY_FORBIDDEN_PARAMS: Dict[str, List[str]] = {
    "write_file": ["overwrite"],  # 只读模式下连 overwrite=true 也不能用
}


# ============================================================
# 权限上下文
# ============================================================
@dataclass
class PermissionContext:
    """一次会话的权限上下文，包含当前模式、已确认的工具、已拦截记录。"""
    mode: PermissionMode = PermissionMode.READ_ONLY
    confirmed_tools: Set[str] = field(default_factory=set)
    blocked_log: List[Dict[str, Any]] = field(default_factory=list)
    confirmed_log: List[Dict[str, Any]] = field(default_factory=list)

    def set_mode(self, mode: PermissionMode):
        self.mode = mode
        # 切换模式时清空已确认列表，避免权限降级后仍沿用旧授权
        self.confirmed_tools.clear()

    def record_block(self, tool_name: str, reason: str, arguments: Dict[str, Any]):
        self.blocked_log.append({
            "tool": tool_name,
            "reason": reason,
            "arguments": arguments,
        })

    def record_confirm(self, tool_name: str, arguments: Dict[str, Any]):
        self.confirmed_log.append({
            "tool": tool_name,
            "arguments": arguments,
        })
        self.confirmed_tools.add(tool_name)


# ============================================================
# 权限检查函数
# ============================================================
def classify_tool(tool_name: str) -> Optional[ToolPermission]:
    """返回工具的权限描述；未在白名单中返回 None。"""
    return PERMISSION_CATALOG.get(tool_name)


def check_permission(
    ctx: PermissionContext,
    tool_name: str,
    arguments: Dict[str, Any],
    auto_confirm: bool = False,
) -> Dict[str, Any]:
    """
    检查本次工具调用是否被允许。

    返回：
        {
            "allowed": bool,
            "reason": str,           # 若 allowed=False，返回拦截原因
            "needs_confirm": bool,   # 若 allowed=True 且需要确认，为 True
        }
    """
    perm = classify_tool(tool_name)
    if perm is None:
        return {
            "allowed": False,
            "reason": f"工具 '{tool_name}' 不在白名单中，默认拒绝。",
            "needs_confirm": False,
        }

    if perm.danger == DangerLevel.FORBIDDEN:
        return {
            "allowed": False,
            "reason": f"工具 '{tool_name}' 属于危险操作（{perm.description}），永远禁止。",
            "needs_confirm": False,
        }

    if ctx.mode == PermissionMode.READ_ONLY:
        if not perm.allowed_in_read_only:
            ctx.record_block(tool_name, "read_only_mode", arguments)
            return {
                "allowed": False,
                "reason": (
                    f"当前处于只读模式，工具 '{tool_name}' 被拒绝。"
                    f"该操作会改变系统状态（{perm.description}）。"
                    f"如需写入，请切换权限模式为 confirm 或 unrestricted。"
                ),
                "needs_confirm": False,
            }

        # 即使在只读模式下允许的工具，也要检查参数级限制
        forbidden_keys = READ_ONLY_FORBIDDEN_PARAMS.get(tool_name, [])
        for key in forbidden_keys:
            if key in arguments:
                ctx.record_block(tool_name, f"forbidden_param_in_read_only:{key}", arguments)
                return {
                    "allowed": False,
                    "reason": (
                        f"只读模式下禁止参数 '{key}'。"
                        f"工具 '{tool_name}' 的该参数会导致写操作。"
                    ),
                    "needs_confirm": False,
                }

    if perm.requires_confirm and ctx.mode != PermissionMode.UNRESTRICTED:
        if tool_name in ctx.confirmed_tools or auto_confirm:
            return {"allowed": True, "reason": "", "needs_confirm": False}
        return {
            "allowed": True,
            "reason": "",
            "needs_confirm": True,
        }

    return {"allowed": True, "reason": "", "needs_confirm": False}


def confirm_tool(
    ctx: PermissionContext,
    tool_name: str,
    arguments: Dict[str, Any],
    user_approved: bool,
) -> bool:
    """
    用户对副作用工具做出确认。
    返回 True 表示已记录授权；False 表示用户拒绝。
    """
    if user_approved:
        ctx.record_confirm(tool_name, arguments)
        return True
    ctx.record_block(tool_name, "user_denied", arguments)
    return False


# ============================================================
# 沙箱路径检查
# ============================================================
def is_path_inside_sandbox(path: str, sandbox_root: str) -> bool:
    """检查路径是否在沙箱根目录下，防止目录遍历逃逸。"""
    abs_path = os.path.abspath(os.path.expanduser(path))
    abs_root = os.path.abspath(os.path.expanduser(sandbox_root))
    return abs_path.startswith(abs_root + os.sep) or abs_path == abs_root


# ============================================================
# 权限报告
# ============================================================
def build_permission_report(ctx: PermissionContext) -> str:
    """生成当前会话的权限执行报告。"""
    lines = [
        "【权限模式】" + ctx.mode.value,
        "【已授权副作用工具】" + (", ".join(sorted(ctx.confirmed_tools)) or "无"),
        "【拦截次数】" + str(len(ctx.blocked_log)),
    ]
    if ctx.blocked_log:
        lines.append("【拦截明细】")
        for item in ctx.blocked_log:
            lines.append(f"  - {item['tool']}: {item['reason']}")
    return "\n".join(lines)


# ============================================================
# 便捷：从环境变量解析权限模式
# ============================================================
def parse_permission_mode(value: str) -> PermissionMode:
    """把字符串解析为 PermissionMode，无法识别时返回 READ_ONLY（默认最小权限）。"""
    try:
        return PermissionMode(value.lower())
    except ValueError:
        return PermissionMode.READ_ONLY
