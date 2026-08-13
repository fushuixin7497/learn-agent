#!/usr/bin/env python3
"""
模块 6-01：轻量版 Graceful Degradation
=====================================
只围绕一个例子——查询天气——演示"外部 API 失败时如何优雅降级"。

降级路径：
    1. 首选 query_weather_api（模拟外部 API，可能 503 失败）
    2. API 失败时，Agent 改用 read_local_weather 读取本地 weather.json
    3. 本地文件也失败时，返回清晰的状态说明，而不是抛异常

运行方式：
    cd ~/learn-agent/lesson-06-01
    export LLM_API_KEY="sk-xxxxx"
    export LLM_BASE_URL="https://api.moonshot.cn/v1"
    export LLM_MODEL="moonshot-v1-8k"
    uv run python agent.py

演示方式：
    # 正常模式：API 有 50% 概率失败，失败后自动读本地文件
    uv run python agent.py

    # 强制 API 失败：观察降级到本地文件
    export WEATHER_API_FAIL=1
    uv run python agent.py

    # 强制 API 和本地文件都失败：观察优雅失败
    export WEATHER_API_FAIL=1
    export WEATHER_FILE_PATH=/tmp/不存在的文件.json
    uv run python agent.py
"""

import json
import os
import random
import sys
import time
from typing import Any, Dict, List

import httpx


# ============================================================
# 配置
# ============================================================
API_KEY = os.getenv("LLM_API_KEY")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

WEATHER_FILE_PATH = os.getenv(
    "WEATHER_FILE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather.json"),
)
FORCE_API_FAIL = os.getenv("WEATHER_API_FAIL", "0") == "1"

if not API_KEY:
    print("❌ 请先设置环境变量 LLM_API_KEY")
    sys.exit(1)

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}",
}
URL = f"{BASE_URL.rstrip('/')}/chat/completions"

SYSTEM_PROMPT = (
    "你是一个天气查询助手。\n"
    "你有两个工具：\n"
    "1. query_weather_api(city): 优先调用外部天气 API 查询实时天气。\n"
    "2. read_local_weather(city): 当外部 API 不可用时，读取本地 weather.json 获取天气。\n"
    "规则：\n"
    "- 一次只调用一个工具。\n"
    "- 如果 query_weather_api 返回 503/服务不可用，必须改用 read_local_weather。\n"
    "- 如果本地文件也没有该城市，向用户说明情况并给出建议。\n"
    "- 回答要简洁，包含城市、温度、天气状况、更新时间。"
)


# ============================================================
# 工具实现
# ============================================================
def query_weather_api(city: str) -> str:
    """
    模拟外部天气 API。
    默认 50% 概率返回 503，用于演示降级；设置 WEATHER_API_FAIL=1 则必定失败。
    """
    if not isinstance(city, str) or not city.strip():
        return "错误：city 必须是非空字符串"

    if FORCE_API_FAIL or random.random() < 0.5:
        return (
            f"错误：外部天气 API 服务暂时不可用（503 Service Unavailable），"
            f"无法查询 '{city}'。请改用本地天气文件。"
        )

    # 模拟 API 成功返回
    return (
        f"✅ 外部 API 查询成功：{city} 当前温度 29℃，晴，湿度 50%，"
        f"更新时间 2026-08-11 15:00。"
    )


