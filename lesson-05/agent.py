#!/usr/bin/env python3
"""
模块 5：Failure-First Design —— 带 Harness 防护的 Agent Loop
=================================================================
在 lesson-04 基础上，引入 AgentHarness 系统性处理各种失败模式。

运行方式：
    export LLM_API_KEY="sk-xxxxx"
    export LLM_BASE_URL="https://api.moonshot.cn/v1"
    export LLM_MODEL="moonshot-v1-8k"

    # 可选：启用错误注入测试
    export INJECT_ERRORS=dead_loop      # 死循环
    export INJECT_ERRORS=partial        # 部分失败
    export INJECT_ERRORS=rate_limit     # 限流
    export INJECT_ERRORS=unknown_tool   # 未知工具
    export INJECT_ERRORS=all              # 全部叠加

    uv run python agent.py
"""

import os
import sys
import json
import time
import httpx

from context_manager import ContextManager, estimate_messages_tokens
from harness import AgentHarness
from tools import get_tools, execute_tool, ENABLE_MISLEADING_TOOL, TOOL_REGISTRY
from injector import ErrorInjector

# ============================================================
# 配置
# ============================================================
API_KEY = os.getenv("LLM_API_KEY")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
CONTEXT_STRATEGY = os.getenv("CONTEXT_STRATEGY", "truncate")
MAX_CONTEXT_TOKENS = int(os.getenv("MAX_CONTEXT_TOKENS", "2000"))
INJECT_ERRORS = os.getenv("INJECT_ERRORS", "")

if CONTEXT_STRATEGY not in ("truncate", "summarize", "memory"):
    print(f"❌ CONTEXT_STRATEGY 必须是 truncate/summarize/memory 之一，当前: {CONTEXT_STRATEGY}")
    sys.exit(1)

if not API_KEY:
    print("❌ 请先设置环境变量 LLM_API_KEY")
    sys.exit(1)

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}",
}
URL = f"{BASE_URL.rstrip('/')}/chat/completions"

SYSTEM_PROMPT = (
    "你是一个聪明的 Agent，可以调用工具帮用户完成任务。\n"
    "重要规则：\n"
    "1. 用户提到的文件名、路径、关键词必须原样使用，严禁擅自修改拼写。\n"
    "2. 如果工具返回错误，请仔细阅读错误信息，修正参数后再次尝试。\n"
    "3. write_file 默认不会覆盖已有文件，如需覆盖请设置 overwrite=true。\n"
    "4. 如果文件已存在且内容一致（幂等），无需重复写入。\n"
    "5. 一步一步思考，每次调用一个工具。\n"
    "6. 如果上下文很长，注意利用已有信息，不要重复请求相同工具。\n"
    "7. 如果收到'目标偏离警告'，请检查当前操作是否与用户目标相关。\n"
    "8. 如果收到'检测到死循环'，请立即停止并告知用户。"
)


