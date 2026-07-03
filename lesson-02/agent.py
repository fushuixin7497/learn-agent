#!/usr/bin/env python3
"""
Agent 开发第二课：手写 Agent Loop（带工具的 REPL 循环）
=========================================================
学习目标：理解 Agent 的本质是一个"状态机"循环：
    调模型 → 解析工具调用 → 执行工具 → 结果回填 context → 重复直到完成

核心协议：OpenAI 兼容的 Function Calling（tool_use）
- 请求时传入 tools 数组（含 name/description/parameters schema）
- 模型决定调用哪个工具，返回 tool_calls
- 我们把工具执行结果以 role="tool" 回填 messages，再调模型
- 当模型 finish_reason="stop" 时终止循环，输出最终答案

运行方式：
    export LLM_API_KEY="sk-xxxxx"
    export LLM_BASE_URL="https://api.moonshot.cn/v1"  # 或 OpenAI / DeepSeek
    export LLM_MODEL="moonshot-v1-8k"
    uv run python agent.py
"""

import os
import sys
import json
import httpx
import subprocess

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
# 工具注册表（Tool Registry）
# ------------------------------------------------------------
# 每个工具是普通的 Python 函数。
# Agent 通过 "name" 字符串映射到函数，通过 JSON 传参调用。
# ============================================================

def calculator(expression: str) -> str:
    """安全计算器：计算数学表达式。"""
    try:
        # 只允许数字和基本运算符，防止代码注入
        allowed = set("0123456789.+-*/() ")
        if not all(c in allowed for c in expression):
            return "错误：表达式包含非法字符"
        result = eval(expression, {"__builtins__": {}}, {})
        return str(result)
    except Exception as e:
        return f"计算错误: {e}"


def read_file(filepath: str) -> str:
    """读取指定文件的内容。"""
    try:
        # 安全：禁止读取上级目录外的敏感文件
        abs_path = os.path.abspath(filepath)
        home = os.path.expanduser("~")
        if not abs_path.startswith(home):
            return "错误：只能读取用户主目录下的文件"
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"读取错误: {e}"


def run_command(command: str) -> str:
    """在 shell 中执行命令并返回输出。"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=os.path.expanduser("~"),
        )
        output = result.stdout.strip()
        err = result.stderr.strip()
        if result.returncode != 0:
            return f"退出码 {result.returncode}\nstderr: {err}\nstdout: {output}"
        return output or "(命令执行成功，无输出)"
    except Exception as e:
        return f"执行错误: {e}"


# 工具名 → Python 函数的映射字典
TOOL_REGISTRY = {
    "calculator": calculator,
    "read_file": read_file,
    "run_command": run_command,
}

# ============================================================
# Tool Schema（告诉模型：我有哪些工具、每个工具怎么用）
# ------------------------------------------------------------
# 必须符合 JSON Schema 格式，模型靠 description 理解用途，
# 靠 parameters 知道该传什么参数。
# ============================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "执行数学计算，例如加减乘除、括号运算。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "数学表达式，例如 '365 * 24' 或 '(1+2)*3'",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取本地文件的内容，用于获取文件中的信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "文件的绝对路径或相对路径，例如 '/Users/albert/learn-agent/lesson-01/chat.py'",
                    }
                },
                "required": ["filepath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "在终端执行 shell 命令，例如查看目录、统计文件大小等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的 shell 命令，例如 'ls -la' 或 'du -sh ~/Downloads'",
                    }
                },
                "required": ["command"],
            },
        },
    },
]


# ============================================================
# Agent Loop 核心（约 60 行）
# ------------------------------------------------------------
# 状态机：
#   STATE_CALL_MODEL  →  发请求给模型（带上 tools）
#   STATE_PARSE        →  看 finish_reason：
#                        - "stop" → 结束，输出答案
#                        - "tool_calls" → 解析工具调用，执行，回填，回到 STATE_CALL_MODEL
# ============================================================
def agent_loop(user_input: str) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "你是一个聪明的 Agent。"
                "你可以调用工具来帮用户完成任务。"
                "如果任务需要多步操作，请一步一步来，每次调用一个工具。"
            ),
        },
        {"role": "user", "content": user_input},
    ]

    step = 0
    while True:
        step += 1
        print(f"\n🔄 === Step {step}: 调用模型 ===")

        payload = {
            "model": MODEL,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",  # 让模型自己决定要不要用工具
            "temperature": 0.3,     # Agent 思考需要确定性，温度低一些
        }

        with httpx.Client(timeout=60.0) as client:
            resp = client.post(URL, headers=HEADERS, json=payload)
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]
        message = choice["message"]
        finish_reason = choice["finish_reason"]

        # 情况 A：模型直接回答，不需要工具 → 终止循环
        if finish_reason == "stop":
            reply = message.get("content", "")
            print(f"🤖 模型直接回答（无需工具）: {reply}")
            return reply

        # 情况 B：模型要求调用工具 → 执行并回填
        if finish_reason == "tool_calls":
            # 先把模型的"思考/决定"加入上下文（role=assistant，含 tool_calls）
            messages.append(message)

            tool_calls = message.get("tool_calls", [])
            print(f"🔧 模型决定调用 {len(tool_calls)} 个工具")

            for tc in tool_calls:
                tc_id = tc["id"]
                tc_type = tc["type"]
                func_name = tc["function"]["name"]
                func_args_json = tc["function"]["arguments"]

                print(f"   ├─ 工具: {func_name}")
                print(f"   │   参数: {func_args_json}")

                # 解析参数并执行对应 Python 函数
                try:
                    args = json.loads(func_args_json)
                except json.JSONDecodeError:
                    result = "错误：参数不是合法 JSON"
                else:
                    if func_name not in TOOL_REGISTRY:
                        result = f"错误：未知工具 '{func_name}'"
                    else:
                        func = TOOL_REGISTRY[func_name]
                        result = func(**args)

                print(f"   └─ 结果: {result[:200]}{'...' if len(result) > 200 else ''}")

                # 关键：把工具执行结果以 role="tool" 回填 messages
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": func_name,
                    "content": str(result),
                })

            # 继续循环：把带工具结果的新 messages 再发给模型
            continue

        # 其他 finish_reason（如 length, content_filter）
        print(f"⚠️ 异常终止: finish_reason={finish_reason}")
        return message.get("content", "")


# ============================================================
# REPL 入口
# ============================================================
def main():
    print("=" * 60)
    print("🤖 Agent Loop —— 最小多步 Agent")
    print(f"   模型: {MODEL}")
    print(f"   接口: {BASE_URL}")
    print("=" * 60)
    print("可用工具: calculator | read_file | run_command")
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
