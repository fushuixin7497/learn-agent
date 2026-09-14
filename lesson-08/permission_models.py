#!/usr/bin/env python3
"""
模块 8：Permission Models —— 安全-效率光谱上的四种权限模式
============================================================
承接模块 7 的"最小权限工具白名单"，本课回答下一个问题：

    确认每一次调用最安全，但用户体验最差；全自动最高效，但风险最大。
    能不能在"安全"和"效率"之间做一个可调节的光谱？

光谱从严格到宽松：

    per-call confirm → session allowlist → auto（两级分类器）→ YOLO

四种模式：

1. CONFIRM（逐次确认）
   每个副作用调用都问一次用户。最安全，效率最低。

2. ALLOWLIST（会话白名单）
   用户对一个工具说一次"yes"，该工具在**本次会话**内自动放行。
   会话结束授权即失效 —— 这就是它和永久白名单的区别。

3. AUTO（自动模式，两级分类器）
   - 第一级（fast path）：廉价规则。工作目录内的编辑 +
     已知安全工具 + 参数大小限制，直接放行，零延迟、零模型调用。
   - 第二级（slow path）：复杂的分类器（生产中通常是 LLM 判断），
     用 15 秒超时包裹。超时或异常时进入**可配置的失败策略**：
       FAIL_CLOSED（默认，安全优先）→ 拒绝
       FAIL_OPEN （效率优先）        → 放行

4. YOLO（全自动）
   全部放行。仅在隔离环境/完全可信任务中使用。

横切关注点：无论哪种模式，**每一个决定都写入审计日志**
（谁决定的、依据什么、结果如何），事后可回放、可追责。

运行：python permission_models.py
"""

import os
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


# ============================================================
# 权限模式与安全-效率光谱
# ============================================================
class Mode(Enum):
    """安全 → 效率光谱上的四个档位。"""
    CONFIRM = "confirm"      # 逐次确认：最安全，最打断
    ALLOWLIST = "allowlist"  # 会话白名单：确认一次，本会话放行
    AUTO = "auto"            # 自动模式：两级分类器决定
    YOLO = "yolo"            # 全自动：全部放行


class FailurePolicy(Enum):
    """AUTO 模式 slow path 失败（超时/异常）时的策略。"""
    FAIL_CLOSED = "fail_closed"  # 安全优先：拿不准就拒绝
    FAIL_OPEN = "fail_open"      # 效率优先：拿不准就放行进沙箱