def read_local_weather(city: str) -> str:
    """读取本地 weather.json 作为降级数据源。"""
    if not isinstance(city, str) or not city.strip():
        return "错误：city 必须是非空字符串"

    if not os.path.exists(WEATHER_FILE_PATH):
        return f"错误：本地天气文件不存在 '{WEATHER_FILE_PATH}'"

    try:
        with open(WEATHER_FILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return f"错误：读取本地天气文件失败：{type(e).__name__}: {e}"

    # 支持拼音 / 中文 / 英文大小写 模糊匹配
    city_key = city.lower().strip()
    record = data.get(city_key)

    if not record:
        # 尝试用中文名反向查找
        for key, value in data.items():
            if value.get("city") == city.strip():
                record = value
                break

    if not record:
        available = ", ".join(f"{k}({v.get('city')})" for k, v in data.items())
        return (
            f"错误：本地天气文件中没有 '{city}' 的数据。"
            f"可用城市：{available}"
        )

    return (
        f"✅ 本地文件查询成功：{record['city']} 当前温度 {record['temperature']}℃，"
        f"{record['condition']}，湿度 {record['humidity']}%，"
        f"风力 {record['wind']}，更新时间 {record['updated_at']}。"
    )


# 工具注册表
TOOL_REGISTRY: Dict[str, Any] = {
    "query_weather_api": query_weather_api,
    "read_local_weather": read_local_weather,
}

# 工具 schema（OpenAI Function Calling 格式）
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_weather_api",
            "description": (
                "调用外部天气 API 查询指定城市的实时天气。"
                "该服务可能不可用（503），如果失败请改用 read_local_weather。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名，例如 beijing、北京、Shanghai",
                    }
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_local_weather",
            "description": (
                "当外部天气 API 不可用时，读取本地 weather.json 获取天气。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名，例如 beijing、北京、Shanghai",
                    }
                },
                "required": ["city"],
            },
        },
    },
]


# ============================================================
# API 调用
# ============================================================
def call_model(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """调用模型，网络错误时返回结构化错误信息，而不是抛异常。"""
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.3,
    }

    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(URL, headers=HEADERS, json=payload)
            resp.raise_for_status()
            return {"ok": True, "data": resp.json()}
    except httpx.ConnectError as e:
        return {"ok": False, "error": f"网络连接错误：无法连接到模型服务。详情: {e}"}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"API 返回错误：{e.response.status_code} - {e.response.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": f"请求异常：{type(e).__name__}: {e}"}


# ============================================================
# 工具执行
# ============================================================
def execute_tool(name: str, arguments: Dict[str, Any]) -> str:
    """规范：错误作为正常返回值，保证 loop 不崩溃。"""
    if name not in TOOL_REGISTRY:
        return f"错误：未知工具 '{name}'"

    func = TOOL_REGISTRY[name]
    try:
        result = func(**arguments)
    except Exception as e:
        return f"工具执行异常 ({name}): {type(e).__name__}: {e}"

    return str(result)


def is_error_result(result: str) -> bool:
    """判断工具返回是否表示失败。"""
    return result.startswith("错误：") or result.startswith("工具执行异常")


# ============================================================
# Agent 主循环
# ============================================================
def agent_loop(user_input: str) -> str:
    """
    极简 Agent 循环：
    - 模型直接回答 → 返回
    - 模型调用工具 → 执行工具，失败时给模型提示换路径
    - 最多 8 步
    """
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_input},
    ]

    completed_steps: List[Dict[str, Any]] = []
    failed_steps: List[Dict[str, Any]] = []
    step = 0
    max_steps = 8

    while step < max_steps:
        step += 1
        print(f"\n🔄 === Step {step}: 调用模型 ===")

        result = call_model(messages)
        if not result["ok"]:
            print(f"❌ Step {step}: 模型请求失败")
            return graceful_failure_report(user_input, completed_steps, failed_steps, result["error"])

        choice = result["data"]["choices"][0]
        message = choice["message"]
        finish_reason = choice["finish_reason"]

        # 情况 A：模型直接回答
        if finish_reason == "stop":
            reply = message.get("content", "")
            print(f"🤖 模型直接回答: {reply}")

            if completed_steps or failed_steps:
                # 如果前面有工具调用，汇总状态
                return status_report(user_input, completed_steps, failed_steps, reply)
            return reply

        # 情况 B：模型调用工具
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

                # 解析参数
                try:
                    args = json.loads(func_args_json)
                except json.JSONDecodeError as e:
                    error = f"错误：参数 JSON 解析失败: {e}"
                    print(f"   └─ ❌ {error}")
                    failed_steps.append({"tool": func_name, "error": error})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": func_name,
                        "content": error,
                    })
                    continue

                # 执行工具
                tool_result = execute_tool(func_name, args)

                if is_error_result(tool_result):
                    failed_steps.append({"tool": func_name, "args": args, "error": tool_result})
                    print(f"   └─ ❌ 工具失败: {tool_result[:120]}")
                    # 把失败信息 + 换路径提示回填给模型
                    hint = (
                        f"{tool_result}\n"
                        f"💡 该路径不可用，请换用其他工具完成用户请求，不要重复同样的失败调用。"
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": func_name,
                        "content": hint,
                    })
                else:
                    completed_steps.append({"tool": func_name, "args": args, "result": tool_result})
                    print(f"   └─ ✅ {tool_result[:120]}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": func_name,
                        "content": tool_result,
                    })

            continue

        # 其他异常 finish_reason
        print(f"⚠️ Step {step}: 异常终止 finish_reason={finish_reason}")
        return message.get("content", "")

    # 超过最大步数
    print(f"❌ Step {step}: 达到最大步数限制")
    return graceful_failure_report(
        user_input, completed_steps, failed_steps, "执行步数过多，已强制终止"
    )


