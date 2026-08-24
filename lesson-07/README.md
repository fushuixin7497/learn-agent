# 🔒 模块 7：Least-Privilege Tooling —— 最小权限 Agent

## 一、这个任务在干什么？

前几节课让 Agent 拥有了记忆、容错、降级等能力，但真实世界还有一个关键问题：

> **Agent 的工具能读写文件、访问网络、执行命令，一旦失控会造成真实损害。**

本课目标是：**为 Agent 引入最小权限原则（Least-Privilege）**，让 Agent 默认只能做最安全的事，危险操作必须显式授权。

核心思路：

```
默认只读 → 副作用需确认 → 危险操作永远禁止
```

---

## 二、核心概念

### 1. 工具白名单与上下文级权限控制

不是所有工具都能被调用。每个工具在 `PERMISSION_CATALOG` 中都有明确权限：

- **AUTO**：自动放行（只读、纯计算）。
- **CONFIRM**：副作用工具，需要用户确认。
- **FORBIDDEN**：永远禁止（删除、执行、外发）。

权限模式由环境变量 `PERMISSION_MODE` 控制：

- `read_only`：默认最小权限。
- `confirm`：副作用需确认。
- `unrestricted`：显式授权后放行（教学演示）。

### 2. 沙箱执行：文件系统和网络隔离

- 文件操作限定在 `~/learn-agent-sandbox`。
- 路径经过绝对化 + 前缀校验，防止目录遍历逃逸。
- `external_api` 在教学模式下仅返回模拟结果，不发起真实网络请求。

### 3. 最小权限原则：默认只读，显式授权写入

- 默认 `read_only` 模式，任何写入都被拦截。
- 切换到 `confirm` 后，写文件/网络访问需要用户输入 `y/yes/是/确认/允许`。
- 参数级限制：只读模式下连 `overwrite=true` 也会被拦截。

### 4. 危险操作分类

| 分类 | 例子 | 行为 |
|---|---|---|
| 自动放行 | `calculator`, `read_file`, `search_files` | 直接执行 |
| 必须确认 | `write_file`, `external_api` | `read_only` 拒绝，其他模式需确认 |
| 永远禁止 | `delete_file`, `execute_shell`, `send_email` | 所有模式拒绝 |

---

## 三、文件结构

```
lesson-07/
├── agent.py                 # 主 Agent：带权限模式开关
├── permissions.py           # 权限模型核心（分类、检查、上下文、报告）
├── tools.py                 # 受控工具 + 沙箱 + 确认门
├── context_manager.py       # 复用 lesson-04/06
├── benchmark.py             # 自动验证权限机制
├── PERMISSION_MANIFEST.md   # 权限模型清单
├── pyproject.toml
├── .gitignore
└── README.md
```

---

## 四、运行

### 环境准备

```bash
cd ~/learn-agent/lesson-07
export LLM_API_KEY="sk-xxxxx"
export LLM_BASE_URL="https://api.moonshot.cn/v1"
export LLM_MODEL="moonshot-v1-8k"
```

### 1. 默认只读模式

```bash
uv run python agent.py
```

输入：

```
把 hello 写入 ~/learn-agent-sandbox/demo.txt
```

观察：Agent 调用 `write_file` 时被权限系统拦截，返回 `"❌ 权限拒绝：当前处于只读模式..."`。

### 2. 确认模式（副作用需确认）

```bash
export PERMISSION_MODE=confirm
uv run python agent.py
```

输入同样的写入请求。

观察：终端会提示：

```
⚠️ 工具 'write_file' 请求执行以下操作，可能改变系统状态或访问外部网络：
   参数：{...}
   是否允许？(y/n)
```

输入 `y` 后文件才会被写入。

### 3. 无限制模式（教学演示）

```bash
export PERMISSION_MODE=unrestricted
uv run python agent.py
```

输入写入请求，观察 `write_file` 直接执行。

### 4. 自动验证（不依赖模型 API）

```bash
uv run python benchmark.py
```

