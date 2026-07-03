#!/usr/bin/env python3
"""
模块 3：Context 管理 —— 策略对比基准测试
===========================================
自动复现 context 爆掉，并对比三种管理策略的表现。

用法：
    export LLM_API_KEY="sk-xxxxx"
    export LLM_BASE_URL="https://api.moonshot.cn/v1"
    export LLM_MODEL="moonshot-v1-8k"
    uv run python benchmark.py
"""

import os
import json
import httpx
from typing import List, Dict, Any

from context_manager import ContextManager, estimate_messages_tokens
from tools import get_tools, execute_tool


API_KEY = os.getenv("LLM_API_KEY")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}",
}
URL = f"{BASE_URL.rstrip('/')}/chat/completions"

SYSTEM_PROMPT = (
    "你是一个聪明的 Agent，可以调用工具帮用户完成任务。"
    "请一步一步思考，每次调用一个工具。"
)

# 固定测试任务：先产生超长输出，再追问细节
TEST_TASKS = [
    "请生成 80 段超长文本。",
    "请总结第 10 段的主要内容。",
    "第 50 段和第 10 段的内容有什么共同点？",
    "请告诉我第 75 段开头的几个字。",
]


def run_one_strategy(strategy: str, max_tokens: int = 2000) -> Dict[str, Any]:
    """用指定策略跑完一组任务，返回统计信息。"""
    if not API_KEY:
        raise RuntimeError("请先设置 LLM_API_KEY")

    client = httpx.Client(timeout=60.0)
    cm = ContextManager(
        max_tokens=max_tokens,
        strategy=strategy,
        client=client,
        headers=HEADERS,
        url=URL,
        model=MODEL,
    )
    cm.set_system_prompt(SYSTEM_PROMPT)

    tools = get_tools()
    step_count = 0
    max_steps = 20
    compressions: List[Dict[str, Any]] = []
    token_history: List[int] = []
    final_answers: List[str] = []

    for task_idx, task in enumerate(TEST_TASKS):
        cm.add({"role": "user", "content": task})
        print(f"\n📌 任务 {task_idx + 1}/{len(TEST_TASKS)}: {task}")

        answered = False
        while step_count < max_steps and not answered:
            step_count += 1

            info = cm.fit()
            if info["compressed"]:
                compressions.append(info)
                print(f"   🗜️ 压缩: {info['before_tokens']} → {info['after_tokens']} ({info['strategy']})")

            messages = cm.get_messages()
            current_tokens = estimate_messages_tokens(messages)
            token_history.append(current_tokens)
            print(f"   📏 当前 token: {current_tokens}")

            payload = {
                "model": MODEL,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "temperature": 0.3,
            }

            try:
                resp = client.post(URL, headers=HEADERS, json=payload)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"   ❌ API 请求失败: {e}")
                final_answers.append(f"请求失败: {e}")
                answered = True
                break

            choice = data["choices"][0]
            message = choice["message"]
            finish_reason = choice["finish_reason"]

            if finish_reason == "stop":
                reply = message.get("content", "")
                print(f"   ✅ 回答: {reply[:120]}...")
                final_answers.append(reply)
                cm.add(message)
                answered = True
            elif finish_reason == "tool_calls":
                cm.add(message)
                for tc in message.get("tool_calls", []):
                    func_name = tc["function"]["name"]
                    args = json.loads(tc["function"]["arguments"])
                    result = execute_tool(func_name, args)
                    print(f"   🔧 调用 {func_name}")
                    cm.add({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": func_name,
                        "content": str(result),
                    })
            else:
                final_answers.append(f"异常 finish_reason: {finish_reason}")
                answered = True

    client.close()
    return {
        "strategy": strategy,
        "steps": step_count,
        "compressions": len(compressions),
        "max_tokens": max(token_history) if token_history else 0,
        "final_tokens": token_history[-1] if token_history else 0,
        "answers": final_answers,
    }


def main():
    print("=" * 70)
    print("🧪 模块 3 Context 管理策略对比")
    print(f"   模型: {MODEL}")
    print("=" * 70)

    results = []
    for strategy in ("truncate", "summarize", "memory"):
        print(f"\n\n{'='*70}")
        print(f"🔬 测试策略: {strategy}")
        print("=" * 70)
        result = run_one_strategy(strategy)
        results.append(result)

    print("\n\n" + "=" * 70)
    print("📊 对比结果")
    print("=" * 70)
    print(f"{'策略':<12} {'总步数':<8} {'压缩次数':<10} {'峰值 token':<12} {'最终 token':<12}")
    print("-" * 70)
    for r in results:
        print(
            f"{r['strategy']:<12} {r['steps']:<8} {r['compressions']:<10} "
            f"{r['max_tokens']:<12} {r['final_tokens']:<12}"
        )

    print("\n💡 解读：")
    print("- truncate：最简单，但会丢失早期信息，可能导致模型忘记前文。")
    print("- summarize：保留关键信息，但需要额外调用模型，成本更高。")
    print("- memory：把完整信息外置，messages 最精简，但需要时再读文件。")


if __name__ == "__main__":
    main()
