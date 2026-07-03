#!/usr/bin/env python3
"""
模块 5：Failure-First Design —— Harness 防护层
===============================================
系统性修复死循环、跑偏、幻觉调用与部分失败。

核心防护机制：
1. 死循环检测与打断（最大步数 + 重复调用检测）
2. 阶段目标与自检避免跑偏
3. 拦截未知工具与异常参数
4. 多步任务的部分失败恢复
5. 限流/超时重试与指数退避
"""

import time
import json
import httpx
from typing import List, Dict, Any, Optional, Tuple

from tools import TOOL_REGISTRY


class AgentHarness:
    """
    Agent Loop 的防护层，检测并处理各种失败模式。
    
    设计原则：所有错误在 Harness 层被捕获、记录、转化为可恢复状态，
    绝不抛异常让 Agent Loop 崩溃。
    """

    def __init__(
        self,
        max_steps: int = 15,
        max_retries: int = 3,
        repeat_threshold: int = 3,
        backoff_base: float = 2.0,
    ):
        self.max_steps = max_steps          # 死循环：最大步数
        self.max_retries = max_retries      # 限流：最大重试次数
        self.repeat_threshold = repeat_threshold  # 重复检测阈值
        self.backoff_base = backoff_base    # 退避基数

        self.call_history: List[List[Tuple[str, str]]] = []  # 工具调用历史
        self.step_errors: List[Dict[str, Any]] = []          # 每步错误记录
        self.retry_count = 0                                 # 当前重试计数
        self.api_retry_count = 0                             # API 重试计数

    # ============================================================
    # 1. 死循环检测与打断
    # ============================================================
    def check_dead_loop(self, step: int, tool_calls: List[Dict]) -> Optional[str]:
        """
        检测死循环：
        - 步数超过 max_steps
        - 连续 repeat_threshold 步调用完全相同的工具+参数
        """
        # 步数检查
        if step >= self.max_steps:
            return f"🛑 达到最大步数限制 {self.max_steps}，强制终止。"

        # 记录当前调用（工具名 + 参数 JSON 字符串）
        current = [
            (tc["function"]["name"], tc["function"]["arguments"])
            for tc in tool_calls
        ]
        self.call_history.append(current)

        # 重复检测：最近 N 步完全相同
        if len(self.call_history) >= self.repeat_threshold:
            last_n = self.call_history[-self.repeat_threshold:]
            if all(c == last_n[0] for c in last_n):
                tools = [name for name, _ in last_n[0]]
                return (
                    f"🛑 检测到死循环：连续 {self.repeat_threshold} 步 "
                    f"调用相同工具组合 {tools}，参数完全一致。"
                    f"请检查是否陷入无效循环，或尝试不同策略。"
                )

        return None

    # ============================================================
    # 2. 阶段目标与自检避免跑偏
    # ============================================================
    def check_drift(
        self, user_goal: str, tool_calls: List[Dict]
    ) -> Optional[str]:
        """
        检测目标偏离：当前工具调用是否与用户目标相关。
        
        启发式规则（教学用）：
        - 从用户目标提取关键词
        - 检查当前工具名是否匹配相关工具列表
        - 如果不匹配，发出警告
        """
        # 目标到相关工具的映射（可扩展）
        goal_tool_map = {
            "生成": ["generate_long_text", "write_file"],
            "读取": ["read_file", "search_files"],
            "搜索": ["search_files", "read_file"],
            "计算": ["calculator"],
            "写": ["write_file"],
            "文件": ["read_file", "write_file", "search_files"],
            "文本": ["generate_long_text", "read_file", "write_file"],
            "长文本": ["generate_long_text"],
            "段": ["generate_long_text", "read_file"],
        }

        # 提取目标关键词
        goal_keywords = set()
        for keyword, tools in goal_tool_map.items():
            if keyword in user_goal:
                goal_keywords.update(tools)

        # 如果目标太模糊，不检测
        if not goal_keywords:
            return None

        # 检查当前工具是否在相关列表中
        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            if tool_name not in goal_keywords:
                return (
                    f"⚠️ 目标偏离警告：用户目标是 '{user_goal}'，"
                    f"但当前调用 '{tool_name}' 可能不直接相关。"
                    f"建议工具：{', '.join(goal_keywords)}。"
                    f"请确认是否偏离目标，或尝试更直接的方式。"
                )

        return None

    # ============================================================
    # 3. 拦截未知工具与异常参数
    # ============================================================
    def intercept_unknown_tool(self, tool_name: str) -> Optional[str]:
        """拦截未知工具调用，返回可用工具列表。"""
        if tool_name not in TOOL_REGISTRY:
            available = list(TOOL_REGISTRY.keys())
            return (
                f"❌ 错误：未知工具 '{tool_name}'。\n"
                f"可用工具：{', '.join(available)}\n"
                f"建议：请从可用工具中选择，或检查工具名拼写。"
            )
        return None

    def intercept_invalid_args(
        self, tool_name: str, args: Dict[str, Any]
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """
        前置参数校验（工具内校验的补充）。
        返回 (错误信息, 修正后的参数)。
        """
        # read_file 的 limit 强制限制
        if tool_name == "read_file":
            limit = args.get("limit", 20)
            if isinstance(limit, int) and limit > 30:
                args = dict(args)  # 复制，不修改原参数
                args["limit"] = 30
                return (
                    f"⚠️ 参数修正：limit={limit} 超过最大限制 30，"
                    f"已自动调整为 30。",
                    args,
                )

            # offset 必须 >= 1
            offset = args.get("offset", 1)
            if isinstance(offset, int) and offset < 1:
                args = dict(args)
                args["offset"] = 1
                return (
                    f"⚠️ 参数修正：offset={offset} 必须 >= 1，"
                    f"已自动调整为 1。",
                    args,
                )

        # generate_long_text 的 paragraphs 范围
        if tool_name == "generate_long_text":
            paragraphs = args.get("paragraphs", 50)
            if isinstance(paragraphs, int):
                if paragraphs < 1:
                    args = dict(args)
                    args["paragraphs"] = 1
                    return (
                        f"⚠️ 参数修正：paragraphs={paragraphs} 必须 >= 1，"
                        f"已自动调整为 1。",
                        args,
                    )
                if paragraphs > 200:
                    args = dict(args)
                    args["paragraphs"] = 200
                    return (
                        f"⚠️ 参数修正：paragraphs={paragraphs} 超过最大 200，"
                        f"已自动调整为 200。",
                        args,
                    )

        return None, args

    # ============================================================
    # 4. 部分失败恢复
    # ============================================================
    def record_partial_failure(self, tool_name: str, error: str, step: int):
        """记录部分失败，供后续恢复提示使用。"""
        self.step_errors.append({
            "tool": tool_name,
            "error": error,
            "step": step,
        })

    def get_recovery_hint(self) -> str:
        """生成恢复提示，告诉模型之前发生了什么。"""
        if not self.step_errors:
            return ""

        hints = ["📋 此前执行中遇到以下问题，请注意避免："]
        for err in self.step_errors[-3:]:  # 只显示最近 3 个
            hints.append(
                f"  - Step {err['step']}: {err['tool']} 失败 — {err['error']}"
            )

        hints.append("💡 建议：检查参数是否正确，或尝试替代方案。")
        return "\n".join(hints)

    # ============================================================
    # 5. 限流/超时重试与退避
    # ============================================================
    def handle_api_error(self, error: Exception) -> Tuple[bool, float]:
        """
        处理 API 错误，返回 (是否重试, 等待秒数)。
        
        支持的状态码：
        - 429: Too Many Requests（限流）
        - 502: Bad Gateway
        - 503: Service Unavailable
        - 504: Gateway Timeout
        """
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            if status in (429, 502, 503, 504):
                if self.api_retry_count < self.max_retries:
                    wait = self.backoff_base ** self.api_retry_count
                    self.api_retry_count += 1
                    return True, wait

        # 连接错误也重试
        if isinstance(error, (httpx.ConnectError, httpx.TimeoutException)):
            if self.api_retry_count < self.max_retries:
                wait = self.backoff_base ** self.api_retry_count
                self.api_retry_count += 1
                return True, wait

        return False, 0.0

    def reset_api_retry(self):
        """成功调用后重置 API 重试计数。"""
        self.api_retry_count = 0

    # ============================================================
    # 6. 综合错误信息丰富化
    # ============================================================
    def enrich_error(self, base_error: str, tool_name: str) -> str:
        """把简单错误信息转化为包含恢复建议的丰富信息。"""
        suggestions = {
            "read_file": "建议：检查文件路径是否正确，使用 search_files 查找文件。",
            "write_file": "建议：检查路径权限，或设置 overwrite=true 强制覆盖。",
            "search_files": "建议：检查目录路径是否存在，或扩大搜索范围。",
            "calculator": "建议：检查表达式格式，只支持 + - * / 和括号。",
            "generate_long_text": "建议：paragraphs 必须在 1~200 之间。",
        }

        suggestion = suggestions.get(tool_name, "建议：检查参数是否正确。")
        return f"{base_error}\n{suggestion}"

    # ============================================================
    # 7. 执行状态报告
    # ============================================================
    def get_status_report(self) -> Dict[str, Any]:
        """返回 Harness 的执行状态报告，用于调试和 benchmark。"""
        return {
            "total_steps": len(self.call_history),
            "total_errors": len(self.step_errors),
            "api_retries": self.api_retry_count,
            "dead_loop_detected": any(
                "死循环" in str(e) for e in self.step_errors
            ),
            "drift_warnings": len([
                e for e in self.step_errors if "偏离" in str(e)
            ]),
        }