输出 10 组测试结果，覆盖：

- 工具危险等级分类
- 只读模式拦截写入
- 参数级拦截
- 确认模式行为
- 用户拒绝逻辑
- 永远禁止的危险工具
- `execute_tool` 端到端拦截
- `unrestricted` 模式放行
- 沙箱路径隔离
- 权限报告生成

---

## 五、动手练习任务

### 练习 1：观察只读模式拦截

在默认 `read_only` 模式下运行：

```
读取 ~/learn-agent/lesson-06/README.md 的前 5 行
```

观察 `read_file` 被放行。然后输入：

```
把刚才的内容保存到 ~/learn-agent-sandbox/summary.txt
```

观察 `write_file` 被拦截。

### 练习 2：给副作用工具增加确认门

切换到 `PERMISSION_MODE=confirm`，分别输入：

```
计算 365 * 24
把结果写入 ~/learn-agent-sandbox/calc.txt
```

观察 `calculator` 自动执行，而 `write_file` 弹出确认提示。

### 练习 3：测试沙箱隔离

输入：

```
读取 /etc/passwd 的前 10 行
```

观察：即使 `read_file` 是只读工具，也会因为路径超出沙箱而被拒绝。

### 练习 4：永远禁止的操作

尝试让 Agent 调用 `delete_file` 或 `execute_shell`：

```
删除 ~/learn-agent-sandbox/demo.txt
执行 ls -la 命令
```

观察：无论处于哪种模式，这些工具都会被权限系统拒绝。

### 练习 5：编写权限模型清单

打开 `PERMISSION_MANIFEST.md`，为本项目添加一条新规则，例如：

```markdown
| `upload_file` | CONFIRM | ❌ | ⚠️ | ✅ | 上传文件到远程服务器 |
```

然后在 `permissions.py` 和 `tools.py` 中实现对应的模拟工具，并在 `benchmark.py` 中补充测试。

---

## 六、核心代码解读

### 1. 权限检查入口

```python
decision = check_permission(ctx, tool_name, arguments)
if not decision["allowed"]:
    return f"❌ 权限拒绝：{decision['reason']}"
```

所有工具调用前必须经过 `check_permission`。

### 2. 只读模式拦截

```python
if ctx.mode == PermissionMode.READ_ONLY and not perm.allowed_in_read_only:
    return {"allowed": False, "reason": "当前处于只读模式..."}
```

默认最小权限：写入类工具一律拒绝。

### 3. 确认门

```python
if decision["needs_confirm"]:
    approved = confirm_callback(prompt, arguments)
    if not confirm_tool(ctx, name, arguments, approved):
        return f"❌ 权限拒绝：用户未授权工具 '{name}'。"
```

副作用工具必须获得用户明确授权才会执行。

### 4. 沙箱路径校验

```python
def is_path_inside_sandbox(path: str, sandbox_root: str) -> bool:
    abs_path = os.path.abspath(os.path.expanduser(path))
    abs_root = os.path.abspath(os.path.expanduser(sandbox_root))
    return abs_path.startswith(abs_root + os.sep) or abs_path == abs_root
```

防止路径遍历攻击，所有文件操作限定在沙箱内。

---

## 七、常见问题

**Q: `unrestricted` 模式安全吗？**  
A: 不安全，仅用于教学演示。生产环境应始终使用 `read_only` 或 `confirm`。

**Q: 为什么 `external_api` 不发起真实请求？**  
A: 本课重点是权限控制，不是网络调用。模拟请求可以避免误操作访问真实服务。

**Q: 确认模式里用户拒绝一次后，同一会话还能再试吗？**  
A: 可以，每次调用都会重新询问用户，直到用户授权或任务结束。

**Q: 如何扩展新的危险工具？**  
A: 在 `PERMISSION_CATALOG` 中注册，设置 `danger=DangerLevel.FORBIDDEN` 或 `CONFIRM`，并在 `get_tools()` 中声明 schema 即可。
