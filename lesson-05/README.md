# 🛡️ 模块 5：Failure-First Design —— 能从多种错误中恢复的 Agent

## 一、这个任务在干什么？

模块 4 实现了 Context 管理，但当 Agent 真正运行时，还会遇到各种**非预期失败**：

- 模型陷入**死循环**：反复调用相同工具、相同参数
- 模型**跑偏**：用户让"生成文本"，模型却在"搜索文件"
- 模型**幻觉调用**：调用不存在的工具名
- 模型传**异常参数**：`limit=100` 超出限制
- **部分失败**：一次发起 3 个工具调用，其中 1 个失败
- **API 限流/超时**：429 Too Many Requests、连接超时

本课目标是：**为每种失败加 Harness 防护**，让 Agent 能从错误中恢复，而不是崩溃或无限循环。

---

## 二、Harness 防护层设计

在 Agent Loop 和工具执行之间插入 `AgentHarness` 层，负责检测、拦截、记录、恢复。

| 防护类型 | 触发条件 | Harness 行为 | 对应 lesson-04 的问题 |
|---|---|---|---|
| **死循环检测** | 连续 3 步调用相同工具+参数 | 打断循环，返回提示 | 反复 `search_files` 读桌面 |
| **目标偏离** | 当前工具与用户目标关键词不匹配 | 警告模型，引导回正 | 从"生成文本"跳到"搜索文件" |
| **未知工具拦截** | 工具名不在 `TOOL_REGISTRY` | 返回可用工具列表 | 调用不存在的工具 |
| **异常参数修正** | `limit>30`、`offset<1` 等 | 自动修正参数并提示 | 传 `limit=100` |
| **部分失败恢复** | 多工具调用中部分失败 | 记录错误，继续执行其他 | 3 个调用中 1 个失败 |
| **限流/超时重试** | HTTP 429/503/超时 | 指数退避重试 3 次 | API 限流直接报错 |

---

## 三、文件结构

```
lesson-05/
├── harness.py          # 核心：Failure-First 防护层
├── agent.py            # 集成 Harness 的 Agent Loop
├── tools.py            # 复用工具 + flaky_tool（模拟不稳定服务）
├── context_manager.py  # 复用 lesson-04
├── injector.py         # 错误注入器（测试用）
├── benchmark.py        # 自动验证 Harness
├── README.md
├── pyproject.toml
├── .python-version
└── .gitignore
```

---

## 四、运行

查看 6 种防护效果，有两种方式：**交互式注入**和**自动验证**。

### 方式 1：交互式注入（逐个体验）

在 `~/learn-agent/lesson-05` 目录下，每次测试前设置 `INJECT_ERRORS` 环境变量，然后启动 agent。

#### 1. 死循环检测

```bash
export INJECT_ERRORS=dead_loop
uv run python agent.py
```

输入：
```
搜索我的主目录
```

观察：
- 模型调用 `search_files`
- 连续 3 次相同调用后
- 终端显示：`🛑 检测到死循环：连续 3 步调用相同工具组合 [...]`

#### 2. 未知工具拦截

```bash
export INJECT_ERRORS=unknown_tool
uv run python agent.py
```

输入：
```
用 flaky_tool 查询数据库
```

观察：
- 模型调用 `flaky_tool`（合法工具）
- Injector 替换为 `magic_spell`（非法工具）
- Harness 拦截：`❌ 错误：未知工具 'magic_spell'`
- 返回可用工具列表，模型修正为合法工具

**注意**：不要直接输入"调用 magic_spell 工具"，因为模型会从 system prompt 知道可用工具列表，直接拒绝而不调用。Injector 的作用是在模型调用**合法工具**时偷偷替换为非法工具名，这样才能测试 Harness 的拦截能力。

#### 3. 参数修正（无需注入，直接测试）

```bash
unset INJECT_ERRORS
uv run python agent.py
```

输入：
```
读取文件 ~/test.txt limit=100
```

观察：
- 模型传 `limit=100`
- Harness 修正：`⚠️ 参数修正：limit=100 超过最大限制 30，已自动调整为 30`
- 工具正常执行

#### 4. 限流重试

```bash
export INJECT_ERRORS=rate_limit
uv run python agent.py
```

输入：
```
用 flaky_tool 查询数据库
```

观察：
- API 返回 429
- Harness 等待 2 秒后重试
- 再失败等待 4 秒，再失败等待 8 秒
- 3 次后放弃，返回友好错误

#### 5. 部分失败恢复

```bash
export INJECT_ERRORS=partial
uv run python agent.py
```

输入：
```
读取文件 ~/test.txt
```

观察：
- 多个工具调用中，某个返回错误
- Harness 记录错误，继续执行其他
- 最终返回部分结果 + 错误提示

#### 6. 目标偏离（无需注入，观察自然行为）

```bash
unset INJECT_ERRORS
uv run python agent.py
```

输入：
```
生成50段超长文本
```

观察：
- 如果模型跑偏去调用 `search_files`
- Harness 输出：`⚠️ 目标偏离警告：用户目标是 '生成50段超长文本'，但当前调用 'search_files' 可能不直接相关`

### 方式 2：自动验证（一键看全部）

```bash
export LLM_API_KEY="sk-xxxxx"
uv run python benchmark.py
```

自动运行 6 个场景，输出汇总表格：

