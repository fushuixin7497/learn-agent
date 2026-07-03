# 🤖 Agent 开发第二课：手写 Agent Loop

## 一、这个任务在干什么？

第一课你学会了**发一次请求、拿一次回答**。  
但真正的 Agent 不是"一问一答"，而是**"循环"**：

```
用户提问
    ↓
调模型（带上 tools 列表）
    ↓
模型决定：直接回答？还是调用工具？
    ↓
如果调用工具 → 执行 Python 函数 → 结果回填 messages → 再调模型
    ↓
模型再决定：直接回答？还是继续调用工具？
    ↓
... 重复直到模型说 "我搞定了"（finish_reason="stop"）
    ↓
输出最终答案
```

这就是 **Agent Loop**，它是所有 Agent 框架（LangChain、AutoGPT、OpenAI Assistants 等）的底层核心。

---

## 二、核心概念

### 1. Agent 的本质是状态机

状态只有两个：
- **STATE_CALL_MODEL**：把当前上下文发给模型
- **STATE_EXECUTE_TOOLS**：解析模型的 tool_calls，执行函数，把结果塞回上下文

循环终止条件：`finish_reason == "stop"`

### 2. Tool Calling 协议

你要告诉模型：**"我有这些工具，你想用哪个就告诉我"**。

请求时传入 `tools` 数组：

```json
{
  "type": "function",
  "function": {
    "name": "calculator",
    "description": "执行数学计算",
    "parameters": {
      "type": "object",
      "properties": {
        "expression": {"type": "string", "description": "数学表达式"}
      },
      "required": ["expression"]
    }
  }
}
```

模型返回 `tool_calls`：

```json
{
  "role": "assistant",
  "content": null,
  "tool_calls": [
    {
      "id": "call_xxx",
      "type": "function",
      "function": {
        "name": "calculator",
        "arguments": "{\"expression\": \"365 * 24\"}"
      }
    }
  ]
}
```

你执行完函数后，必须按这个格式回填：

```json
{
  "role": "tool",
  "tool_call_id": "call_xxx",
  "name": "calculator",
  "content": "8760"
}
```

**为什么一定要回填？** 因为模型在下一次请求时，需要通过 `tool_call_id` 把"工具调用"和"工具结果"一一对应起来，才能继续推理。

### 3. 工具注册表（Tool Registry）

Python 端用一个字典做映射：

```python
TOOL_REGISTRY = {
    "calculator": calculator,
    "read_file": read_file,
    "run_command": run_command,
}
```

模型只认识字符串 `"calculator"`，Python 靠这个字典找到真正的函数并执行。

---

## 三、文件结构

```
lesson-02/
├── agent.py      # 核心：Agent Loop + 3 个工具
├── pyproject.toml
└── README.md     # 本文件
```

---

## 四、运行

```bash
cd ~/learn-agent/lesson-02
export LLM_API_KEY="sk-xxxxx"
export LLM_BASE_URL="https://api.moonshot.cn/v1"   # 或 OpenAI / DeepSeek
export LLM_MODEL="moonshot-v1-8k"
uv run python agent.py
```

### 推荐测试用例

输入这些，观察 Agent 的每一步动作：

| 测试输入 | 预期行为 |
|---------|---------|
| `365 天有多少小时？` | 调用 `calculator`，计算 `365*24` |
| `帮我看看 ~/learn-agent/lesson-01/chat.py 的前 10 行` | 先 `read_file` 读取文件，再总结内容 |
| `我的 Downloads 文件夹占多大空间？` | 调用 `run_command` 执行 `du -sh ~/Downloads`，再回答 |
| `先算 1024 * 768，再告诉我结果加上 100 是多少` | 多步：两次 `calculator` 调用 |

---

## 五、观察重点

1. **每一步循环都有打印**：你能清晰看到模型在哪一步决定调用什么工具、传了什么参数、得到了什么结果。
2. **`finish_reason` 是关键**：
   - `tool_calls` → 继续循环
   - `stop` → 任务完成，终止循环
3. **messages 数组会越滚越长**：里面包含了 system、user、assistant（带 tool_calls）、tool（结果）... 模型靠这个完整的"记忆"来做下一步决策。

---

## 六、常见问题

**Q: 模型不调用工具，直接乱答怎么办？**  
A: 降低 `temperature`（脚本里已设为 0.3），或在 system prompt 里更明确地告诉模型"优先使用工具"。

**Q: 模型调用工具时参数不对（比如 JSON 解析失败）？**  
A: 这是常见情况。生产环境中需要加参数校验和错误处理（脚本里已包含 try/except）。

**Q: 模型一次调用多个工具怎么办？**  
A: `tool_calls` 是一个数组，脚本里已用 `for tc in tool_calls` 支持并发执行多个工具。

**Q: 工具执行耗时很长（比如跑一个训练任务）怎么办？**  
A: 生产环境中，你应该在 STATE_EXECUTE_TOOLS 时把工具放到后台线程/队列执行，异步拿到结果后再继续循环。本课为了教学清晰，用的是同步执行。
