#!/usr/bin/env python3
"""
模块 2：工具设计 —— Agent Loop（复用模块1结构，接入健壮工具）
=================================================================
运行方式：
    export LLM_API_KEY="sk-xxxxx"
    export LLM_BASE_URL="https://api.moonshot.cn/v1"
    export LLM_MODEL="moonshot-v1-8k"
    uv run python agent.py
"""

import os
import sys
import json
import httpx

from tools import get_tools, execute_tool, ENABLE_MISLEADING_TOOL, TOOL_REGISTRY

# ============================================================
# 配置
# ============================================================
API_KEY = os.getenv("LLM_API_KEY")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

if not API_KEY:
    print("❌ 请先设置环境变量 LLM_API_KEY")
    sys.exit(1)

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}",
}
URL = f"{BASE_URL.rstrip('/')}/chat/completions"


# ============================================================
# Agent Loop（约 70 行）
# ============================================================
def agent_loop(user_input: str) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "你是一个聪明的 Agent，可以调用工具帮用户完成任务。\n"
                "重要规则：\n"
                "1. 用户提到的文件名、路径、关键词必须原样使用，严禁擅自修改拼写。\n"
                "2. 如果工具返回错误，请仔细阅读错误信息，修正参数后再次尝试。\n"
                "3. write_file 默认不会覆盖已有文件，如需覆盖请设置 overwrite=true。\n"
                "4. 如果文件已存在且内容一致（幂等），无需重复写入。\n"
                "5. 一步一步思考，每次调用一个工具。"
            ),
        },
        {"role": "user", "content": user_input},
    ]

    tools = get_tools()
    step = 0
    max_steps = 10  # 防止无限循环的安全阀

    while step < max_steps:
        step += 1
        print(f"\n🔄 === Step {step}: 调用模型 ===")

        payload = {
            "model": MODEL,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.3,
        }

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(URL, headers=HEADERS, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.ConnectError as e:
            return f"网络连接错误：无法连接到模型服务。请检查网络、VPN 或代理设置。详情: {e}"
        except httpx.HTTPStatusError as e:
            return f"API 返回错误：{e.response.status_code} - {e.response.text[:200]}"
        except Exception as e:
            return f"请求异常：{type(e).__name__}: {e}"

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
            messages.append(message)
            tool_calls = message.get("tool_calls", [])
            print(f"🔧 模型决定调用 {len(tool_calls)} 个工具")

            for tc in tool_calls:
                tc_id = tc["id"]
                func_name = tc["function"]["name"]
                func_args_json = tc["function"]["arguments"]

                print(f"   ├─ 工具: {func_name}")
                print(f"   │   参数: {func_args_json}")

                # 解析参数（模型返回的 arguments 是 JSON 字符串）
                try:
                    args = json.loads(func_args_json)
                except json.JSONDecodeError as e:
                    result = f"错误：参数 JSON 解析失败: {e}"
                else:
                    # 核心：所有错误在 execute_tool 内部被捕获，返回字符串
                    result = execute_tool(func_name, args)

                preview = result[:300] + "..." if len(result) > 300 else result
                print(f"   └─ 结果: {preview}")

                # 回填工具结果
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": func_name,
                    "content": str(result),
                })

            continue

        # 其他异常情况
        print(f"⚠️ 异常终止: finish_reason={finish_reason}")
        return message.get("content", "")

    print("⚠️ 达到最大步数限制，强制终止循环")
    return "Agent 执行步数过多，已强制终止。"


# ============================================================
# REPL 入口
# ============================================================
def main():
    tool_names = list(TOOL_REGISTRY.keys())
    if ENABLE_MISLEADING_TOOL:
        tool_names.append("do_something（模糊工具，观察误用）")

    print("=" * 60)
    print("🔧 模块 2：工具设计 —— 健壮 Agent")
    print(f"   模型: {MODEL}")
    print(f"   工具: {', '.join(tool_names)}")
    print("=" * 60)
    print("输入 'quit' 退出")
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
        print(f"\n✅ Final Answer: {final_answer}")


if __name__ == "__main__":
    main()
