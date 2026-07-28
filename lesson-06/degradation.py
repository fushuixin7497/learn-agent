#!/usr/bin/env python3
"""
模块 6：Graceful Degradation 核心
=================================
实现"降级策略金字塔"：
    完全成功 → 部分成功 → 安全兜底 → 优雅失败

核心职责：
1. 检测模型响应质量（空回答、拒绝、乱答、格式错误）。
2. 在模型质量不达标时，用更简单 prompt / 预设模板重试。
3. 在工具不可用时，提示模型换路径，并保留已完成部分。
4. 所有失败都转化为给用户的清晰状态说明，绝不抛异常让 Agent crash。
"""

import os
import json
import time
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple


# ============================================================
# 配置常量
# ============================================================
DEGRADE_MODE = os.getenv("DEGRADE_MODE", "on").lower()      # off / on / forced
MAX_NONSENSE_RETRIES = int(os.getenv("MAX_NONSENSE_RETRIES", "2"))
SIMPLE_PROMPT_SUFFIX = (
    "\n\n【系统提示：请直接回答用户问题，或只调用一个最相关的工具。"
    "如果无法完成，请明确说明原因和已完成的步骤。】"
)

# 被认为是"拒绝/乱答"的关键词（中文 & 英文）
REFUSAL_PATTERNS = [
    "对不起", "抱歉", "无法", "不能", " I'm sorry", "I cannot", "I can't",
    " apologize", "无关", "无法回答", "不能回答", "没有意义", "不清楚",
    "我不知道", "没有相关信息", "invalid", "error", "not available",
]

# 被认为是"安全兜底"可接受的兜底关键词
FALLBACK_ACCEPTED = [
    "已完成", "部分完成", "已执行", "成功", "结果如下", "最终答案",
]


# ============================================================
# 数据类：保存一次 Agent 运行的状态
# ============================================================
@dataclass
class DegradationState:
    """记录 Agent 运行的降级状态与中间结果。"""

    user_input: str
    degrade_mode: str = "on"
    completed_tools: List[Dict[str, Any]] = field(default_factory=list)
    failed_tools: List[Dict[str, Any]] = field(default_factory=list)
    nonsense_count: int = 0
    partial_result: str = ""
    final_status: str = "pending"   # pending / success / partial / fallback / failed
    status_message: str = ""
    retries_left: int = MAX_NONSENSE_RETRIES

    def record_tool_success(self, tool_name: str, args: Dict[str, Any], result: str):
        """记录一个成功的工具调用。"""
        self.completed_tools.append({
            "tool": tool_name,
            "args": args,
            "result_preview": result[:200],
        })
        # 累计部分结果：优先保留写入类/计算类结果
        if tool_name in ("write_file", "calculator", "generate_long_text"):
            self.partial_result = result[:800]

    def record_tool_failure(self, tool_name: str, args: Dict[str, Any], error: str):
        """记录一个失败的工具调用。"""
        self.failed_tools.append({
            "tool": tool_name,
            "args": args,
            "error": error[:300],
        })

    def to_user_report(self) -> str:
        """生成给用户的清晰状态报告。"""
        lines = []
        lines.append(f"【运行状态】{self._status_label()}")
        lines.append(f"【用户请求】{self.user_input}")

        if self.completed_tools:
            lines.append(f"\n✅ 已完成步骤 ({len(self.completed_tools)})：")
            for item in self.completed_tools:
                lines.append(f"  · {item['tool']}: {item['args']} → {item['result_preview'][:120]}...")

        if self.failed_tools:
            lines.append(f"\n⚠️ 失败步骤 ({len(self.failed_tools)})：")
            for item in self.failed_tools:
                lines.append(f"  · {item['tool']}: {item['error'][:120]}")

        if self.partial_result:
            lines.append(f"\n📦 已保留的中间结果：\n{self.partial_result[:600]}")

        if self.status_message:
            lines.append(f"\n💡 说明：{self.status_message}")

        return "\n".join(lines)

    def _status_label(self) -> str:
        return {
            "success": "完全成功",
            "partial": "部分成功（已降级）",
            "fallback": "安全兜底（使用预设回复）",
            "failed": "优雅失败（所有路径均不可用）",
            "pending": "运行中",
        }.get(self.final_status, "未知")


# ============================================================
# 响应质量评估
# ============================================================
def is_nonsense_reply(reply: str) -> bool:
    """
    判断模型回复是否属于"乱答/拒绝/无意义"。
    规则：
    - 空或纯空白
    - 包含拒绝关键词
    - 既不调用工具，也不包含有效信息（太短）
    """
    if not reply or not reply.strip():
        return True

    reply_lower = reply.lower()
    if any(p.lower() in reply_lower for p in REFUSAL_PATTERNS):
        return True

    # 太短且无标点/结构，认为无意义
    stripped = reply.strip()
    if len(stripped) < 10 and not stripped.endswith(("？", "?", "。", ".", "!", "！")):
        return True

    return False


