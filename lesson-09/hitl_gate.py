#!/usr/bin/env python3
"""
模块 9：Human-in-the-Loop Gates —— 人为参与的门
================================================
模块 8 解决了"谁来决定能不能做"（权限光谱）。本课解决下一个问题：

    用户不在电脑前时，Agent 遇到了不可逆操作，该怎么办？

真实场景：Agent 凌晨 2 点跑批处理，中途要删除一个文件、发一封邮件。
发送前它必须得到人类批准 —— 但人正在睡觉。两种坏做法：

- 停下来干等（阻塞整个进程，一晚白跑）
- 跳过批准继续干（违背 Human-in-the-Loop 原则）

正确做法 —— **确认门（Confirmation Gate）**：

1. Agent 识别出"这是不可逆操作"，在门前**暂停（suspend）**。
2. 把门的**悬挂状态**完整落盘：完整对话上下文 + 执行轨迹 + 待批准
   的工具调用 + 暂停原因。进程直接退出，不占资源。
3. 用户回来后执行 `resume`，从磁盘恢复现场，在门处做出决定
   （批准 → 执行；拒绝 → 把拒绝反馈给模型，让它换条路走完任务）。
4. 恢复后的 Agent 接着暂停点继续，**不丢任何上下文**。

本课核心代码只有一个文件：hitl_gate.py（纯标准库）。

运行：
    python3 hitl_gate.py run            # 启动任务，将在删除操作前暂停落盘
    python3 hitl_gate.py resume <id>    # 恢复悬挂的门，做出人审决定后继续
"""

import json
import os
import sys
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional


# ============================================================
# 工具与可逆性分类
# ============================================================
# 关键设计：判断"要不要在门前暂停"的依据不是"危不危险"，
# 而是"可不可逆"——危险操作（如 execute_shell）直接永久禁止；
# 不可逆但合理的操作（删除、外发）才进入确认门。
AUTO_TOOLS = {"read_file"}                       # 只读，自动执行
IRREVERSIBLE_TOOLS = {"delete_file", "send_email"}  # 不可逆 → 确认门


def tool_read_file(args: Dict[str, Any]) -> str:
    return f"(已读取 {args['path']}) 内容：这是一份过期的临时报告"


def tool_delete_file(args: Dict[str, Any]) -> str:
    return f"(已删除 {args['path']})"


TOOLS = {
    "read_file": tool_read_file,
    "delete_file": tool_delete_file,
}


# ============================================================
# 悬挂状态：门的全部现场，可序列化
# ============================================================
SUSPEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           ".lesson09_suspensions")


