#!/usr/bin/env python3
"""
Agent 开发第一课：最小 LLM 调用与多轮对话
=============================================
学习目标：
1. 理解 LLM API 的最小请求结构（system prompt / messages / model / stream）
2. 理解 HTTP 层面的交互：Headers + JSON Body + JSON Response
3. 观察多轮对话中 context（messages）的增长
4. 对比流式（stream=true）与非流式（stream=false）的差异
5. 学会读取 token 消耗（prompt_tokens / completion_tokens / total_tokens）

运行方式：
    export LLM_API_KEY="your-api-key"
    uv run python chat.py
"""

import os
import sys
import json
import httpx

# ============================================================
# 配置区：通过环境变量注入，避免密钥硬编码
# ============================================================
API_KEY = os.getenv("LLM_API_KEY")
# 默认使用 OpenAI；国内用户可改为 Kimi/DeepSeek 等兼容端点
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")
# 通过环境变量 LLM_STREAM=true 开启流式
STREAM = os.getenv("LLM_STREAM", "false").lower() == "true"

# ============================================================
# 核心概念 1：System Prompt
# ------------------------------------------------------------
# system 消息定义模型的"全局人设"和"行为边界"。
# 它不会显示在对话记录中，但会在每次请求时作为最高优先级指令注入。
# 例如："你是一个严谨的代码审查助手"、"回答要简短，使用中文"。
# ============================================================
SYSTEM_PROMPT = {
    "role": "system",
    "content": "你是一个有帮助的助手。回答简洁，必要时使用中文。",
}

# ============================================================
# 核心概念 2：Messages（对话上下文）
# ------------------------------------------------------------
# messages 是一个数组，按时间顺序保存 system / user / assistant 消息。
# 多轮对话的核心就是不断往这个数组追加 user 和 assistant 消息，
# 然后整个数组作为 context 再次发给模型。
# 
# ⚠️ 注意：上下文越长，消耗的 prompt_tokens 越多，费用越高，
# 且可能触发模型的 max_context_length 限制而被截断。
# ============================================================
messages = [SYSTEM_PROMPT]


def chat_once(user_input: str) -> dict:
    """
    发送一次聊天请求。
    
    参数:
        user_input: 用户当前输入的文本
        
    返回:
        dict，包含:
        - reply: 助手的完整回复文本
        - prompt_tokens: 输入 token 数（含历史上下文）
        - completion_tokens: 输出 token 数
        - total_tokens: 总 token 数
    """
    # 把用户输入追加到上下文
    messages.append({"role": "user", "content": user_input})

    # ============================================================
    # 核心概念 3：请求体三要素
    # ------------------------------------------------------------
    # model:    指定模型版本（如 gpt-4o-mini / moonshot-v1-8k / deepseek-chat）
    # messages: 完整对话历史（含 system prompt）
    # stream:   false = 等模型全部生成完再返回（非流式）
    #           true  = 模型生成一个字就吐一个字（流式，SSE 协议）
    # temperature: 随机性采样参数，0~2。越低越确定，越高越有创意。
    # ============================================================
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": STREAM,
        "temperature": 0.7,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }

    # OpenAI 兼容接口的统一端点
    url = f"{BASE_URL.rstrip('/')}/chat/completions"

    if STREAM:
        return _chat_stream(url, headers, payload)
    else:
        return _chat_non_stream(url, headers, payload)


