#!/usr/bin/env python3
"""
模块 6：Graceful Degradation —— 失败时降级而非崩溃的 Agent
=============================================================
在 lesson-04（Factor 4：Context 管理）基础上，加入"降级模式"开关：

    DEGRADE_MODE=off     # 关闭降级，行为同 lesson-04
    DEGRADE_MODE=on      # 启用降级（默认）
    DEGRADE_MODE=forced  # 强制每个工具都不可用，演示降级路径

运行方式：
    export LLM_API_KEY="sk-xxxxx"
    export LLM_BASE_URL="https://api.moonshot.cn/v1"
    export LLM_MODEL="moonshot-v1-8k"

    # 默认开启降级
    uv run python agent.py

    # 模拟所有工具不可用，观察 Agent 给出替代方案
    export DEGRADE_MODE=forced
    uv run python agent.py

    # 关闭降级对比
    export DEGRADE_MODE=off
    uv run python agent.py
"""

import os
import sys
import json
import time
from typing import List, Dict, Any
import httpx

from context_manager import ContextManager, estimate_messages_tokens
from tools import get_tools, execute_tool, ENABLE_MISLEADING_TOOL, TOOL_REGISTRY
from degradation import (
    DegradationState,
    MAX_NONSENSE_RETRIES,
    is_degradation_enabled,
    should_force_degradation,
    maybe_degrade_tool_result,
    is_nonsense_reply,
    downgrade_messages,
    build_tool_unavailable_hint,
    generate_fallback_reply,
    generate_graceful_failure,
)
from fallback_templates import status_line

# ============================================================
# 配置
# ============================================================
API_KEY = os.getenv("LLM_API_KEY")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
CONTEXT_STRATEGY = os.getenv("CONTEXT_STRATEGY", "truncate")
MAX_CONTEXT_TOKENS = int(os.getenv("MAX_CONTEXT_TOKENS", "2000"))

_DEGRADE_MODE = os.getenv("DEGRADE_MODE", "on").lower()
if _DEGRADE_MODE not in ("off", "on", "forced"):
    print(f"❌ DEGRADE_MODE 必须是 off/on/forced 之一，当前: {_DEGRADE_MODE}")
    sys.exit(1)

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
    "2. 如果工具返回错误，请仔细阅读错误信息，修正参数后再次尝试，或换用其他工具。\n"
    "3. write_file 默认不会覆盖已有文件，如需覆盖请设置 overwrite=true。\n"
    "4. 如果文件已存在且内容一致（幂等），无需重复写入。\n"
    "5. 一步一步思考，每次调用一个工具。\n"
    "6. 如果上下文很长，注意利用已有信息，不要重复请求相同工具。\n"
    "7. 【降级规则】如果某个工具不可用，请尝试替代方案，而不是反复调用同一个失败工具。\n"
    "8. 【降级规则】如果无法完成全部任务，请优先保留已完成部分，并说明失败原因。"
)

# ============================================================
# API 调用（带简单重试）
# ============================================================
def call_model(
    client: httpx.Client,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    max_retries: int = 2,
) -> Dict[str, Any]:
    """调用模型，网络类错误做指数退避重试，返回原始 data。"""
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0.3,
    }

    for attempt in range(max_retries + 1):
        try:
            resp = client.post(URL, headers=HEADERS, json=payload)
            resp.raise_for_status()
            return {"ok": True, "data": resp.json()}
        except httpx.ConnectError as e:
            error = f"网络连接错误：无法连接到模型服务。详情: {e}"
        except httpx.HTTPStatusError as e:
            error = f"API 返回错误：{e.response.status_code} - {e.response.text[:200]}"
        except Exception as e:
            error = f"请求异常：{type(e).__name__}: {e}"

        if attempt < max_retries:
            wait = 2 ** attempt
            print(f"🔄 Step 0 [retry]: 模型请求失败，{wait}s 后重试... ({error[:80]})")
            time.sleep(wait)
        else:
            return {"ok": False, "error": error}

    return {"ok": False, "error": "未知请求错误"}