# ============================================================
# Agent Loop with Harness
# ============================================================
def agent_loop(user_input: str, injector: ErrorInjector = None) -> str:
    client = httpx.Client(timeout=60.0)
    harness = AgentHarness(max_steps=15)

    cm = ContextManager(
        max_tokens=MAX_CONTEXT_TOKENS,
        strategy=CONTEXT_STRATEGY,
        client=client,
        headers=HEADERS,
        url=URL,
        model=MODEL,
    )
    cm.set_system_prompt(SYSTEM_PROMPT)
    cm.add({"role": "user", "content": user_input})

    tools = get_tools()
    step = 0

    while True:
        step += 1
        print(f"\n🔄 === Step {step}: 调用模型 ===")

        # 每次调用前压缩 context
        info = cm.fit()
        if info["compressed"]:
            print(f"🗜️ Context 压缩 [{info['strategy']}]")
            print(f"   压缩前: {info['before_tokens']} tokens → 压缩后: {info['after_tokens']} tokens")
            print(f"   详情: {info['detail'].strip()}")

        messages = cm.get_messages()
        print(f"📏 当前 messages 估算 token: {estimate_messages_tokens(messages)}")

        # 添加恢复提示（如果有历史错误）
        recovery_hint = harness.get_recovery_hint()
        if recovery_hint:
            print(f"💡 恢复提示:\n{recovery_hint}")
            cm.add({"role": "user", "content": recovery_hint})
            messages = cm.get_messages()

        payload = {
            "model": MODEL,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.3,
        }

        # API 调用 + 重试逻辑
        data = None
        for attempt in range(harness.max_retries + 1):
            try:
                resp = client.post(URL, headers=HEADERS, json=payload)
                resp.raise_for_status()
                data = resp.json()
                harness.reset_api_retry()
                break
            except httpx.HTTPStatusError as e:
                should_retry, wait = harness.handle_api_error(e)
                if should_retry and attempt < harness.max_retries:
                    print(f"⏳ API 错误 {e.response.status_code}，{wait}秒后重试...")
                    time.sleep(wait)
                    continue
                return f"API 返回错误：{e.response.status_code} - {e.response.text[:200]}"
            except httpx.ConnectError as e:
                should_retry, wait = harness.handle_api_error(e)
                if should_retry and attempt < harness.max_retries:
                    print(f"⏳ 连接错误，{wait}秒后重试...")
                    time.sleep(wait)
                    continue
                return f"网络连接错误：无法连接到模型服务。请检查网络、VPN 或代理设置。详情: {e}"
            except Exception as e:
                return f"请求异常：{type(e).__name__}: {e}"

        if data is None:
            return "请求失败：无法获取模型响应。"

        choice = data["choices"][0]
        message = choice["message"]
        finish_reason = choice["finish_reason"]

        # 情况 A：任务完成
        if finish_reason == "stop":
            reply = message.get("content", "")
            print(f"🤖 模型直接回答: {reply}")
            return reply

        # 情况 B：调用工具
        if finish_reason == "tool_calls":
            tool_calls = message.get("tool_calls", [])

            # Harness：死循环检测
            dead_loop_msg = harness.check_dead_loop(step, tool_calls)
            if dead_loop_msg:
                print(f"🛑 {dead_loop_msg}")
                return dead_loop_msg

            # Harness：目标偏离检测
            drift_msg = harness.check_drift(user_input, tool_calls)
            if drift_msg:
                print(f"⚠️ {drift_msg}")
                # 把偏离警告加入 messages，让模型看到
                cm.add({"role": "user", "content": drift_msg})

            cm.add(message)
            print(f"🔧 模型决定调用 {len(tool_calls)} 个工具")

            for tc in tool_calls:
                tc_id = tc["id"]
                func_name = tc["function"]["name"]
                func_args_json = tc["function"]["arguments"]

                print(f"   ├─ 工具: {func_name}")
                print(f"   │   参数: {func_args_json}")

                # Harness：拦截未知工具
                unknown_msg = harness.intercept_unknown_tool(func_name)
                if unknown_msg:
                    print(f"   └─ ❌ {unknown_msg}")
                    result = unknown_msg
                    harness.record_partial_failure(func_name, unknown_msg, step)
                    cm.add({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": func_name,
                        "content": str(result),
                    })
                    continue

                # 解析参数
                try:
                    args = json.loads(func_args_json)
                except json.JSONDecodeError as e:
                    result = f"错误：参数 JSON 解析失败: {e}"
                    print(f"   └─ ❌ {result}")
                    harness.record_partial_failure(func_name, result, step)
                    cm.add({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": func_name,
                        "content": str(result),
                    })
                    continue

                # Harness：前置参数校验与修正
                intercept_msg, corrected_args = harness.intercept_invalid_args(func_name, args)
                if intercept_msg:
                    print(f"   ⚠️ {intercept_msg}")
                    args = corrected_args

                # 执行工具（支持错误注入）
                # 注意：使用 tools.execute_tool 模块路径，确保 injector 的 patch 生效
                import tools as _tools_module
                if injector and not injector.is_clean():
                    result = injector.run_with_injection(_tools_module.execute_tool, func_name, args)
                else:
                    result = _tools_module.execute_tool(func_name, args)

                # Harness：丰富错误信息
                if result.startswith("错误：") or result.startswith("❌"):
                    result = harness.enrich_error(result, func_name)
                    harness.record_partial_failure(func_name, result, step)

                preview = result[:300] + "..." if len(result) > 300 else result
                print(f"   └─ 结果: {preview}")

                # 回填工具结果
                cm.add({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": func_name,
                    "content": str(result),
                })

            continue

        # 其他异常情况
        print(f"⚠️ 异常终止: finish_reason={finish_reason}")
        return message.get("content", "")


# ============================================================
# REPL 入口
# ============================================================
def main():
    tool_names = list(TOOL_REGISTRY.keys())
    if ENABLE_MISLEADING_TOOL:
        tool_names.append("do_something（模糊工具，观察误用）")

    print("=" * 60)
    print("🛡️ 模块 5：Failure-First Design —— 健壮 Agent")
    print(f"   模型: {MODEL}")
    print(f"   Context 策略: {CONTEXT_STRATEGY}")
    print(f"   Context 阈值: {MAX_CONTEXT_TOKENS} tokens")
    print(f"   工具: {', '.join(tool_names)}")
    print("=" * 60)
    print("输入 'quit' 退出")
    print("提示：试试 '用 flaky_tool 查询数据库，失败就重试'")
    print("-" * 60)

    # 错误注入器
    injector = None
    if INJECT_ERRORS:
        injector = ErrorInjector()
        if INJECT_ERRORS == "dead_loop":
            injector.preset_dead_loop()
            print("🧪 已启用错误注入：死循环场景")
        elif INJECT_ERRORS == "partial":
            injector.preset_partial_failure()
            print("🧪 已启用错误注入：部分失败场景")
        elif INJECT_ERRORS == "rate_limit":
            injector.preset_rate_limit_burst()
            print("🧪 已启用错误注入：限流场景")
        elif INJECT_ERRORS == "unknown_tool":
            injector.preset_unknown_tool()
            print("🧪 已启用错误注入：未知工具场景")
        elif INJECT_ERRORS == "all":
            injector.preset_all()
            print("🧪 已启用错误注入：全部场景叠加")
        print("-" * 60)

    while True:
        try:
            user_input = input("\n👤 You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 Bye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit"):
            print("👋 Bye!")
            break

        final_answer = agent_loop(user_input, injector)
        print(f"\n✅ Final Answer: {final_answer}")


if __name__ == "__main__":
    main()
