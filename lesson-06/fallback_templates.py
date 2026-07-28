#!/usr/bin/env python3
"""
模块 6：安全兜底模板
===================
当模型连续乱答、所有工具都不可用、或达到最大降级次数时，
用预设模板生成清晰、无 stack trace 的状态说明。

设计原则：
- 不暴露内部异常细节。
- 告诉用户"已经做了什么"、"哪里失败了"、"接下来能做什么"。
- 语言风格稳定、友好、可预测。
"""

from typing import List, Dict, Any


def partial_success_template(
    user_input: str,
    completed: List[Dict[str, Any]],
    failed: List[Dict[str, Any]],
    partial_result: str = "",
) -> str:
    """部分成功：有步骤完成，也有步骤失败。"""
    lines = [
        "【部分成功】我已经尽力完成了部分任务，但遇到了一些问题。",
        f"\n你的请求：{user_input}",
    ]

    if completed:
        lines.append("\n✅ 已完成的步骤：")
        for item in completed:
            lines.append(f"  · {item['tool']}：{item['result_preview'][:100]}...")

    if failed:
        lines.append("\n⚠️ 未能完成的步骤：")
        for item in failed:
            lines.append(f"  · {item['tool']}：{item['error'][:100]}")

    if partial_result:
        lines.append(f"\n📦 已保留的中间结果：\n{partial_result[:500]}")

    lines.append(
        "\n💡 建议：如果失败步骤对结果很重要，可以检查对应服务/路径是否可用后重试；"
        "否则上述已完成部分已经可用。"
    )
    return "\n".join(lines)


def service_unavailable_template(service_name: str, alternative: str = "") -> str:
    """外部服务不可用时的兜底说明。"""
    lines = [
        f"【服务不可用】'{service_name}' 当前无法访问。",
    ]
    if alternative:
        lines.append(f"💡 建议改用：{alternative}")
    lines.append("如果该服务对你很重要，请稍后重试或检查网络/API 状态。")
    return "\n".join(lines)


def nonsense_reply_template(user_input: str, retry_count: int) -> str:
    """模型连续乱答后的兜底说明。"""
    return (
        f"【模型响应异常】我在处理你的请求时，模型连续 {retry_count} 次没有给出有效回答。\n"
        f"你的请求：{user_input}\n\n"
        "💡 建议：\n"
        "  1. 尝试把问题说得更具体、更简单；\n"
        "  2. 检查模型 API 是否正常工作；\n"
        "  3. 如果是多步骤任务，可以拆成几个小任务分别执行。"
    )


def graceful_failure_template(user_input: str, reason: str) -> str:
    """所有路径都失败时的最终说明。"""
    return (
        f"【无法完成】很抱歉，当前无法处理你的请求。\n"
        f"你的请求：{user_input}\n"
        f"原因：{reason}\n\n"
        "💡 建议：\n"
        "  1. 检查 LLM_API_KEY、网络连接和模型服务状态；\n"
        "  2. 尝试开启 DEGRADE_MODE=on（当前已启用则尝试 off 对比）；\n"
        "  3. 把任务拆小后重试。"
    )


def status_line(step: int, status: str, detail: str = "") -> str:
    """在 Agent 运行过程中打印的状态行，用于给用户清晰反馈。"""
    emoji = {
        "success": "✅",
        "partial": "⚠️",
        "fallback": "🛡️",
        "failed": "❌",
        "retry": "🔄",
        "degrade": "🔽",
    }.get(status, "ℹ️")
    base = f"{emoji} Step {step} [{status}]"
    if detail:
        base += f": {detail}"
    return base