# ============================================================
# Agent Loop with Graceful Degradation
# ============================================================
def agent_loop(user_input: str) -> str:
    client = httpx.Client(timeout=60.0)
    state = DegradationState(user_input=user_input, degrade_mode=_DEGRADE_MODE)

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
    max_steps = 12
    degraded_once = False  # 是否已经做过 prompt 降级

    while step < max_steps:
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

        # 调用模型
        result = call_model(client, messages, tools)
        if not result["ok"]:
            print(status_line(step, "failed", result["error"]))
            # 网络/API 完全失败，走优雅失败
            return generate_graceful_failure(state, result["error"])

        data = result["data"]
        choice = data["choices"][0]
        message = choice["message"]
        finish_reason = choice["finish_reason"]

        # 情况 A：任务完成（stop）
        if finish_reason == "stop":
            reply = message.get("content", "")
            print(f"🤖 模型直接回答: {reply}")

            # 如果启用了降级，检测是否是乱答/拒绝
            if is_degradation_enabled() and is_nonsense_reply(reply):
                state.nonsense_count += 1
                print(status_line(step, "degrade", f"检测到模型乱答/拒绝（{state.nonsense_count}/{MAX_NONSENSE_RETRIES}）"))

                if state.nonsense_count > MAX_NONSENSE_RETRIES:
                    print(status_line(step, "fallback", "达到最大乱答次数，使用安全兜底"))
                    return generate_fallback_reply(state)

                # 第一次：用更简单 prompt 重试
                if not degraded_once:
                    degraded_once = True
                    simpler = downgrade_messages(cm.get_messages())
                    # 用简化后的 system prompt 替换当前 messages
                    cm = ContextManager(
                        max_tokens=MAX_CONTEXT_TOKENS,
                        strategy=CONTEXT_STRATEGY,
                        client=client,
                        headers=HEADERS,
                        url=URL,
                        model=MODEL,
                    )
                    for m in simpler:
                        cm.add(m)
                    print(status_line(step, "degrade", "已切换为更简单 prompt 重试"))
                    continue
                else:
                    # 已经降过级还是乱答，使用兜底
                    print(status_line(step, "fallback", "简化 prompt 后仍乱答，使用安全兜底"))
                    return generate_fallback_reply(state)

            # 正常完成
            state.final_status = "success"
            if state.completed_tools or state.failed_tools:
                # 如果前面有步骤完成/失败，给一份状态汇总
                state.status_message = "任务已完成。"
                return state.to_user_report()
            return reply

        # 情况 B：调用工具
        if finish_reason == "tool_calls":
            cm.add(message)
            tool_calls = message.get("tool_calls", [])
            print(f"🔧 模型决定调用 {len(tool_calls)} 个工具")

            for tc in tool_calls:
                tc_id = tc["id"]
                func_name = tc["function"]["name"]
                func_args_json = tc["function"]["arguments"]

                print(f"   ├─ 工具: {func_name}")
                print(f"   │   参数: {func_args_json}")

                # 解析参数
                try:
                    args = json.loads(func_args_json)
                except json.JSONDecodeError as e:
                    result = f"错误：参数 JSON 解析失败: {e}"
                    print(f"   └─ ❌ {result}")
                    state.record_tool_failure(func_name, args, result)
                    cm.add({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": func_name,
                        "content": str(result),
                    })
                    continue

                # 执行工具
                real_result = execute_tool(func_name, args)

                # forced 模式：模拟工具不可用
                if should_force_degradation():
                    real_result = maybe_degrade_tool_result(func_name, real_result)

                # 判断工具是否失败
                is_error = (
                    real_result.startswith("错误：")
                    or real_result.startswith("❌")
                    or real_result.startswith("工具执行异常")
                )

                if is_error:
                    state.record_tool_failure(func_name, args, real_result)
                    # 启用降级时，给模型"换路径"提示
                    if is_degradation_enabled():
                        hint = build_tool_unavailable_hint(func_name, real_result)
                        print(f"   └─ ❌ 工具失败，提示模型换路径")
                        print(f"      {real_result[:120]}")
                        cm.add({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "name": func_name,
                            "content": hint,
                        })
                        continue
                    else:
                        print(f"   └─ ❌ {real_result[:200]}")
                else:
                    state.record_tool_success(func_name, args, real_result)
                    print(f"   └─ ✅ {real_result[:200]}")

                # 回填工具结果
                cm.add({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": func_name,
                    "content": str(real_result),
                })

            # 一轮工具调用结束后，检查是否有成功完成的步骤
            if state.completed_tools and not state.failed_tools:
                state.final_status = "partial" if state.failed_tools else "success"

            continue

        # 其他异常情况
        print(f"⚠️ 异常终止: finish_reason={finish_reason}")
        return message.get("content", "")

    # 达到最大步数
    print(status_line(step, "failed", "达到最大步数限制"))
    state.final_status = "partial"
    state.status_message = "执行步数过多，已强制终止。"
    return state.to_user_report()


# ============================================================
# REPL 入口
# ============================================================
def main():
    tool_names = list(TOOL_REGISTRY.keys())
    if ENABLE_MISLEADING_TOOL:
        tool_names.append("do_something（模糊工具，观察误用）")

    degrade_label = {
        "off": "关闭",
        "on": "开启（默认）",
        "forced": "强制工具不可用（演示降级）",
    }.get(_DEGRADE_MODE, _DEGRADE_MODE)

    print("=" * 60)
    print("🔽 模块 6：Graceful Degradation —— 失败时降级而非崩溃")
    print(f"   模型: {MODEL}")
    print(f"   降级模式: {degrade_label}")
    print(f"   Context 策略: {CONTEXT_STRATEGY}")
    print(f"   Context 阈值: {MAX_CONTEXT_TOKENS} tokens")
    print(f"   工具: {', '.join(tool_names)}")
    print("=" * 60)
    print("输入 'quit' 退出")
    print("提示：试试 '用 external_api 查询天气，如果不可用就改用本地文件'")
    if _DEGRADE_MODE == "forced":
        print("🧪 当前为 forced 模式：所有工具调用都会返回'不可用'，观察 Agent 如何降级。")
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

        final_answer = agent_loop(user_input)
        print(f"\n✅ Final Answer:\n{final_answer}")


if __name__ == "__main__":
    main()
