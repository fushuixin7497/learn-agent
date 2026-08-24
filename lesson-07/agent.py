#!/usr/bin/env python3
"""
模块 7：Least-Privilege Tooling —— 最小权限 Agent
==================================================
在 lesson-06 基础上加入权限控制：

    PERMISSION_MODE=read_only      # 默认：拦截所有写入/网络/危险操作
    PERMISSION_MODE=confirm        # 副作用工具需要用户确认
    PERMISSION_MODE=unrestricted   # 显式授权后放行（教学演示，生产不推荐）

运行方式：
    export LLM_API_KEY="sk-xxxxx"
    export LLM_BASE_URL="https://api.moonshot.cn/v1"
    export LLM_MODEL="moonshot-v1-8k"

    # 默认只读模式
    uv run python agent.py

    # 需要确认副作用
    export PERMISSION_MODE=confirm
    uv run python agent.py

    # 显式授权（演示用）
    export PERMISSION_MODE=unrestricted
    uv run python agent.py
"""

import os
import sys
import json
import time
from typing import List, Dict, Any
import httpx

from context_manager import ContextManager, estimate_messages_tokens
from permissions import (
    PermissionMode,
    PermissionContext,
    parse_permission_mode,
    build_permission_report,
)
from tools import get_tools, execute_tool, TOOL_REGISTRY, get_tool_danger_label, SANDBOX_ROOT

# ============================================================
# 配置
# ============================================================
API_KEY = os.getenv("LLM_API_KEY")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
CONTEXT_STRATEGY = os.getenv("CONTEXT_STRATEGY", "truncate")
MAX_CONTEXT_TOKENS = int(os.getenv("MAX_CONTEXT_TOKENS", "2000"))

_PERMISSION_MODE = parse_permission_mode(os.getenv("PERMISSION_MODE", "read_only"))

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
    "当前系统运行在【最小权限模式】下，请遵守以下安全规则：\n"
    "1. 默认只读，所有写入、删除、网络、执行类操作都需要用户显式授权。\n"
    "2. 文件操作只能在沙箱目录内进行，路径严禁逃逸。\n"
    "3. 用户提到的文件名、路径、关键词必须原样使用，严禁擅自修改拼写。\n"
    "4. write_file 默认不会覆盖已有文件，如需覆盖请设置 overwrite=true。\n"
    "5. 如果工具返回'权限拒绝'，说明当前模式不允许该操作，不要反复调用同一工具。\n"
    "6. 一步一步思考，每次调用一个工具。"
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
    """调用模型，网络类错误做指数退避重试。"""
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
# 交互式确认回调
# ============================================================
def make_confirm_callback(auto_approve: bool = False):
    """返回一个确认回调函数。auto_approve=True 用于测试自动放行。"""
    def callback(prompt: str, arguments: Dict[str, Any]) -> bool:
        if auto_approve:
            return True
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes", "是", "确认", "允许")
    return callback


# ============================================================
# Agent Loop with Least-Privilege
# ============================================================
def agent_loop(user_input: str, auto_approve: bool = False) -> str:
    client = httpx.Client(timeout=60.0)
    ctx = PermissionContext(mode=_PERMISSION_MODE)

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
    confirm_callback = make_confirm_callback(auto_approve=auto_approve)

    while step < max_steps:
        step += 1
        print(f"\n🔄 === Step {step}: 调用模型 ===")

        info = cm.fit()
        if info["compressed"]:
            print(f"🗜️ Context 压缩 [{info['strategy']}]")
            print(f"   压缩前: {info['before_tokens']} tokens → 压缩后: {info['after_tokens']} tokens")
            print(f"   详情: {info['detail'].strip()}")

        messages = cm.get_messages()
        print(f"📏 当前 messages 估算 token: {estimate_messages_tokens(messages)}")

        result = call_model(client, messages, tools)
        if not result["ok"]:
            print(f"❌ 模型请求失败: {result['error']}")
            return f"模型请求失败: {result['error']}"

        data = result["data"]
        choice = data["choices"][0]
        message = choice["message"]
        finish_reason = choice["finish_reason"]

        if finish_reason == "stop":
            reply = message.get("content", "")
            print(f"🤖 模型直接回答: {reply}")
            return reply

        if finish_reason == "tool_calls":
            cm.add(message)
            tool_calls = message.get("tool_calls", [])
            print(f"🔧 模型决定调用 {len(tool_calls)} 个工具")

            for tc in tool_calls:
                tc_id = tc["id"]
                func_name = tc["function"]["name"]
                func_args_json = tc["function"]["arguments"]

                danger_label = get_tool_danger_label(func_name)
                print(f"   ├─ 工具: {func_name} [{danger_label}]")
                print(f"   │   参数: {func_args_json}")

                try:
                    args = json.loads(func_args_json)
                except json.JSONDecodeError as e:
                    result = f"错误：参数 JSON 解析失败: {e}"
                    print(f"   └─ ❌ {result}")
                    cm.add({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": func_name,
                        "content": str(result),
                    })
                    continue

                # 在权限上下文下执行工具
                real_result = execute_tool(func_name, args, ctx, confirm_callback=confirm_callback)

                is_error = (
                    real_result.startswith("错误：")
                    or real_result.startswith("❌")
                    or real_result.startswith("工具执行异常")
                )

                if is_error:
                    print(f"   └─ ❌ {real_result[:200]}")
                else:
                    print(f"   └─ ✅ {real_result[:200]}")

                cm.add({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": func_name,
                    "content": str(real_result),
                })

            continue

        print(f"⚠️ 异常终止: finish_reason={finish_reason}")
        return message.get("content", "")

    print("❌ 达到最大步数限制")
    return "执行步数过多，已强制终止。"


# ============================================================
# REPL 入口
# ============================================================
def main():
    tool_names = list(TOOL_REGISTRY.keys())
    mode_label = {
        PermissionMode.READ_ONLY: "只读模式（默认最小权限）",
        PermissionMode.CONFIRM: "确认模式（副作用需确认）",
        PermissionMode.UNRESTRICTED: "显式授权模式（教学演示）",
    }.get(_PERMISSION_MODE, _PERMISSION_MODE.value)

    # 确保沙箱目录存在
    os.makedirs(SANDBOX_ROOT, exist_ok=True)

    print("=" * 60)
    print("🔒 模块 7：Least-Privilege Tooling —— 最小权限 Agent")
    print(f"   模型: {MODEL}")
    print(f"   权限模式: {mode_label}")
    print(f"   沙箱目录: {SANDBOX_ROOT}")
    print(f"   Context 策略: {CONTEXT_STRATEGY}")
    print(f"   Context 阈值: {MAX_CONTEXT_TOKENS} tokens")
    print(f"   工具: {', '.join(tool_names)}")
    print("=" * 60)
    print("输入 'quit' 退出，输入 'report' 查看权限报告")
    print("提示：试试 '把 hello 写入 ~/learn-agent-sandbox/demo.txt'")
    print("-" * 60)

    ctx = PermissionContext(mode=_PERMISSION_MODE)

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
        if user_input.lower() == "report":
            print(build_permission_report(ctx))
            continue

        final_answer = agent_loop(user_input)
        print(f"\n✅ Final Answer:\n{final_answer}")


if __name__ == "__main__":
    main()
