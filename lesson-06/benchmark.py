#!/usr/bin/env python3
"""
模块 6：Graceful Degradation —— 自动验证
=========================================
在不依赖真实模型 API 的情况下，验证降级金字塔的每个层级：

1. 完全成功：工具正常执行，state 标记 success。
2. 部分成功：部分工具失败，state 保留已完成部分。
3. 安全兜底：模型连续乱答，触发 fallback 模板。
4. 优雅失败：所有路径都不可用，给出清晰状态说明。

运行方式：
    uv run python benchmark.py
"""

import os
import sys
from typing import Dict, Any

from degradation import (
    DegradationState,
    is_nonsense_reply,
    downgrade_messages,
    classify_tool_error,
    build_tool_unavailable_hint,
    generate_fallback_reply,
    generate_graceful_failure,
    maybe_degrade_tool_result,
)
from fallback_templates import (
    partial_success_template,
    service_unavailable_template,
    nonsense_reply_template,
    graceful_failure_template,
    status_line,
)
from tools import execute_tool, calculator


def test_response_quality() -> bool:
    """测试乱答/拒绝检测。"""
    print("\n" + "=" * 60)
    print("🧪 测试 1：响应质量检测")

    cases = [
        ("", True, "空字符串"),
        ("   ", True, "纯空白"),
        ("对不起，我无法回答这个问题。", True, "拒绝"),
        ("I'm sorry, I can't help with that.", True, "英文拒绝"),
        ("abc", True, "无意义短字符串"),
        ("计算结果是 8760。", False, "有效回答"),
        ("已为你写入文件 /Users/albert/notes.txt", False, "有效陈述"),
    ]

    all_pass = True
    for reply, expected, desc in cases:
        result = is_nonsense_reply(reply)
        status = "✅" if result == expected else "❌"
        print(f"   {status} {desc}: {result} (期望 {expected})")
        if result != expected:
            all_pass = False

    return all_pass


def test_prompt_downgrade() -> bool:
    """测试复杂 prompt 降级为简单 prompt。"""
    print("\n" + "=" * 60)
    print("🧪 测试 2：Prompt 降级")

    complex_prompt = (
        "你是一个聪明的 Agent，可以调用工具帮用户完成任务。\n"
        "重要规则：\n"
        "1. 用户提到的文件名必须原样使用。\n"
        "2. 如果工具返回错误，请修正参数后再次尝试。\n"
        "3. write_file 默认不会覆盖已有文件。\n"
    )

    messages = [
        {"role": "system", "content": complex_prompt},
        {"role": "user", "content": "计算 365 * 24"},
    ]

    degraded = downgrade_messages(messages)
    system_msg = degraded[0]["content"]

    has_suffix = "直接回答用户问题" in system_msg
    preserved_rules = "文件名必须原样使用" in system_msg

    print(f"   {'✅' if has_suffix else '❌'} 已追加简化约束")
    print(f"   {'✅' if preserved_rules else '❌'} 保留核心规则")

    return has_suffix and preserved_rules


def test_tool_error_classification() -> bool:
    """测试工具错误分类与换路径提示。"""
    print("\n" + "=" * 60)
    print("🧪 测试 3：工具不可用 → 换路径提示")

    errors = [
        ("错误：文件不存在 '/Users/albert/xxx.txt'", "not_found"),
        ("错误：429 Too Many Requests", "service_unavailable"),
        ("错误：limit 必须是 1~500 的整数", "generic"),
        ("错误：表达式包含非法字符", "invalid_expression"),
    ]

    all_pass = True
    for err, expected_type in errors:
        error_type, suggestion = classify_tool_error(err)
        ok = error_type == expected_type and suggestion
        print(f"   {'✅' if ok else '❌'} {err[:40]}... → {error_type}")
        if not ok:
            all_pass = False

    hint = build_tool_unavailable_hint("external_api", "错误：外部 API 503")
    has_suggestion = "请换一条路径" in hint
    print(f"   {'✅' if has_suggestion else '❌'} 换路径提示包含行动指令")

    return all_pass and has_suggestion


def test_partial_success_state() -> bool:
    """测试多步任务中保留已完成部分。"""
    print("\n" + "=" * 60)
    print("🧪 测试 4：多步任务保留已完成部分")

    state = DegradationState(user_input="计算并写入结果")
    state.record_tool_success("calculator", {"expression": "365*24"}, "计算结果：365*24 = 8760")
    state.record_tool_failure("write_file", {"filepath": "/root/test.txt"}, "错误：路径超出允许范围")

    report = state.to_user_report()
    has_completed = "已完成步骤" in report and "365*24 = 8760" in report
    has_failed = "失败步骤" in report and "路径超出允许范围" in report
    has_partial = "部分成功" in report or "运行状态】" in report

    print(f"   {'✅' if has_completed else '❌'} 报告包含已完成步骤")
    print(f"   {'✅' if has_failed else '❌'} 报告包含失败步骤")
    print(f"   {'✅' if has_partial else '❌'} 报告包含状态标签")

    return has_completed and has_failed and has_partial