@dataclass
class SuspendedState:
    """
    Agent 在确认门处暂停那一刻的完整快照。

    恢复任务只需要这一个对象 —— 它同时携带：
    - messages   : 完整对话上下文（模型靠它知道之前发生了什么）
    - trace      : 执行轨迹（人在批准前想看看 Agent 走到哪一步、为什么）
    - pending_*  : 卡在门上的那个工具调用（批准后要执行的就是它）
    """
    session_id: str
    suspended_at: str
    reason: str                 # 为什么暂停（哪条规则触发的门）
    pending_tool: str
    pending_args: Dict[str, Any]
    messages: List[Dict[str, str]]
    trace: List[Dict[str, Any]]

    def save(self) -> str:
        os.makedirs(SUSPEND_DIR, exist_ok=True)
        path = os.path.join(SUSPEND_DIR, f"{self.session_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        return path

    @staticmethod
    def load(session_id: str) -> "SuspendedState":
        path = os.path.join(SUSPEND_DIR, f"{session_id}.json")
        with open(path, encoding="utf-8") as f:
            return SuspendedState(**json.load(f))

    @staticmethod
    def list_saved() -> List[str]:
        if not os.path.isdir(SUSPEND_DIR):
            return []
        return sorted(f[:-5] for f in os.listdir(SUSPEND_DIR)
                      if f.endswith(".json"))


# ============================================================
# 教学用"模型"：根据对话历史决定下一步
# ============================================================
def fake_model(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    假模型，离线可跑。它的决策完全取决于 messages ——
    这正好演示了为什么"完整保存上下文"是恢复的前提：
    恢复后模型从同一套 messages 出发，会做出和暂停前一致的决定。
    """
    tool_results = "\n".join(m["content"] for m in messages
                             if m["role"] == "tool")
    # messages[0] 是最初的任务指令，之后的 user 消息都是门处的人工反馈
    user_feedback = "\n".join(m["content"] for m in messages[1:]
                              if m["role"] == "user")
    if "拒绝" in user_feedback:
        return {"type": "final",
                "content": "报告已读取（内容为过期临时文件），但删除被用户拒绝，"
                           "文件保留，任务以安全方式收尾。"}
    if "read_file" not in tool_results:
        return {"type": "tool_call", "tool": "read_file",
                "args": {"path": "~/tmp/old_report.txt"}}
    if "delete_file" not in tool_results:
        return {"type": "tool_call", "tool": "delete_file",
                "args": {"path": "~/tmp/old_report.txt"}}
    # 走到这里说明 delete_file 已有工具结果 —— 即用户批准且已执行
    return {"type": "final",
            "content": "任务完成：已确认报告内容并删除 old_report.txt。"}


# ============================================================
# Agent 主循环：唯一的暂停点在确认门
# ============================================================
def run_loop(messages: List[Dict[str, str]],
             trace: List[Dict[str, Any]],
             pending: Optional[SuspendedState] = None) -> None:
    """
    从当前状态继续跑，直到拿到最终回答。

    pending 不为 None 表示"刚从磁盘恢复"：跳过模型决策，
    先把用户在门处的决定落实到 messages，再继续循环。
    """
    if pending is not None:
        if pending.trace:  # 继承暂停前的轨迹
            trace = pending.trace + trace
        messages = pending.messages
        print(f"\n【恢复现场】session={pending.session_id}  "
              f"暂停于 {pending.suspended_at}")
        print(f"  待决定操作: {pending.pending_tool} {pending.pending_args}")
        print(f"  轨迹事件数: {len(trace)}  对话轮数: {len(messages)}")
        answer = input(f"\n  允许执行 {pending.pending_tool}? (y/n) ")
        approved = answer.strip().lower() in ("y", "yes", "是")
        trace.append({"event": "gate_resume", "approved": approved,
                      "at": _now()})
        if approved:
            result = TOOLS[pending.pending_tool](pending.pending_args)
            messages.append({"role": "tool",
                             "content": f"[{pending.pending_tool}] {result}"})
            print(f"  → 已执行: {result}")
        else:
            messages.append({"role": "user",
                             "content": f"用户拒绝了 {pending.pending_tool}"
                                        "，请不删除文件，以安全方式收尾。"})
            print("  → 已拒绝，拒绝原因将反馈给模型")

    while True:
        action = fake_model(messages)
        if action["type"] == "final":
            trace.append({"event": "final", "at": _now()})
            print(f"\n【最终回答】{action['content']}")
            break

        tool, args = action["tool"], action["args"]
        if tool in IRREVERSIBLE_TOOLS:
            # ── 确认门：暂停、落盘、退出 ──
            state = SuspendedState(
                session_id=uuid.uuid4().hex[:8],
                suspended_at=_now(),
                reason=f"不可逆操作 '{tool}' 需要人工批准",
                pending_tool=tool,
                pending_args=args,
                messages=messages,
                trace=trace + [{"event": "gate_suspend", "tool": tool,
                                "at": _now()}],
            )
            path = state.save()
            print(f"\n【确认门】{state.reason}")
            print(f"  操作: {tool} {args}")
            print(f"  完整状态已保存: {path}")
            print(f"  进程退出。回来后执行：")
            print(f"    python3 {os.path.basename(__file__)} resume "
                  f"{state.session_id}")
            return

        # 可逆/只读操作：直接执行
        result = TOOLS[tool](args)
        messages.append({"role": "assistant",
                         "content": f"调用 {tool} {args}"})
        # 工具结果带上工具名前缀，模型据此判断"哪一步已完成"
        messages.append({"role": "tool", "content": f"[{tool}] {result}"})
        trace.append({"event": "tool_call", "tool": tool, "args": args,
                      "at": _now()})
        print(f"  执行 {tool:12s} -> {result}")

    _print_trace(trace)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _print_trace(trace: List[Dict[str, Any]]) -> None:
    print("\n【执行轨迹】")
    for i, e in enumerate(trace, 1):
        detail = e.get("tool") or (f"approved={e['approved']}"
                                   if "approved" in e else "")
        print(f"  {i}. {e['event']:14s} {detail}")


# ============================================================
# 入口
# ============================================================
def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "run":
        print("任务：检查并清理 ~/tmp/old_report.txt")
        run_loop(messages=[{"role": "user",
                            "content": "检查并清理 ~/tmp/old_report.txt"}],
                 trace=[])
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "resume":
        saved = SuspendedState.list_saved()
        if not saved:
            print("没有已保存的悬挂状态。先运行: python3 hitl_gate.py run")
            return
        session_id = sys.argv[2] if len(sys.argv) >= 3 else saved[-1]
        if session_id not in saved:
            print(f"找不到 {session_id}。已保存: {', '.join(saved)}")
            return
        run_loop(messages=[], trace=[], pending=SuspendedState.load(session_id))
        return

    print(__doc__)


if __name__ == "__main__":
    main()