def is_fallback_acceptable(reply: str) -> bool:
    """判断一个兜底回复是否已经包含了可用信息。"""
    if not reply:
        return False
    return any(kw in reply for kw in FALLBACK_ACCEPTED) or len(reply) > 30


# ============================================================
# Prompt 降级
# ============================================================
def build_simpler_prompt(original_prompt: str) -> str:
    """
    当模型对复杂 prompt 乱答时，生成一个更简单的版本。
    做法：
    - 保留 system prompt 核心规则
    - 追加"直接回答/只调用一个工具"的约束
    - 移除可能让模型困惑的多条规则中的部分（教学简化）
    """
    simplified = original_prompt.strip()
    # 如果 prompt 已经带后缀，不再追加
    if SIMPLE_PROMPT_SUFFIX not in simplified:
        simplified += SIMPLE_PROMPT_SUFFIX
    return simplified


def downgrade_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    对 messages 中的 system prompt 做降级处理，返回新的 messages。
    用于模型乱答/拒绝后的重试。
    """
    new_messages = []
    for m in messages:
        if m.get("role") == "system":
            new_messages.append({
                "role": "system",
                "content": build_simpler_prompt(m.get("content", "")),
            })
        else:
            new_messages.append(dict(m))
    return new_messages


# ============================================================
# 工具不可用处理
# ============================================================
def classify_tool_error(result: str) -> Tuple[str, str]:
    """
    对工具返回的错误进行分类，返回 (error_type, suggestion)。
    用于提示模型换路径。
    """
    result_lower = result.lower()

    if "429" in result or "503" in result or "502" in result or "504" in result:
        return "service_unavailable", "该服务暂时不可用，请尝试其他工具或稍后重试。"

    if "文件不存在" in result or "路径" in result:
        return "not_found", "请检查路径是否正确，或用 search_files 查找文件。"

    if "越界" in result or "超出范围" in result:
        return "out_of_range", "请调整 offset/limit 参数，或分段读取。"

    if "表达式" in result or "计算" in result:
        return "invalid_expression", "请检查数学表达式格式，只支持 + - * / 和括号。"

    if "未知工具" in result:
        return "unknown_tool", "请从可用工具列表中选择正确的工具名。"

    return "generic", "请检查参数或尝试换一种方式完成。"


def build_tool_unavailable_hint(tool_name: str, error: str) -> str:
    """构造给模型的"工具不可用，请换路径"提示。"""
    error_type, suggestion = classify_tool_error(error)
    return (
        f"⚠️ 工具 '{tool_name}' 调用失败（{error_type}）：{error[:200]}\n"
        f"💡 {suggestion}\n"
        f"请换一条路径继续完成用户请求，不要重复同样的失败调用。"
    )


# ============================================================
# 安全兜底回复
# ============================================================
def generate_fallback_reply(state: DegradationState) -> str:
    """
    当模型连续乱答/所有工具都不可用/达到最大重试次数时，
    生成一个预设的安全兜底回复，向用户说明当前状态。
    """
    state.final_status = "fallback"
    state.status_message = (
        f"模型连续 {MAX_NONSENSE_RETRIES} 次未能给出有效回答，"
        f"已切换为安全兜底模式。下面是当前已保留的状态与建议。"
    )
    return state.to_user_report()


def generate_graceful_failure(state: DegradationState, reason: str) -> str:
    """
    所有降级路径都失败时的最终回复。
    包含：已完成部分、失败原因、下一步建议。
    """
    state.final_status = "failed"
    state.status_message = (
        f"无法继续执行：{reason}。"
        "请检查网络、API 密钥或工具可用性后重试。"
    )
    return state.to_user_report()


# ============================================================
# 降级模式开关工具函数
# ============================================================
def should_force_degradation() -> bool:
    """DEGRADE_MODE=forced 时，强制每个工具都返回不可用，用于演示降级效果。"""
    return os.getenv("DEGRADE_MODE", "on").lower() == "forced"


def is_degradation_enabled() -> bool:
    """是否启用降级处理。"""
    return os.getenv("DEGRADE_MODE", "on").lower() in ("on", "forced")


# ============================================================
# 工具结果拦截（模拟不可用）
# ============================================================
def maybe_degrade_tool_result(tool_name: str, real_result: str) -> str:
    """
    在 forced 模式下，模拟工具不可用；
    正常模式下透传真实结果。
    """
    if not should_force_degradation():
        return real_result

    # forced 模式：假装外部服务不可用
    return (
        f"错误：服务 '{tool_name}' 当前不可用（模拟强制降级模式）。"
        "请尝试其他工具或换一条路径完成。"
    )