def _chat_non_stream(url: str, headers: dict, payload: dict) -> dict:
    """
    非流式请求：发送一次 POST，等待服务器返回完整的 JSON，再解析。
    
    优点：代码简单，便于直接拿到 usage（token 消耗）。
    缺点：用户要等模型全部生成完才能看到第一个字，长文本时体验差。
    """
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()  # 如果 401/429/500 会抛异常
        data = resp.json()

    # 典型响应结构（非流式）：
    # {
    #   "id": "chatcmpl-xxx",
    #   "object": "chat.completion",
    #   "created": 1717000000,
    #   "model": "gpt-4o-mini",
    #   "choices": [
    #     {
    #       "index": 0,
    #       "message": {"role": "assistant", "content": "你好！..."},
    #       "finish_reason": "stop"
    #     }
    #   ],
    #   "usage": {
    #     "prompt_tokens": 56,
    #     "completion_tokens": 23,
    #     "total_tokens": 79
    #   }
    # }
    choice = data["choices"][0]
    assistant_msg = choice["message"]["content"]

    # 核心概念 4：Token 计费与长度约束
    # ------------------------------------------------------------
    # prompt_tokens:     输入文本被 Tokenizer 切分后的数量（含 system + 历史）
    # completion_tokens: 模型生成的 token 数量
    # total_tokens:      两者之和
    # 
    # 计费公式 ≈ 输入单价 × prompt_tokens + 输出单价 × completion_tokens
    # 不同模型的单价差异巨大（gpt-4o-mini 比 gpt-4o 便宜 10~20 倍）。
    # 上下文越长，prompt_tokens 越大，费用线性增长。
    # ============================================================
    usage = data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", 0)

    # 把助手回复加入上下文，供下一轮使用
    messages.append({"role": "assistant", "content": assistant_msg})

    return {
        "reply": assistant_msg,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _chat_stream(url: str, headers: dict, payload: dict) -> dict:
    """
    流式请求：使用 SSE（Server-Sent Events）协议，逐块读取响应。
    
    优点：模型生成第一个 token 后立刻推给用户，体验接近"实时打字"。
    缺点：需要客户端逐行解析 data: {...} 片段，且 usage 不一定在流里返回。
    
    SSE 数据格式示例：
        data: {"id":"...","choices":[{"delta":{"content":"你"}}]}
        data: {"id":"...","choices":[{"delta":{"content":"好"}}]}
        data: [DONE]
    """
    reply_parts = []
    usage = {}

    with httpx.Client(timeout=60.0) as client:
        # stream="text" 让 httpx 以文本方式逐行 yield，便于解析 SSE
        with client.stream("POST", url, headers=headers, json=payload) as resp:
            resp.raise_for_status()
            print("🤖 ", end="", flush=True)

            for line in resp.iter_lines():
                line = line.strip()
                if not line or line.startswith(":"):
                    # SSE 规范：空行和 : 开头的是心跳/注释，忽略
                    continue
                if line == "data: [DONE]":
                    break

                if line.startswith("data: "):
                    json_str = line[6:]
                    try:
                        chunk = json.loads(json_str)
                    except json.JSONDecodeError:
                        continue

                    choice = chunk["choices"][0]
                    delta = choice.get("delta", {})

                    if "content" in delta and delta["content"]:
                        text = delta["content"]
                        print(text, end="", flush=True)
                        reply_parts.append(text)

                    # 部分服务商在最后一个 chunk 附带 usage（如 OpenAI 的新接口）
                    if "usage" in chunk and chunk["usage"]:
                        usage = chunk["usage"]

    print()  # 换行

    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", 0)

    full_reply = "".join(reply_parts)
    messages.append({"role": "assistant", "content": full_reply})

    return {
        "reply": full_reply,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def main():
    if not API_KEY:
        print("❌ 错误：未设置 LLM_API_KEY 环境变量")
        print()
        print("👉 快速配置指南（选一个即可）：")
        print()
        print("1️⃣  OpenAI")
        print("    export LLM_API_KEY='sk-xxxxxxxx'")
        print("    export LLM_MODEL='gpt-4o-mini'")
        print()
        print("2️⃣  Moonshot (Kimi) —— 国内友好，有免费额度")
        print("    export LLM_API_KEY='sk-xxxxxxxx'")
        print("    export LLM_BASE_URL='https://api.moonshot.cn/v1'")
        print("    export LLM_MODEL='moonshot-v1-8k'")
        print()
        print("3️⃣  DeepSeek —— 性价比高")
        print("    export LLM_API_KEY='sk-xxxxxxxx'")
        print("    export LLM_BASE_URL='https://api.deepseek.com/v1'")
        print("    export LLM_MODEL='deepseek-chat'")
        print()
        print("💡 提示：把 export 语句写入 ~/.zshrc，然后执行 source ~/.zshrc，可永久生效。")
        sys.exit(1)

    global STREAM

    print("=" * 60)
    print("🚀 Agent 开发第一课：最小 LLM 调用")
    print(f"   模型    : {MODEL}")
    print(f"   接口    : {BASE_URL}")
    print(f"   模式    : {'流式 (Stream)' if STREAM else '非流式 (Blocking)'}")
    print("=" * 60)
    print("输入 'quit' 或 'exit' 退出")
    print("输入 'reset' 清空对话上下文")
    print("输入 'stream' 切换 流式/非流式")
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
        if user_input.lower() == "reset":
            messages.clear()
            messages.append(SYSTEM_PROMPT)
            print("🔄 上下文已清空（system prompt 保留）")
            continue
        if user_input.lower() == "stream":
            STREAM = not STREAM
            mode_name = "流式" if STREAM else "非流式"
            print(f"🔀 已切换为 {mode_name}模式")
            continue

        result = chat_once(user_input)

        if not STREAM:
            print(f"🤖 Bot: {result['reply']}")

        # 打印 token 消耗与上下文规模
        context_pairs = (len(messages) - 1) // 2  # 减去 system，除以 2（user + assistant）
        print(
            f"📊 Token => 输入: {result['prompt_tokens']} | "
            f"输出: {result['completion_tokens']} | "
            f"总计: {result['total_tokens']} | "
            f"历史轮数: {context_pairs}"
        )


if __name__ == "__main__":
    main()
