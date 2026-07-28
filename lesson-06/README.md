# 🔽 模块 6：Graceful Degradation —— 失败时降级而非崩溃的 Agent

## 一、这个任务在干什么？

模块 4 解决了 Context 管理问题，模块 5 解决了多种错误防护问题。但真实运行中还会遇到另一种情况：

> **模型或工具没有彻底崩溃，但已经无法按原计划完成全部任务。**

例如：

- 模型开始乱答、拒绝、或输出无意义内容
- 某个外部工具 503 不可用，但任务还有其他路径可走
- 多步任务中前 3 步成功，第 4 步失败
- API 限流或网络抖动导致请求失败

本课目标是：**为 Agent 加入 Graceful Degradation（优雅降级）能力**，让失败不是 crash，而是：

```
完全成功 → 部分成功 → 安全兜底 → 优雅失败
```

---

## 二、降级策略金字塔

| 层级 | 触发条件 | Agent 行为 | 用户体验 |
|---|---|---|---|
| **完全成功** | 模型正常完成，工具调用成功 | 返回最终答案 | 和往常一样 |
| **部分成功** | 部分工具失败，但其他步骤已完成 | 保留已完成部分，说明失败步骤 | "已完成 A、B，但 C 因为 XX 失败" |
| **安全兜底** | 模型连续乱答/拒绝 | 切换到更简单 prompt 重试；仍失败则使用预设模板 | "模型暂时无法有效回答，当前状态如下..." |
| **优雅失败** | 所有路径都不可用 | 给出清晰状态说明 + 已保留结果 + 下一步建议 | "无法完成，原因是 XX，建议 YY" |

核心原则：**绝不暴露 stack trace，永远给用户可理解的状态说明。**

---

## 三、文件结构

```
lesson-06/
├── agent.py              # 主 Agent：带 DEGRADE_MODE 开关
├── degradation.py        # 降级策略核心（金字塔实现）
├── fallback_templates.py # 安全兜底模板集合
├── tools.py              # 工具 + external_api（模拟不可用）
├── context_manager.py    # 复用 lesson-04
├── benchmark.py          # 自动验证降级机制
├── pyproject.toml
├── .gitignore
└── README.md
```

---

## 四、运行

### 环境准备

```bash
cd ~/learn-agent/lesson-06
export LLM_API_KEY="sk-xxxxx"
export LLM_BASE_URL="https://api.moonshot.cn/v1"
export LLM_MODEL="moonshot-v1-8k"
```

### 1. 默认开启降级

```bash
uv run python agent.py
```

输入：

```
计算 365 * 24
```

观察：正常返回结果，状态为"完全成功"。

### 2. 模拟工具完全不可用，观察替代方案

```bash
export DEGRADE_MODE=forced
uv run python agent.py
```

输入：

```
用 external_api 查询天气，如果不可用就改用本地搜索
```

观察：

- 每个工具调用都会返回"服务不可用"
- Agent 没有 crash
- 提示模型"换一条路径"
- 最终返回"优雅失败"状态说明，包含失败原因和建议

### 3. 关闭降级对比

```bash
export DEGRADE_MODE=off
uv run python agent.py
```

输入同样的请求，观察 Agent 是否反复调用失败工具、没有换路径提示。

### 4. 自动验证（不依赖模型 API）

```bash
uv run python benchmark.py
```

输出 7 组测试结果：

```
🔽 模块 6 Graceful Degradation —— 自动验证
============================================================
🧪 测试 1：响应质量检测
   ✅ 空字符串: True (期望 True)
   ✅ 纯空白: True (期望 True)
   ...
📊 验证结果汇总
============================================================
通过: 7
失败: 0
总计: 7
🎉 所有 Graceful Degradation 机制验证通过！
```

---

## 五、动手练习任务

### 练习 1：观察降级模式开关

分别用 `DEGRADE_MODE=off` / `on` / `forced` 运行同一请求：