# ============================================================
# 状态报告
# ============================================================
def _preview(text: str, max_len: int = 80) -> str:
    """截取文本预览，避免重复省略号。"""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip(".") + "..."


def status_report(
    user_input: str,
    completed: List[Dict[str, Any]],
    failed: List[Dict[str, Any]],
    final_reply: str,
) -> str:
    """有工具调用历史时，把最终回答和运行状态一起呈现。"""
    lines = [final_reply]

    if completed:
        lines.append("\n✅ 已完成的步骤：")
        for item in completed:
            lines.append(f"  · {item['tool']}({item.get('args', {})}) → {_preview(item['result'])}")

    if failed:
        lines.append("\n⚠️ 遇到问题的步骤：")
        for item in failed:
            lines.append(f"  · {item['tool']}({item.get('args', {})}) → {_preview(item['error'])}")

    return "\n".join(lines)


def graceful_failure_report(
    user_input: str,
    completed: List[Dict[str, Any]],
    failed: List[Dict[str, Any]],
    reason: str,
) -> str:
    """所有路径都失败时，给出清晰说明，不暴露 stack trace。"""
    lines = [
        "【无法完成查询】",
        f"你的请求：{user_input}",
        f"原因：{reason}",
    ]

    if completed:
        lines.append("\n✅ 已完成的步骤：")
        for item in completed:
            lines.append(f"  · {item['tool']}({item.get('args', {})}) → {_preview(item['result'])}")

    if failed:
        lines.append("\n⚠️ 失败的步骤：")
        for item in failed:
            lines.append(f"  · {item['tool']}({item.get('args', {})}) → {_preview(item['error'])}")

    lines.append(
        "\n💡 建议：\n"
        "  1. 稍后重试 query_weather_api（服务恢复后即可查询）；\n"
        "  2. 检查 weather.json 中是否有该城市数据，并确认文件路径正确；\n"
        "  3. 检查 LLM_API_KEY 和网络连接。"
    )

    return "\n".join(lines)


# ============================================================
# REPL 入口
# ============================================================
def main():
    api_mode = "强制失败" if FORCE_API_FAIL else "50% 概率失败"

    print("=" * 60)
    print("🌤️  模块 6-01：天气查询与优雅降级（轻量版）")
    print(f"   模型: {MODEL}")
    print(f"   外部天气 API: {api_mode}")
    print(f"   本地天气文件: {WEATHER_FILE_PATH}")
    print("=" * 60)
    print("输入示例：北京天气怎么样？ / 查询上海天气")
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
        print(f"\n✅ Final Answer:\n{final_answer}")


if __name__ == "__main__":
    main()
