#!/usr/bin/env python3
"""
模块 5：错误注入器 —— 用于测试 Harness 防护机制
===============================================
人为触发各种错误场景，验证 Agent 的恢复能力。

使用方式：
    from injector import ErrorInjector
    
    injector = ErrorInjector()
    
    # 让 read_file 下次调用返回错误
    injector.inject_tool_error("read_file", "错误：文件被锁定")
    
    # 让 API 接下来 3 次返回 429
    injector.inject_rate_limit(3)
    
    # 执行带注入的 agent_loop
    result = injector.run_with_injection(agent_loop, "读取文件")
"""

import json
import random
from typing import Dict, Any, List, Optional, Callable
from unittest.mock import patch


class ErrorInjector:
    """
    错误注入器：在工具调用或 API 请求中插入人为错误，
    用于测试 Harness 的防护和恢复能力。
    """

    def __init__(self):
        # 工具错误注入队列：{tool_name: [error_msg1, error_msg2, ...]}
        self._tool_errors: Dict[str, List[str]] = {}
        
        # API 错误注入计数
        self._api_error_remaining: int = 0
        self._api_error_status: int = 429
        
        # 循环注入模式
        self._loop_pattern: Optional[List[Dict[str, Any]]] = None
        self._loop_repeat: int = 0
        
        # 未知工具注入
        self._inject_unknown_tool: Optional[str] = None

    # ============================================================
    # 1. 工具错误注入
    # ============================================================
    def inject_tool_error(self, tool_name: str, error_msg: str, times: int = 1):
        """
        让指定工具接下来 N 次调用返回错误。
        
        示例：
            injector.inject_tool_error("read_file", "错误：文件不存在", 2)
            # 接下来 2 次调用 read_file 都会返回 "错误：文件不存在"
        """
        if tool_name not in self._tool_errors:
            self._tool_errors[tool_name] = []
        self._tool_errors[tool_name].extend([error_msg] * times)

    def inject_tool_random_errors(
        self, tool_name: str, error_pool: List[str], fail_rate: float = 0.5, max_times: int = 10
    ):
        """
        让指定工具以指定概率随机返回错误，最多 N 次。
        
        示例：
            injector.inject_tool_random_errors(
                "flaky_tool",
                ["错误：429", "错误：503", "错误：超时"],
                fail_rate=0.5,
                max_times=10
            )
        """
        errors = []
        for _ in range(max_times):
            if random.random() < fail_rate:
                errors.append(random.choice(error_pool))
        self._tool_errors[tool_name] = errors

    # ============================================================
    # 2. API 限流注入
    # ============================================================
    def inject_rate_limit(self, next_n_calls: int = 3, status: int = 429):
        """
        让 API 接下来 N 次返回指定状态码（默认 429）。
        
        示例：
            injector.inject_rate_limit(3, 429)
            # 接下来 3 次 API 调用返回 429 Too Many Requests
        """
        self._api_error_remaining = next_n_calls
        self._api_error_status = status

    def inject_service_unavailable(self, next_n_calls: int = 2):
        """让 API 接下来 N 次返回 503。"""
        self.inject_rate_limit(next_n_calls, 503)

    # ============================================================
    # 3. 未知工具注入
    # ============================================================
    def inject_unknown_tool_call(self, tool_name: str = "nonexistent_tool"):
        """
        让模型下次调用时收到一个未知工具名。
        
        实现方式：在 execute_tool 调用时，临时替换工具名。
        """
        self._inject_unknown_tool = tool_name

    # ============================================================
    # 4. 循环模式注入
    # ============================================================
    def inject_loop_pattern(self, pattern: List[Dict[str, Any]], repeat: int = 5):
        """
        注入循环模式，测试死循环检测。
        
        示例：
            injector.inject_loop_pattern([
                {"name": "search_files", "arguments": {"directory": "~", "keyword": "."}}
            ], repeat=5)
        """
        self._loop_pattern = pattern
        self._loop_repeat = repeat

    # ============================================================
    # 5. 执行带注入的函数
    # ============================================================
    def run_with_injection(self, func: Callable, *args, **kwargs) -> Any:
        """
        执行函数，同时应用所有注入的错误。
        
        通过 monkey-patch execute_tool 和 httpx.Client.post 实现注入。
        """
        import tools as _tools_module
        import httpx

        # 保存原始函数（通过模块直接引用，避免被 patch 影响）
        _original_execute = _tools_module.execute_tool
        _original_post = httpx.Client.post

        # 创建注入版 execute_tool
        def injected_execute_tool(name: str, arguments: dict) -> str:
            # 检查是否有未知工具注入
            if self._inject_unknown_tool:
                name = self._inject_unknown_tool
                self._inject_unknown_tool = None

            # 检查是否有工具错误注入
            if name in self._tool_errors and self._tool_errors[name]:
                error_msg = self._tool_errors[name].pop(0)
                # 如果错误队列空了，删除键
                if not self._tool_errors[name]:
                    del self._tool_errors[name]
                return error_msg

            # 否则正常执行（使用保存的原始函数引用）
            return _original_execute(name, arguments)

        # 创建注入版 httpx.Client.post
        def injected_post(self_client, url, **kwargs):
            if self._api_error_remaining > 0:
                self._api_error_remaining -= 1
                # 构造一个假的 HTTPStatusError
                from httpx import Response, Request, HTTPStatusError
                request = Request("POST", url)
                response = Response(
                    self._api_error_status,
                    request=request,
                    content=json.dumps({
                        "error": {
                            "message": f"模拟错误：HTTP {self._api_error_status}",
                            "type": "injected_error"
                        }
                    }).encode()
                )
                raise HTTPStatusError(
                    f"模拟注入的 HTTP {self._api_error_status}",
                    request=request,
                    response=response
                )
            return _original_post(self_client, url, **kwargs)

        # 应用 patch
        with patch("tools.execute_tool", injected_execute_tool):
            with patch("httpx.Client.post", injected_post):
                return func(*args, **kwargs)

    # ============================================================
    # 6. 便捷场景预设
    # ============================================================
    def preset_dead_loop(self):
        """预设：死循环场景（连续搜索相同目录）"""
        self.inject_loop_pattern([
            {"name": "search_files", "arguments": {"directory": "~", "keyword": "."}}
        ], repeat=5)

    def preset_partial_failure(self):
        """预设：部分失败场景（3 个调用中 1 个失败）"""
        self.inject_tool_error("read_file", "错误：文件不存在 '/Users/albert/xxx.txt'", 1)

    def preset_rate_limit_burst(self):
        """预设：限流突发（连续 3 次 429）"""
        self.inject_rate_limit(3, 429)

    def preset_unknown_tool(self):
        """预设：未知工具调用"""
        self.inject_unknown_tool_call("nonexistent_tool_xyz")

    def preset_all(self):
        """预设：全部错误场景叠加（用于 stress test）"""
        self.preset_dead_loop()
        self.preset_partial_failure()
        self.preset_rate_limit_burst()
        self.preset_unknown_tool()

    # ============================================================
    # 7. 状态查询
    # ============================================================
    def get_status(self) -> Dict[str, Any]:
        """返回当前注入状态。"""
        return {
            "pending_tool_errors": {
                k: len(v) for k, v in self._tool_errors.items()
            },
            "pending_api_errors": self._api_error_remaining,
            "pending_unknown_tool": self._inject_unknown_tool is not None,
            "loop_pattern_repeat": self._loop_repeat,
        }

    def is_clean(self) -> bool:
        """检查是否所有注入已消耗完毕。"""
        return (
            not any(self._tool_errors.values())
            and self._api_error_remaining == 0
            and self._inject_unknown_tool is None
            and self._loop_repeat == 0
        )