```
读取 ~/learn-agent/lesson-04/agent.py 的前 10 行
```

观察：

- `off`：失败时直接返回错误
- `on`：失败时提示模型换路径
- `forced`：所有工具都不可用，最终优雅失败

### 练习 2：模拟工具不可用，观察换路径

启用 `DEGRADE_MODE=on`，输入：

```
先用 external_api 查天气，如果失败就用 search_files 查找本地文件
```

由于 `external_api` 有 50% 概率返回 503，观察 Agent 是否在失败后改用 `search_files`。

### 练习 3：设计连续乱答时的安全兜底

在 `agent.py` 中，当模型连续乱答达到 `MAX_NONSENSE_RETRIES` 次时，会自动调用 `generate_fallback_reply()`。

你可以在 `fallback_templates.py` 中修改兜底回复风格，例如：

- 更正式的客服语气
- 更简短的技术说明
- 引导用户换种方式提问

### 练习 4：多步任务中保留已完成部分

输入一个需要多步的任务：

```
先计算 365*24，再写入 ~/tmp/calc.txt，最后读取确认
```

如果写入失败（例如路径不可用），Agent 会保留"计算结果"，并说明"写入失败"。

### 练习 5：扩展新的不可用场景

在 `degradation.py` 的 `classify_tool_error()` 中，添加你自己的错误分类：

```python
if "数据库连接失败" in result:
    return "db_error", "请检查数据库配置或改用本地文件。"
```

然后构造一个会返回这种错误的工具，观察 Agent 的提示是否正确。

---

## 六、核心代码解读

### 1. 降级模式开关

```python
DEGRADE_MODE = os.getenv("DEGRADE_MODE", "on").lower()
# off:  关闭降级
# on:   启用降级（默认）
# forced: 强制所有工具不可用，用于演示
```

### 2. 响应质量检测

```python
def is_nonsense_reply(reply: str) -> bool:
    if not reply or not reply.strip():
        return True
    if any(p in reply.lower() for p in REFUSAL_PATTERNS):
        return True
    # ... 更多规则
```

用于检测模型是否乱答、拒绝或输出无意义内容。

### 3. Prompt 降级

```python
def downgrade_messages(messages):
    # 在 system prompt 后追加更简单的约束
    simplified = original_prompt + SIMPLE_PROMPT_SUFFIX
```

当模型对复杂 prompt 乱答时，切换为更简单的 prompt 重试。

### 4. 工具不可用 → 换路径

```python
def build_tool_unavailable_hint(tool_name, error):
    return (
        f"工具 '{tool_name}' 调用失败：{error}\n"
        f"请换一条路径继续完成用户请求，"
        f"不要重复同样的失败调用。"
    )
```

把错误信息回填给模型，明确要求换路径。

### 5. 状态报告

```python
def to_user_report(self):
    return f"""
    【运行状态】{status}
    【用户请求】{user_input}
    ✅ 已完成步骤：...
    ⚠️ 失败步骤：...
    💡 说明：...
    """
```

所有失败都转化为用户可理解的状态说明。

---

## 七、常见问题

**Q: `DEGRADE_MODE=forced` 会真的破坏文件吗？**  
A: 不会。它只是在工具返回结果后，把成功结果替换为"模拟不可用"字符串，不会触发真实副作用。

**Q: 模型乱答检测准确吗？**  
A: 教学用的是关键词 + 长度启发式。生产环境可升级为用另一个 LLM 做质量判断，或结合 BLEU/ROUGE 等指标。

**Q: 为什么强制降级后还是可能调用工具？**  
A: `forced` 模式让每次工具调用都返回错误，但 Agent 仍可能尝试其他工具，这正是"换路径"的演示效果。

**Q: Graceful Degradation 和 Failure-First Design（lesson-05）有什么区别？**  
A: lesson-05 重点是"识别并拦截各种失败模式"；lesson-06 重点是"失败后如何保留价值并继续服务"，两者互补。