class Decision(Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    ASK = "ASK"   # 需要问用户（CONFIRM 模式或分类器不确定时）


# ============================================================
# 审计日志：每一个决定都被记录
# ============================================================
@dataclass
class AuditEntry:
    tool: str
    arguments: Dict[str, Any]
    mode: Mode
    decision: Decision
    decided_by: str   # user / fast_path / slow_path / failure_policy / yolo
    reason: str
    elapsed_ms: float


@dataclass
class AuditLog:
    entries: List[AuditEntry] = field(default_factory=list)

    def record(self, entry: AuditEntry):
        self.entries.append(entry)

    def report(self) -> str:
        lines = [f"【审计日志】共 {len(self.entries)} 条决定"]
        for e in self.entries:
            lines.append(
                f"  [{e.decision.value:5s}] {e.tool:15s} "
                f"by={e.decided_by:15s} {e.elapsed_ms:7.1f}ms  {e.reason}"
            )
        return "\n".join(lines)


# ============================================================
# 工具与危险分类（沿用模块 7 的思想，这里做最小实现）
# ============================================================
SAFE_TOOLS = {"calculator", "read_file", "search_files"}
SIDE_EFFECT_TOOLS = {"write_file", "external_api"}
FORBIDDEN_TOOLS = {"execute_shell", "send_email", "delete_file"}


def is_path_inside(path: str, root: str) -> bool:
    abs_path = os.path.abspath(os.path.expanduser(path))
    abs_root = os.path.abspath(os.path.expanduser(root))
    return abs_path.startswith(abs_root + os.sep)


# ============================================================
# Slow path 分类器（可插拔）
# ============================================================
Classifier = Callable[[str, Dict[str, Any]], Decision]


def heuristic_classifier(tool: str, arguments: Dict[str, Any]) -> Decision:
    """
    教学用 slow path 分类器，模拟生产中"让 LLM 判断这次调用是否安全"。
    规则可以任意复杂；这里只做演示：工作目录外的写入一律转人工，其余 ALLOW。
    """
    if tool == "write_file":
        path = arguments.get("path", "")
        if not is_path_inside(path, WORKDIR):
            return Decision.ASK
    return Decision.ALLOW


def run_with_timeout(fn: Callable, timeout_s: float) -> Optional[Any]:
    """在 timeout_s 内运行 fn，超时返回 None。生产可用 concurrent.futures。"""
    box: Dict[str, Any] = {}

    def worker():
        try:
            box["result"] = fn()
        except Exception as exc:  # 分类器自身异常也视为 slow path 失败
            box["error"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        return None                      # 超时
    if "error" in box:
        raise box["error"]
    return box.get("result")


# ============================================================
# 权限门：四种模式的统一入口
# ============================================================
WORKDIR = os.path.expanduser("~/learn-agent-sandbox")
AUTO_TIMEOUT_S = 15.0        # slow path 默认超时：15 秒
FAST_PATH_MAX_ARGS_BYTES = 4096  # fast path 参数大小上限，防注入大 payload


class PermissionGate:
    """
    所有工具调用必须经过的门。request() 是唯一入口，
    返回 Decision；产生决定的同时必然写一条审计日志。
    """

    def __init__(
        self,
        mode: Mode,
        workdir: str = WORKDIR,
        failure_policy: FailurePolicy = FailurePolicy.FAIL_CLOSED,
        classifier: Classifier = heuristic_classifier,
        timeout_s: float = AUTO_TIMEOUT_S,
    ):
        self.mode = mode
        self.workdir = workdir
        self.failure_policy = failure_policy
        self.classifier = classifier
        self.timeout_s = timeout_s
        self.session_allowlist: set = set()   # ALLOWLIST 模式：本会话已授权工具
        self.audit = AuditLog()

    # ---------- 统一入口 ----------
    def request(self, tool: str, arguments: Dict[str, Any]) -> Decision:
        started = time.perf_counter()

        if tool in FORBIDDEN_TOOLS:
            return self._finish(started, tool, arguments, Decision.DENY,
                                "catalog", "危险工具，任何模式下都禁止")

        if self.mode == Mode.YOLO:
            return self._finish(started, tool, arguments, Decision.ALLOW,
                                "yolo", "YOLO 模式，全部放行")

        if self.mode == Mode.CONFIRM:
            if tool in SAFE_TOOLS:
                return self._finish(started, tool, arguments, Decision.ALLOW,
                                    "catalog", "只读工具，自动放行")
            return self._finish(started, tool, arguments, Decision.ASK,
                                "confirm_mode", "副作用工具，逐次询问用户")

        if self.mode == Mode.ALLOWLIST:
            if tool in SAFE_TOOLS:
                return self._finish(started, tool, arguments, Decision.ALLOW,
                                    "catalog", "只读工具，自动放行")
            if tool in self.session_allowlist:
                return self._finish(started, tool, arguments, Decision.ALLOW,
                                    "session_allowlist", "已在会话白名单中")
            return self._finish(started, tool, arguments, Decision.ASK,
                                "allowlist_mode", "首次使用，询问用户是否加入会话白名单")

        # AUTO 模式：两级分类器
        return self._auto_decide(started, tool, arguments)

    # ---------- AUTO：fast path → slow path → 失败策略 ----------
    def _auto_decide(self, started, tool, arguments) -> Decision:
        # 第一级 fast path：廉价规则，零模型调用
        if tool in SAFE_TOOLS:
            return self._finish(started, tool, arguments, Decision.ALLOW,
                                "fast_path", "只读工具，规则直接放行")
        if (
            tool == "write_file"
            and is_path_inside(arguments.get("path", ""), self.workdir)
            and len(str(arguments.get("content", ""))) <= FAST_PATH_MAX_ARGS_BYTES
        ):
            return self._finish(started, tool, arguments, Decision.ALLOW,
                                "fast_path", "工作目录内的小写入，规则直接放行")

        # 第二级 slow path：复杂分类器 + 超时
        try:
            result = run_with_timeout(
                lambda: self.classifier(tool, arguments), self.timeout_s)
        except Exception as exc:
            result = None
            reason_suffix = f"分类器异常: {exc}"
        else:
            reason_suffix = f"分类器超时(>{self.timeout_s}s)"

        if result is None:
            # slow path 失败 → 可配置的失败策略
            if self.failure_policy == FailurePolicy.FAIL_CLOSED:
                return self._finish(started, tool, arguments, Decision.DENY,
                                    "failure_policy",
                                    f"{reason_suffix}，fail-closed 拒绝")
            return self._finish(started, tool, arguments, Decision.ALLOW,
                                "failure_policy",
                                f"{reason_suffix}，fail-open 放行")

        if result == Decision.ASK:
            return self._finish(started, tool, arguments, Decision.ASK,
                                "slow_path", "分类器不确定，转人工确认")
        return self._finish(started, tool, arguments, result,
                            "slow_path", f"分类器判定 {result.value}")

    # ---------- 用户响应 ----------
    def respond(self, tool: str, arguments: Dict[str, Any], approved: bool):
        """
        用户对 ASK 的响应。
        ALLOWLIST 模式下选择"允许"会把工具加入会话白名单（后续不再询问）。
        """
        started = time.perf_counter()
        if approved and self.mode == Mode.ALLOWLIST:
            self.session_allowlist.add(tool)
            return self._finish(started, tool, arguments, Decision.ALLOW,
                                "user", "用户批准，并加入会话白名单")
        if approved:
            return self._finish(started, tool, arguments, Decision.ALLOW,
                                "user", "用户批准（仅本次）")
        return self._finish(started, tool, arguments, Decision.DENY,
                            "user", "用户拒绝")

    # ---------- 审计 ----------
    def _finish(self, started, tool, arguments, decision, decided_by, reason) -> Decision:
        self.audit.record(AuditEntry(
            tool=tool,
            arguments=dict(arguments),
            mode=self.mode,
            decision=decision,
            decided_by=decided_by,
            reason=reason,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        ))
        return decision


# ============================================================
# 演示：同一批调用，跑在光谱的不同位置上
# ============================================================
CALLS = [
    ("calculator", {"expr": "1024 * 4"}),
    ("read_file", {"path": "~/notes.txt"}),
    ("write_file", {"path": "~/learn-agent-sandbox/a.txt", "content": "hello"}),
    ("write_file", {"path": "/etc/hosts", "content": "..."}),
    ("execute_shell", {"cmd": "rm -rf /"}),
]


def demo_mode(mode: Mode, gate_kwargs: Optional[Dict] = None, approve_all: bool = True):
    gate = PermissionGate(mode, **(gate_kwargs or {}))
    print(f"\n{'='*64}\n模式: {mode.value.upper()}"
          + (f"  (policy={gate.failure_policy.value}, timeout={gate.timeout_s}s)"
             if mode == Mode.AUTO else "")
          + f"\n{'='*64}")
    for tool, args in CALLS:
        decision = gate.request(tool, args)
        line = f"  {tool:15s} {str(args)[:40]:42s} -> {decision.value}"
        if decision == Decision.ASK:
            # 模拟用户在终端输入 y/n
            line += f"  (用户输入: {'y' if approve_all else 'n'})"
            gate.respond(tool, args, approved=approve_all)
        print(line)
    print(gate.audit.report())


def demo_failure_policy():
    """slow path 超时（分类器被模拟成 2 秒才返回，超时限 0.5 秒），
    分别在 fail-closed 和 fail-open 策略下观察结果。"""
    slow_classifier: Classifier = lambda t, a: (time.sleep(2), Decision.ALLOW)[1]
    for policy in (FailurePolicy.FAIL_CLOSED, FailurePolicy.FAIL_OPEN):
        gate = PermissionGate(
            Mode.AUTO, classifier=slow_classifier,
            timeout_s=0.5, failure_policy=policy)
        decision = gate.request("external_api", {"url": "https://example.com"})
        print(f"  policy={policy.value:12s} -> {decision.value}"
              f"  ({gate.audit.entries[-1].reason})")


if __name__ == "__main__":
    print("同一批工具调用，依次放在安全-效率光谱的四个档位上：\n")

    demo_mode(Mode.CONFIRM)                       # 逐次确认
    demo_mode(Mode.ALLOWLIST)                     # 会话白名单（重复调用时见差异）
    demo_mode(Mode.AUTO)                          # 两级分类器
    demo_mode(Mode.YOLO)                          # 全自动

    print(f"\n{'='*64}\nAUTO 模式失败策略：slow path 超时后的行为\n{'='*64}")
    demo_failure_policy()
