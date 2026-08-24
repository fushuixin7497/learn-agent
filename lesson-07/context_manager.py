#!/usr/bin/env python3
"""
模块 3：Context 管理核心
========================
处理长对话导致的 context 窗口爆掉问题。

核心策略：
1. 截断（truncate）：丢弃最老的消息，保留 system + 最近消息。
2. 摘要（summarize）：用模型把早期对话压缩成摘要。
3. 外置记忆（memory）：把早期对话写入文件，messages 中只保留引用。

设计原则：
- 常驻 system prompt 永远保留。
- 压缩只作用于动态信息（user / assistant / tool）。
- 提供 token 估算，方便观察窗口占用。
"""

import os
import json
import httpx
from typing import List, Dict, Any, Callable


# ============================================================
# Token 估算
# ============================================================
def estimate_tokens(text: str) -> int:
    """
    粗略估算 token 数：中文字符按 1 token，其他按 4 字符/token。
    教学用，不依赖 tiktoken，简单可控。
    """
    if not text:
        return 0
    cn_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other_count = len(text) - cn_count
    return cn_count + max(1, other_count // 4)


def estimate_message_tokens(message: Dict[str, Any]) -> int:
    """估算单条消息的 token 数。"""
    total = 0
    content = message.get("content") or ""
    total += estimate_tokens(content)

    # tool_calls / function 调用也有 arguments
    tool_calls = message.get("tool_calls", [])
    for tc in tool_calls:
        args = tc.get("function", {}).get("arguments", "")
        total += estimate_tokens(args)
    return total


def estimate_messages_tokens(messages: List[Dict[str, Any]]) -> int:
    """估算消息列表的总 token 数。"""
    return sum(estimate_message_tokens(m) for m in messages)


# ============================================================
# Context Manager
# ============================================================
class ContextManager:
    """
    管理对话历史，防止 context 窗口爆掉。

    用法：
        cm = ContextManager(max_tokens=2000, strategy="summarize", client=client)
        cm.set_system_prompt("你是一个聪明的 Agent...")
        cm.add({"role": "user", "content": "..."})
        cm.fit()  # 触发压缩策略
        messages = cm.get_messages()
    """

    def __init__(
        self,
        max_tokens: int = 2000,
        strategy: str = "truncate",
        client: httpx.Client = None,
        headers: Dict[str, str] = None,
        url: str = None,
        model: str = None,
        memory_path: str = None,
    ):
        if strategy not in ("truncate", "summarize", "memory"):
            raise ValueError(f"不支持的 strategy: {strategy}")

        self.max_tokens = max_tokens
        self.strategy = strategy
        self.client = client
        self.headers = headers or {}
        self.url = url
        self.model = model
        self.memory_path = memory_path or os.path.expanduser(
            "~/.learn-agent/lesson-04-memory.md"
        )

        self.messages: List[Dict[str, Any]] = []
        self.system_prompt: str = ""

        # 统计信息
        self.last_compression_info: Dict[str, Any] = {}

    def set_system_prompt(self, content: str):
        """设置常驻 system prompt。"""
        self.system_prompt = content
        # 如果 messages 里已有 system，替换它
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = content
        else:
            self.messages.insert(0, {"role": "system", "content": content})

    def add(self, message: Dict[str, Any]):
        """添加一条消息。"""
        self.messages.append(message)

    def get_messages(self) -> List[Dict[str, Any]]:
        """获取当前 messages。"""
        return self.messages

    def current_tokens(self) -> int:
        """当前 messages 估算 token 数。"""
        return estimate_messages_tokens(self.messages)

    def fit(self) -> Dict[str, Any]:
        """
        检查 token 是否超限，如果超限则执行压缩策略。
        返回压缩信息字典，供上层打印/记录。
        """
        current = self.current_tokens()
        self.last_compression_info = {
            "strategy": self.strategy,
            "before_tokens": current,
            "after_tokens": current,
            "compressed": False,
            "detail": "",
        }

        if current <= self.max_tokens:
            return self.last_compression_info

        # 需要压缩
        if self.strategy == "truncate":
            self._truncate()
        elif self.strategy == "summarize":
            self._summarize()
        elif self.strategy == "memory":
            self._dump_to_memory()

        self.last_compression_info["after_tokens"] = self.current_tokens()
        self.last_compression_info["compressed"] = True
        return self.last_compression_info

    # --------------------------------------------------------
    # 消息分组工具：保证 tool_calls / tool 消息成对
    # --------------------------------------------------------
    def _split_system_and_dynamic(self):
        """分离 system prompt 和动态消息。"""
        if self.messages and self.messages[0].get("role") == "system":
            return [self.messages[0]], self.messages[1:]
        return [], self.messages[:]

    def _build_units(self, dynamic: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """
        把动态消息分成不可拆分的单元。
        关键：assistant 带 tool_calls 时，必须和它后面的 tool 消息作为一个单元。
        """
        units = []
        i = 0
        while i < len(dynamic):
            msg = dynamic[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                group = [msg]
                j = i + 1
                while j < len(dynamic) and dynamic[j].get("role") == "tool":
                    group.append(dynamic[j])
                    j += 1
                units.append(group)
                i = j
            else:
                units.append([msg])
                i += 1
        return units

    def _units_to_messages(self, units: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        return [m for unit in units for m in unit]

    # --------------------------------------------------------
    # 策略 1：截断
    # --------------------------------------------------------
    def _truncate(self):
        """
        丢弃最老的消息单元，直到 token 数低于阈值。
        永远保留 system prompt（第一条）。
        如果只剩一个单元也超限，则对该单元做简单截断（通常不会出现）。
        """
        system, dynamic = self._split_system_and_dynamic()
        units = self._build_units(dynamic)

        # 从最早单元开始删除
        while units and estimate_messages_tokens(
            system + self._units_to_messages(units)
        ) > self.max_tokens:
            removed = units.pop(0)
            roles = ", ".join(m.get("role") for m in removed)
            self.last_compression_info["detail"] += f"[截断] {roles}\n"

        self.messages = system + self._units_to_messages(units)

    # --------------------------------------------------------
    # 策略 2：摘要
    # --------------------------------------------------------
    def _summarize(self):
        """
        把早期对话发给模型生成摘要，然后用摘要消息替换原始消息。
        保留 system + 最近若干完整单元 + 摘要。
        """
        system, dynamic = self._split_system_and_dynamic()
        units = self._build_units(dynamic)

        # 最近 2 个单元不摘要，避免切到 tool_calls 一半
        keep_recent = 1
        to_summarize = units[:-keep_recent] if len(units) > keep_recent else []
        recent = units[-keep_recent:] if len(units) > keep_recent else units[:]

        if not to_summarize:
            # 消息不多，直接截断兜底
            self._truncate()
            return

        summary_text = self._call_llm_for_summary(
            self._units_to_messages(to_summarize)
        )
        summary_message = {
            "role": "user",
            "content": (
                "以下是此前对话的摘要，请基于它继续回答后续问题：\n"
                f"{summary_text}"
            ),
        }

        self.messages = system + [summary_message] + self._units_to_messages(recent)
        self.last_compression_info["detail"] = (
            f"[摘要] 将 {len(to_summarize)} 个单元压缩为 1 条摘要"
        )

    def _call_llm_for_summary(self, messages: List[Dict[str, Any]]) -> str:
        """调用模型生成摘要。如果调用失败，回退为简单文本拼接。"""
        if not self.client or not self.url or not self.model:
            # 无模型可用，退化为拼接
            return self._fallback_summary(messages)

        prompt_messages = [
            {
                "role": "system",
                "content": (
                    "请用一段简短的中文总结以下对话中的关键事实和决策，"
                    "保留用户原始需求、已执行的关键步骤和结果。"
                    "不要添加没有依据的内容。"
                ),
            },
            {
                "role": "user",
                "content": "对话记录：\n" + json.dumps(messages, ensure_ascii=False, indent=2),
            },
        ]

        payload = {
            "model": self.model,
            "messages": prompt_messages,
            "temperature": 0.3,
        }

        try:
            resp = self.client.post(self.url, headers=self.headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"].get("content", "").strip()
        except Exception as e:
            print(f"⚠️ 摘要生成失败，回退为简单拼接: {e}")
            return self._fallback_summary(messages)

    def _fallback_summary(self, messages: List[Dict[str, Any]]) -> str:
        """摘要失败时的兜底：只保留 user 消息的前 200 字。"""
        parts = []
        for m in messages:
            if m.get("role") == "user":
                text = (m.get("content") or "")[:200]
                parts.append(f"用户曾问：{text}")
        return "\n".join(parts) or "（无可用摘要）"

    # --------------------------------------------------------
    # 策略 3：外置记忆
    # --------------------------------------------------------
    def _dump_to_memory(self):
        """
        把早期对话写入本地文件，messages 里只保留一条引用。
        保留 system + 最近若干完整单元 + 引用。
        
        关键改进：如果单元内有超长 tool 结果（>1000字），
        把 tool 内容外置到文件，只保留引用路径。
        """
        system, dynamic = self._split_system_and_dynamic()
        units = self._build_units(dynamic)

        keep_recent = 1
        to_store = units[:-keep_recent] if len(units) > keep_recent else []
        recent = units[-keep_recent:] if len(units) > keep_recent else units[:]

        if not to_store and not recent:
            self._truncate()
            return

        # 收集所有需要外置的消息（包括 recent 里的超长内容）
        all_messages_to_store = []
        
        # 处理 to_store：全部外置
        for unit in to_store:
            for m in unit:
                all_messages_to_store.append(m)
        
        # 处理 recent：只外置超长的 tool 结果，保留 assistant 和短消息
        recent_messages = []
        for unit in recent:
            new_unit = []
            for m in unit:
                content = m.get("content", "")
                if m.get("role") == "tool" and len(content) > 1000:
                    # 超长 tool 结果外置
                    tool_ref_path = f"{self.memory_path}.tool_{m.get('tool_call_id', 'unknown')}.txt"
                    try:
                        with open(tool_ref_path, "w", encoding="utf-8") as f:
                            f.write(content)
                        # 替换为引用消息
                        new_unit.append({
                            "role": "tool",
                            "tool_call_id": m.get("tool_call_id", ""),
                            "name": m.get("name", ""),
                            "content": f"[内容已外置到文件：{tool_ref_path}，如需完整内容请读取该文件]"
                        })
                        all_messages_to_store.append({
                            "role": "tool",
                            "tool_call_id": m.get("tool_call_id", ""),
                            "name": m.get("name", ""),
                            "content": content  # 完整内容存入记忆文件
                        })
                    except Exception as e:
                        print(f"⚠️ tool 结果外置失败: {e}")
                        new_unit.append(m)
                else:
                    new_unit.append(m)
            recent_messages.extend(new_unit)

        to_store_messages = all_messages_to_store

        os.makedirs(os.path.dirname(self.memory_path), exist_ok=True)
        try:
            with open(self.memory_path, "w", encoding="utf-8") as f:
                f.write("# Lesson-04 外置记忆\n\n")
                for i, m in enumerate(to_store_messages, 1):
                    f.write(f"## 消息 {i} [{m.get('role')}]\n\n")
                    f.write(json.dumps(m, ensure_ascii=False, indent=2))
                    f.write("\n\n")
        except Exception as e:
            print(f"⚠️ 外置记忆写入失败: {e}")
            self._truncate()
            return

        # 从外置内容中提取关键摘要（前 200 字），让模型至少知道之前发生了什么
        summary = ""
        for m in to_store_messages:
            content = m.get("content", "")
            if len(content) > 200:
                summary += content[:200] + "...\n"
            else:
                summary += content + "\n"
        summary = summary.strip()[:500]  # 摘要不超过 500 字

        memory_message = {
            "role": "user",
            "content": (
                "由于上下文过长，此前对话已写入外置记忆文件。"
                f"文件路径：{self.memory_path}\n"
                "重要：如果后续问题涉及之前对话中的任何内容（如文件内容、数据、编号等），"
                "你必须先调用 read_file 读取该文件，再基于文件内容作答。"
                "不要凭记忆回答，因为你当前看不到完整内容。\n\n"
                "读取技巧：记忆文件可能很长，建议先用 offset=1, limit=20 读取开头了解结构，"
                "再根据问题定位到具体段落。不要一次读取超过 100 行。\n\n"
                "此前对话摘要（不完整，仅供参考）：\n"
                f"{summary}"
            ),
        }

        self.messages = system + [memory_message] + recent_messages
        self.last_compression_info["detail"] = (
            f"[外置记忆] 将 {len(to_store_messages)} 条消息写入 {self.memory_path}"
        )