def test_forced_degradation_simulation() -> bool:
    """测试强制降级模式：真实成功结果会被替换为不可用。"""
    print("\n" + "=" * 60)
    print("🧪 测试 5：强制降级模拟")

    os.environ["DEGRADE_MODE"] = "forced"
    from degradation import should_force_degradation

    real = calculator("1+1")
    degraded = maybe_degrade_tool_result("calculator", real)

    is_degraded = "不可用" in degraded and "模拟强制降级" in degraded
    print(f"   {'✅' if is_degraded else '❌'} forced 模式下真实结果被替换")
    print(f"      原结果: {real}")
    print(f"      降级后: {degraded[:80]}...")

    os.environ["DEGRADE_MODE"] = "on"
    return is_degraded


def test_fallback_templates() -> bool:
    """测试安全兜底模板生成清晰状态说明。"""
    print("\n" + "=" * 60)
    print("🧪 测试 6：安全兜底与优雅失败模板")

    state = DegradationState(user_input="用 external_api 查天气")
    state.record_tool_failure("external_api", {"query": "天气"}, "错误：外部 API 503")
    fallback = generate_fallback_reply(state)

    has_status = "安全兜底" in fallback
    has_failed_tool = "external_api" in fallback
    no_stack = "Traceback" not in fallback and "Exception" not in fallback

    print(f"   {'✅' if has_status else '❌'} fallback 回复包含状态说明")
    print(f"   {'✅' if has_failed_tool else '❌'} fallback 回复包含失败工具")
    print(f"   {'✅' if no_stack else '❌'} fallback 回复无 stack trace")

    failure = generate_graceful_failure(state, "所有工具均不可用")
    has_reason = "所有工具均不可用" in failure
    has_advice = "检查" in failure or "重试" in failure

    print(f"   {'✅' if has_reason else '❌'} 优雅失败包含失败原因")
    print(f"   {'✅' if has_advice else '❌'} 优雅失败包含下一步建议")

    return has_status and has_failed_tool and no_stack and has_reason and has_advice


def test_template_functions() -> bool:
    """测试独立模板函数。"""
    print("\n" + "=" * 60)
    print("🧪 测试 7：独立兜底模板函数")

    partial = partial_success_template(
        user_input="读取并总结文件",
        completed=[{"tool": "read_file", "result_preview": "文件内容摘要..."}],
        failed=[{"tool": "summary", "error": "模型不可用"}],
    )

    unavailable = service_unavailable_template("external_api", "search_files")
    nonsense = nonsense_reply_template("复杂问题", 2)
    graceful = graceful_failure_template("写报告", "网络断开")

    checks = [
        ("partial_success_template", "部分成功" in partial and "已完成的步骤" in partial),
        ("service_unavailable_template", "服务不可用" in unavailable and "search_files" in unavailable),
        ("nonsense_reply_template", "模型响应异常" in nonsense),
        ("graceful_failure_template", "无法完成" in graceful and "网络断开" in graceful),
        ("status_line", "🔄 Step 3 [retry]" == status_line(3, "retry")),
    ]

    all_pass = True
    for name, ok in checks:
        print(f"   {'✅' if ok else '❌'} {name}")
        if not ok:
            all_pass = False

    return all_pass


def main():
    print("=" * 60)
    print("🔽 模块 6 Graceful Degradation —— 自动验证")
    print("=" * 60)

    tests = [
        ("响应质量检测", test_response_quality),
        ("Prompt 降级", test_prompt_downgrade),
        ("工具错误分类", test_tool_error_classification),
        ("保留已完成部分", test_partial_success_state),
        ("强制降级模拟", test_forced_degradation_simulation),
        ("安全兜底模板", test_fallback_templates),
        ("独立模板函数", test_template_functions),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            ok = test_func()
            if ok:
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"   ❌ 测试异常: {type(e).__name__}: {e}")
            failed += 1

    print("\n" + "=" * 60)
    print("📊 验证结果汇总")
    print("=" * 60)
    print(f"通过: {passed}")
    print(f"失败: {failed}")
    print(f"总计: {len(tests)}")

    if failed == 0:
        print("\n🎉 所有 Graceful Degradation 机制验证通过！")
        return 0
    else:
        print(f"\n⚠️ {failed} 个测试未通过，请检查实现。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