```
📊 验证结果汇总
场景                  状态    Harness      步数    错误数
死循环检测            ✅ PASS  是           3       0
未知工具拦截          ✅ PASS  是           2       1
参数越界修正          ✅ PASS  是           1       0
部分失败恢复          ✅ PASS  是           4       1
限流重试              ✅ PASS  是           5       0
正常执行              ✅ PASS  否           1       0
```

### 推荐流程

先跑 **benchmark** 看整体效果，再挑感兴趣的单个场景用 **交互式** 深入观察：

```bash
# 1. 先看自动验证结果
cd ~/learn-agent/lesson-05
export LLM_API_KEY="sk-xxxxx"
uv run python benchmark.py

# 2. 再逐个体验
export INJECT_ERRORS=dead_loop
uv run python agent.py
# 输入：搜索我的主目录

# 3. 换下一个
export INJECT_ERRORS=rate_limit
uv run python agent.py
# 输入：用 flaky_tool 查询数据库
```

**注意**：`INJECT_ERRORS` 环境变量只在当前终端有效，关掉窗口或新开标签页需要重新设置。

---

## 五、动手练习任务

### 练习 1：观察死循环检测

输入：

```
搜索我的主目录
```

（启用 `INJECT_ERRORS=dead_loop`）

观察：
- 模型尝试调用 `search_files`
- Harness 检测到连续 3 次相同调用
- 输出：`🛑 检测到死循环：连续 3 步调用相同工具组合 [...]`

### 练习 2：观察未知工具拦截

输入：

```
调用 magic_spell 工具
```

（启用 `INJECT_ERRORS=unknown_tool`）

观察：
- 模型尝试调用 `magic_spell`
- Harness 拦截：`❌ 错误：未知工具 'magic_spell'`
- 返回可用工具列表，模型修正为合法工具

### 练习 3：观察参数修正

输入：

```
读取文件 ~/test.txt limit=100
```

观察：
- 模型传 `limit=100`
- Harness 修正：`⚠️ 参数修正：limit=100 超过最大限制 30，已自动调整为 30`
- 工具正常执行

### 练习 4：观察限流重试

输入：

```
用 flaky_tool 查询数据库
```

（启用 `INJECT_ERRORS=rate_limit`）

观察：
- API 返回 429
- Harness 等待 2 秒后重试
- 再失败等待 4 秒，再失败等待 8 秒
- 3 次后放弃，返回友好错误

### 练习 5：观察部分失败恢复

输入：

```
读取文件 ~/test.txt
```

（启用 `INJECT_ERRORS=partial`）

观察：
- 3 个工具调用中 1 个失败
- Harness 记录错误，继续执行其他 2 个
- 最终返回部分结果 + 错误提示

### 练习 6：记录真实跑偏并改进

正常输入：

```
生成 50 段超长文本
```

观察模型是否偏离目标（比如去搜索文件）。如果偏离：
- Harness 输出 `⚠️ 目标偏离警告`
- 检查 `harness.get_status_report()` 中的 `drift_warnings` 计数
- 思考：目标关键词映射是否需要扩展？

---

## 六、Harness 核心代码解读

### 死循环检测

```python
def check_dead_loop(self, step, tool_calls):
    # 记录当前调用
    current = [(tc["function"]["name"], tc["function"]["arguments"]) 
               for tc in tool_calls]
    self.call_history.append(current)
    
    # 检查最近 N 步是否完全相同
    if len(self.call_history) >= self.repeat_threshold:
        last_n = self.call_history[-self.repeat_threshold:]
        if all(c == last_n[0] for c in last_n):
            return f"检测到死循环：连续 {self.repeat_threshold} 步调用相同工具"
```

### 指数退避重试

```python
def handle_api_error(self, error):
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in (429, 502, 503, 504):
            if self.api_retry_count < self.max_retries:
                wait = self.backoff_base ** self.api_retry_count  # 2^0, 2^1, 2^2
                self.api_retry_count += 1
                return True, wait  # 重试，等待 wait 秒
    return False, 0.0  # 不重试
```

### 错误信息丰富化

```python
def enrich_error(self, base_error, tool_name):
    suggestions = {
        "read_file": "建议：检查文件路径是否正确，使用 search_files 查找文件。",
        "write_file": "建议：检查路径权限，或设置 overwrite=true 强制覆盖。",
        # ...
    }
    return f"{base_error}\n{suggestions.get(tool_name, '建议：检查参数是否正确。')}"
```

---

## 七、常见问题

**Q: 死循环检测阈值 3 步是不是太低了？**  
A: 教学用 3 步方便快速复现。生产环境建议 5 步，且可配置。

**Q: 目标偏离检测的启发式规则准确吗？**  
A: 教学用简单关键词匹配。生产环境可用 LLM 判断当前动作与目标的相关性。

**Q: 为什么错误注入用 monkey-patch？**  
A: 不需要修改工具代码，注入和恢复都干净。生产环境可用依赖注入或中间件。

**Q: Harness 层会不会让 Agent 变慢？**  
A: 检测逻辑都是本地计算（O(1)），不影响。重试会增加延迟，但这是必要的。

**Q: 如果 Harness 本身有 bug 怎么办？**  
A: Harness 也遵循"错误作为正常返回值"原则，绝不抛异常。所有检测失败都返回提示信息，让模型继续决策。
