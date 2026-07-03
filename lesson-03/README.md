# 🔧 模块 2：工具设计 —— 让模型用得对、用得稳

## 一、这个任务在干什么？

模块 1 你实现了 Agent Loop，模型能调用工具了。但**能调用 ≠ 调得对**：
- 模型可能传错参数类型（给 `offset` 传个字符串）
- 模型可能在不该调用时乱调用（把 `read_file` 当 `run_command` 用）
- 工具执行出错可能直接抛异常，把 Agent Loop 整崩溃
- 有副作用的工具（写文件）被重复调用，可能覆盖重要数据

本课目标是：**设计一套健壮的工具规范**，让模型调得准、错得起、重复调也不怕。

---

## 二、五大设计原则

### 原则 1：工具 description 本身就是 prompt

模型选工具时，**唯一依据**就是 `description` 字符串。它读的不是你的代码，而是这段 JSON：

```json
{"name": "read_file", "description": "读取本地文本文件内容..."}
```

| description 质量 | 模型行为 |
|-----------------|---------|
| ❌ 模糊： `"处理某个路径"` | 模型乱调用，什么都往里丢 |
| ✅ 清晰： `"读取本地文本文件。可指定起始行 offset 和读取行数 limit..."` | 模型准确判断使用场景 |

**好 description 的公式**：
> 功能定义 + 使用场景 + 参数示例 + 边界限制

---

### 原则 2：参数校验必须在工具内进行

**永远不要信任模型传来的参数。** 模型可能：
- 传错类型（`offset: "abc"` 而不是数字）
- 传越界值（`limit: 99999`）
- 传恶意路径（`filepath: "../../../etc/passwd"`）

每个工具的第一件事就是校验：

```python
def read_file(filepath: str, offset: int = 1, limit: int = 100):
    if not isinstance(filepath, str) or not filepath:
        return "错误：filepath 必须是非空字符串"
    if not isinstance(offset, int) or offset < 1:
        return "错误：offset 必须是 >=1 的整数"
    if not isinstance(limit, int) or limit < 1 or limit > 500:
        return "错误：limit 必须是 1~500 的整数"
```

---

### 原则 3：返回值要利于模型继续决策

工具的返回值不只是给人看的，**更是给模型看的下一步指令**。

| 差的返回值 | 好的返回值 |
|-----------|-----------|
| `2` | `计算结果：365 * 24 = 8760` |
| `文件不存在` | `错误：文件不存在 '/Users/albert/x.txt'。请检查路径是否正确，或使用 search_files 查找。` |
| `成功` | `成功（幂等）：文件已存在且内容完全一致，无需重复写入。` |

好的返回值包含：**状态 + 数据 + 下一步提示**，模型读到后就知道该继续还是该重试。

---

### 原则 4：错误作为正常返回值，避免 loop 崩溃

如果工具抛异常，Agent Loop 会直接中断，用户看不到任何结果。

正确做法：**所有错误在工具内捕获，转成字符串返回。**

```python
def execute_tool(name: str, arguments: dict) -> str:
    try:
        result = func(**arguments)
    except Exception as e:
        return f"工具执行异常: {type(e).__name__}: {e}"
    return result
```

这样即使模型传了完全错误的参数，Loop 也会继续：模型读到错误信息 → 修正参数 → 再次调用。

---

### 原则 5：有副作用工具要考虑幂等

**幂等（Idempotent）**：同样的输入，执行 N 次和执行 1 次的效果相同。

`write_file` 是最典型的有副作用工具。如果不做幂等：

```
Step 1: 写入 todo.md "买牛奶"
Step 2: 模型以为没写成功，再写一次 → 文件被覆盖/追加混乱
```

我们的幂等设计：

```python
if os.path.exists(path):
    if existing_content == new_content:
        return "成功（幂等）：文件已存在且内容一致，无需重复写入。"
    if not overwrite:
        return "错误：文件已存在且内容不同。如需覆盖，请设置 overwrite=true。"
```

模型拿到 `"成功（幂等）..."` 就知道：**任务已完成，不需要再写了。**

---

## 三、文件结构

```
lesson-03/
├── tools.py       # 工具定义 + 注册表 + Schema（核心产出物）
├── agent.py       # Agent Loop（复用模块1结构，接入健壮工具）
├── pyproject.toml
└── README.md      # 本文件
```

---

## 四、运行

```bash
cd ~/learn-agent/lesson-03
export LLM_API_KEY="sk-xxxxx"
export LLM_BASE_URL="https://api.moonshot.cn/v1"
export LLM_MODEL="moonshot-v1-8k"
uv run python agent.py
```

---

## 五、动手练习任务

### 练习 1：观察参数校验

输入让模型读取一个越界的文件：

```
帮我读取 /etc/passwd
```

观察输出：
- 模型尝试调用 `read_file`
- 工具返回 `"错误：路径 '/etc/passwd' 超出允许范围..."`
- 模型读取错误后，可能会尝试修正路径或告诉你无法读取

### 练习 2：测试幂等性

```
在 ~/learn-agent/test.txt 里写入 "hello world"
```

观察 Step 1：成功写入。  
再次输入同样的指令，观察 Step 1：返回 `"成功（幂等）：文件已存在且内容完全一致..."`。

然后修改指令：

```
在 ~/learn-agent/test.txt 里写入 "hello agent"
```

观察：返回 `"错误：文件已存在且内容不同。如需覆盖，请设置 overwrite=true。"`  
模型可能会再次调用 `write_file` 并设置 `overwrite=true`。

### 练习 3：观察模糊 description 的误用

编辑 `tools.py`，把顶部这行：

```python
ENABLE_MISLEADING_TOOL = False
```

改成：

```python
ENABLE_MISLEADING_TOOL = True
```

重新运行 `agent.py`，输入：

```
帮我读取 ~/learn-agent/lesson-01/chat.py 的内容
```

观察模型是不是**错误地调用了 `do_something`** 而不是 `read_file`。

因为 `do_something` 的 description 是 `"处理某个路径"`，模型无法区分它和 `read_file` 的用途，导致**工具误用**。

---

## 六、常见问题

**Q: 模型收到错误返回值后不会重试怎么办？**  
A: 检查 system prompt 是否明确告诉模型"如果工具返回错误，请仔细阅读并修正参数"。同时降低 temperature（0.3 以下）增加确定性。

**Q: 为什么限制 `limit` 最大 500 行？**  
A: 防止模型读取超大日志文件导致 prompt_tokens 爆炸。如果文件很大，模型应该分多次读取（offset 递增），而不是一次全读。

**Q: 怎么防止模型用 `write_file` 写恶意代码？**  
A: 本课做了路径限制（只能写主目录），但生产环境中还需要：内容安全扫描、禁止写入 `.bashrc` 等敏感文件、沙箱化执行环境。
