#!/usr/bin/env python3
"""
模块 7：Least-Privilege Tooling —— 自动验证
=============================================
不依赖模型 API，直接验证权限控制机制是否生效。
"""

import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from permissions import (
    PermissionMode,
    PermissionContext,
    check_permission,
    confirm_tool,
    classify_tool,
    DangerLevel,
    build_permission_report,
    is_path_inside_sandbox,
)
from tools import execute_tool, calculator, read_file, write_file, SANDBOX_ROOT


# ============================================================
# 测试辅助
# ============================================================
def _make_temp_sandbox():
    """创建一个临时沙箱目录，避免污染真实 SANDBOX_ROOT。"""
    return tempfile.mkdtemp(prefix="lesson-07-test-")


def _assert(name: str, actual, expected):
    status = "✅" if actual == expected else "❌"
    print(f"   {status} {name}: {actual} (期望 {expected})")
    return actual == expected


def run_all_tests():
    all_pass = True

    print("🔒 模块 7 Least-Privilege Tooling —— 自动验证")
    print("=" * 60)

    # --------------------------------------------------------
    # 测试 1：工具分类
    # --------------------------------------------------------
    print("\n🧪 测试 1：工具危险等级分类")
    all_pass &= _assert("calculator 为 AUTO", classify_tool("calculator").danger, DangerLevel.AUTO)
    all_pass &= _assert("read_file 为 AUTO", classify_tool("read_file").danger, DangerLevel.AUTO)
    all_pass &= _assert("write_file 为 CONFIRM", classify_tool("write_file").danger, DangerLevel.CONFIRM)
    all_pass &= _assert("external_api 为 CONFIRM", classify_tool("external_api").danger, DangerLevel.CONFIRM)
    all_pass &= _assert("delete_file 为 FORBIDDEN", classify_tool("delete_file").danger, DangerLevel.FORBIDDEN)
    all_pass &= _assert("execute_shell 为 FORBIDDEN", classify_tool("execute_shell").danger, DangerLevel.FORBIDDEN)
    all_pass &= _assert("未知工具无权限", classify_tool("unknown_tool"), None)

    # --------------------------------------------------------
    # 测试 2：只读模式拦截写入
    # --------------------------------------------------------
    print("\n🧪 测试 2：只读模式拦截 write_file")
    ctx = PermissionContext(mode=PermissionMode.READ_ONLY)
    decision = check_permission(ctx, "write_file", {"filepath": "demo.txt", "content": "hi"})
    all_pass &= _assert("read_only 下 write_file 被拒绝", decision["allowed"], False)
    all_pass &= _assert("拦截原因包含只读模式", "只读模式" in decision["reason"], True)

    # 只读模式下 read_file 放行
    decision = check_permission(ctx, "read_file", {"filepath": "demo.txt"})
    all_pass &= _assert("read_only 下 read_file 被放行", decision["allowed"], True)

    # --------------------------------------------------------
    # 测试 3：只读模式参数级拦截
    # --------------------------------------------------------
    print("\n🧪 测试 3：只读模式参数级拦截")
    decision = check_permission(ctx, "write_file", {"filepath": "demo.txt", "content": "hi", "overwrite": True})
    all_pass &= _assert("read_only 下含 overwrite 被拒绝", decision["allowed"], False)

    # --------------------------------------------------------
    # 测试 4：确认模式下副作用需确认
    # --------------------------------------------------------
    print("\n🧪 测试 4：确认模式下副作用需确认")
    ctx = PermissionContext(mode=PermissionMode.CONFIRM)
    decision = check_permission(ctx, "write_file", {"filepath": "demo.txt", "content": "hi"})
    all_pass &= _assert("confirm 下 write_file 允许但需确认", decision["allowed"], True)
    all_pass &= _assert("confirm 下 write_file 需要确认", decision["needs_confirm"], True)

    decision = check_permission(ctx, "calculator", {"expression": "1+1"})
    all_pass &= _assert("confirm 下 calculator 自动放行", decision["allowed"], True)
    all_pass &= _assert("confirm 下 calculator 不需确认", decision["needs_confirm"], False)

    # --------------------------------------------------------
    # 测试 5：用户拒绝后工具不执行
    # --------------------------------------------------------
    print("\n🧪 测试 5：用户拒绝后工具不执行")
    ctx = PermissionContext(mode=PermissionMode.CONFIRM)
    approved = confirm_tool(ctx, "write_file", {"filepath": "demo.txt"}, user_approved=False)
    all_pass &= _assert("拒绝后返回 False", approved, False)
    all_pass &= _assert("拒绝后未进入已授权集合", "write_file" not in ctx.confirmed_tools, True)

    # --------------------------------------------------------
    # 测试 6：永远禁止的危险工具
    # --------------------------------------------------------
    print("\n🧪 测试 6：永远禁止的危险工具")
    for mode in (PermissionMode.READ_ONLY, PermissionMode.CONFIRM, PermissionMode.UNRESTRICTED):
        ctx = PermissionContext(mode=mode)
        for tool in ("delete_file", "execute_shell", "send_email"):
            decision = check_permission(ctx, tool, {"filepath": "x"} if tool == "delete_file" else {"command": "ls"} if tool == "execute_shell" else {"to": "a", "subject": "s", "body": "b"})
            all_pass &= _assert(f"{mode.value} 下 {tool} 被拒绝", decision["allowed"], False)

    # --------------------------------------------------------
    # 测试 7：execute_tool 端到端拦截
    # --------------------------------------------------------
    print("\n🧪 测试 7：execute_tool 端到端拦截")
    ctx = PermissionContext(mode=PermissionMode.READ_ONLY)
    result = execute_tool("write_file", {"filepath": "demo.txt", "content": "hello"}, ctx)
    all_pass &= _assert("read_only 下 execute_tool 拦截 write_file", result.startswith("❌ 权限拒绝"), True)

    result = execute_tool("delete_file", {"filepath": "demo.txt"}, ctx)
    all_pass &= _assert("read_only 下 execute_tool 拦截 delete_file", result.startswith("❌ 权限拒绝"), True)

    # --------------------------------------------------------
    # 测试 8：execute_tool 在 unrestricted 模式下放行
    # --------------------------------------------------------
    print("\n🧪 测试 8：unrestricted 模式下 write_file 可执行")
    ctx = PermissionContext(mode=PermissionMode.UNRESTRICTED)
    os.makedirs(SANDBOX_ROOT, exist_ok=True)
    test_file = os.path.join(SANDBOX_ROOT, "permission_test.txt")
    # 清理可能存在的旧文件，保证测试幂等
    if os.path.exists(test_file):
        os.remove(test_file)
    result = execute_tool("write_file", {"filepath": test_file, "content": "least privilege"}, ctx)
    all_pass &= _assert("unrestricted 下写入成功", result.startswith("成功："), True)
    all_pass &= _assert("写入后文件存在", os.path.exists(test_file), True)
    # 测试结束清理
    if os.path.exists(test_file):
        os.remove(test_file)

    # --------------------------------------------------------
    # 测试 9：沙箱路径隔离
    # --------------------------------------------------------
    print("\n🧪 测试 9：沙箱路径隔离")
    all_pass &= _assert("沙箱内路径合法", is_path_inside_sandbox(os.path.join(SANDBOX_ROOT, "a.txt"), SANDBOX_ROOT), True)
    all_pass &= _assert("沙箱外路径非法", is_path_inside_sandbox("/etc/passwd", SANDBOX_ROOT), False)
    all_pass &= _assert("目录遍历非法", is_path_inside_sandbox(os.path.join(SANDBOX_ROOT, "../etc/passwd"), SANDBOX_ROOT), False)

    # --------------------------------------------------------
    # 测试 10：权限报告
    # --------------------------------------------------------
    print("\n🧪 测试 10：权限报告生成")
    ctx = PermissionContext(mode=PermissionMode.CONFIRM)
    confirm_tool(ctx, "write_file", {"filepath": "x"}, user_approved=True)
    report = build_permission_report(ctx)
    all_pass &= _assert("报告包含模式", "confirm" in report, True)
    all_pass &= _assert("报告包含已授权工具", "write_file" in report, True)

    # --------------------------------------------------------
    # 汇总
    # --------------------------------------------------------
    print("\n📊 验证结果汇总")
    print("=" * 60)
    print(f"{'🎉 所有权限机制验证通过！' if all_pass else '⚠️ 存在验证失败，请检查实现。'}")
    return all_pass


if __name__ == "__main__":
    ok = run_all_tests()
    sys.exit(0 if ok else 1)
