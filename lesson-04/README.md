# 🧠 模块 3：Context 管理 —— 长任务下仍稳定的 Agent

## 一、这个任务在干什么？

模块 2 实现了健壮的工具，但当任务变长、工具输出变大时，messages 会迅速撑满模型的 context 窗口，导致：

- API 直接报错（context length exceeded）
- 模型遗忘早期指令或对话
- Token 成本飙升

本课目标是：**在信息损失和成本之间做权衡**，让 Agent 在长任务下仍然稳定运行。

---

## 二、三种 Context 管理策略

| 策略 | 做法 | 优点 | 缺点 | 适用场景 |
|---|---|---|---|---|
| **截断（truncate）** | 丢弃最老的消息 | 实现简单、零额外成本 | 信息永久丢失 | 对早期信息不敏感的任务 |
| **摘要（summarize）** | 用模型把早期对话压缩成摘要 | 保留关键信息 | 需要额外 API 调用 | 需要保留上下文的连续对话 |
| **外置记忆（memory）** | 把早期对话写入文件 | messages 最精简，完整信息可回放 | 需要时再读文件 | 长文档分析、审计场景 |

---

## 三、文件结构

```
lesson-04/
├── context_manager.py   # Context 管理核心（本课核心产出物）
├── agent.py             # 接入 ContextManager 的 Agent Loop
├── tools.py             # 复用 lesson-03 工具 + generate_long_text
├── benchmark.py         # 三种策略对比测试
├── pyproject.toml
├── .python-version
└── README.md
```

---

## 四、运行

### 1. 交互式 Agent

```bash
cd ~/learn-agent/lesson-04
export LLM_API_KEY="sk-xxxxx"
export LLM_BASE_URL="https://api.moonshot.cn/v1"
export LLM_MODEL="moonshot-v1-8k"

# 默认截断策略
uv run python agent.py

# 切换策略
export CONTEXT_STRATEGY=summarize
export CONTEXT_STRATEGY=memory
```

### 2. 策略对比基准测试

```bash
export LLM_API_KEY="sk-xxxxx"
uv run python benchmark.py
```

---

## 五、动手练习任务

### 练习 1：复现 context 爆掉

输入：

```
请生成 100 段超长文本
```

观察 `generate_long_text` 返回的数千字内容如何撑大 messages，并触发压缩。

### 练习 2：对比三种策略

分别用 `truncate`、`summarize`、`memory` 运行同一长任务：

```
请生成 80 段超长文本
```

然后连续追问：

```
第 10 段的主要内容是什么？
第 75 段的开头几个字是什么？
```

观察哪种策略能保留足够信息正确回答。

### 练习 3：调整阈值

修改 `MAX_CONTEXT_TOKENS` 环境变量（默认 2000），观察压缩触发时机：

```bash
export MAX_CONTEXT_TOKENS=500   # 更早触发压缩
export MAX_CONTEXT_TOKENS=4000  # 更晚触发压缩
```

### 练习 4：常驻 system prompt 与动态信息

在 `context_manager.py` 的三种策略中，system prompt 始终被保留。验证这一点：

1. 在 system prompt 里加入一条特殊规则，例如"每次回答前先说一句'收到'"。
2. 运行长任务触发压缩。
3. 观察模型是否仍然遵守这条规则 —— 这说明 system prompt 没有被误删。

---

## 六、核心代码解读

### Token 估算

```python
def estimate_tokens(text: str) -> int:
    cn_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other_count = len(text) - cn_count
    return cn_count + max(1, other_count // 4)
```

教学中用字符估算，避免依赖 tiktoken，同时足够直观。

### ContextManager 设计

```python
cm = ContextManager(max_tokens=2000, strategy="summarize")
cm.set_system_prompt("...")
cm.add({"role": "user", "content": "..."})
cm.fit()  # 超限则压缩
messages = cm.get_messages()
```

- `fit()` 在每次调用模型前执行。
- 只压缩动态消息，system prompt 永远保留。
- 压缩信息会打印到终端，方便观察。

---

## 七、常见问题

**Q: 为什么 token 估算和实际 API 返回的 prompt_tokens 不一致？**  
A: 这里用的是简化估算（中文字符按 1 token，其他按 4 字符/token），目的是教学演示。生产环境可用 tiktoken 或调用 API 的 usage 字段。

**Q: 摘要策略会调用额外模型吗？**  
A: 会。`_summarize()` 会把早期对话发给模型生成摘要，产生额外成本和延迟。这是信息保留与成本的权衡。

**Q: 外置记忆文件在哪里？**  
A: 默认写入 `~/.learn-agent/lesson-04-memory.md`，可通过 `memory_path` 参数修改。

**Q: 什么时候用截断、什么时候用摘要？**  
A: 简单任务/早期信息不重要 → 截断；需要保持上下文连贯 → 摘要；需要完整审计/回放 → 外置记忆。
