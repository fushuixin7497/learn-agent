# 🌤️ 模块 6-01：天气查询与优雅降级（轻量版）

## 一、这个例子在讲什么？

只聚焦一件事：**查询天气时，如果外部 API 不可用，Agent 如何自动降级到本地文件，而不是崩溃。**

这就是 Graceful Degradation 的最小可用示例：

```
外部 API 成功 → 直接返回答案
外部 API 失败 → 读取本地 weather.json
全部失败     → 给用户清晰说明 + 建议
```

代码尽量轻量，没有 context manager、没有复杂的降级模块，全部逻辑都在 `agent.py` 里，方便你一行一行吃透。

---

## 二、文件结构

```
lesson-06-01/
├── agent.py      # 主 Agent：天气查询 + 降级逻辑
├── weather.json  # 本地兜底天气数据
└── README.md     # 本说明
```

---

## 三、运行

### 环境准备

```bash
cd ~/learn-agent/lesson-06-01
export LLM_API_KEY="sk-xxxxx"
export LLM_BASE_URL="https://api.moonshot.cn/v1"
export LLM_MODEL="moonshot-v1-8k"
```

### 1. 默认模式：观察 API 失败后自动降级

```bash
uv run python agent.py
```

输入：

```
北京天气怎么样？
```

观察：

- 模型先调用 `query_weather_api`
- 如果返回 503，模型会收到"换路径"提示
- 然后调用 `read_local_weather` 读取 `weather.json`
- 最终返回天气结果，并说明经历了哪些步骤

因为 `query_weather_api` 有 50% 概率失败，你可以多试几次，分别看到"直接成功"和"降级成功"两条路径。

### 2. 强制 API 失败：只看降级路径

```bash
export WEATHER_API_FAIL=1
uv run python agent.py
```

输入同样的请求，观察 Agent 如何从 `query_weather_api` 切换到 `read_local_weather`。

### 3. 强制所有路径失败：观察优雅失败

```bash
export WEATHER_API_FAIL=1
export WEATHER_FILE_PATH=/tmp/不存在的文件.json
uv run python agent.py
```

输入：

```
北京天气怎么样？
```

观察：

- `query_weather_api` 失败
- `read_local_weather` 也失败（文件不存在）
- Agent 不会抛异常，而是返回一份状态报告：失败原因 + 已完成步骤 + 下一步建议

---

## 四、核心代码速览

### 1. 两个工具

```python
def query_weather_api(city: str) -> str:
    # 模拟外部 API，可能返回 503
    ...

def read_local_weather(city: str) -> str:
    # 读取本地 weather.json 作为兜底
    ...
```

**关键设计**：工具失败时返回 `"错误：..."` 字符串，而不是抛异常。这样 Agent 循环不会 crash。

### 2. Agent 循环

```python
def agent_loop(user_input: str) -> str:
    # 1. 调用模型
    # 2. 如果 finish_reason == "stop"，直接返回
    # 3. 如果 finish_reason == "tool_calls"，执行工具
    # 4. 工具失败时，把"换路径提示"回填给模型，继续循环
    # 5. 超过最大步数或模型请求失败，返回优雅失败报告
```

### 3. 优雅失败报告

```python
def graceful_failure_report(user_input, completed, failed, reason):
    return """
    【无法完成查询】
    你的请求：...
    原因：...
    ⚠️ 失败的步骤：...
    💡 建议：...
    """
```

---

## 五、动手练习

### 练习 1：修改失败概率

在 `agent.py` 中找到 `query_weather_api`，把 `random.random() < 0.5` 改成 `0.0` 或 `1.0`，观察不同失败率下的行为。

### 练习 2：添加新城市

在 `weather.json` 中添加你所在的城市，然后输入城市名查询。

### 练习 3：换一种失败场景

修改 `query_weather_api`，让它在 city 为空时返回特定错误，观察 Agent 是否会反复调用同一个失败工具。

### 练习 4：把 lesson-06 的降级思想迁移过来

对比一下：

- `lesson-06`：通用 Graceful Degradation 框架
- `lesson-06-01`：只解决天气查询这一个问题

理解了这一个例子，再回头看 `lesson-06` 就会轻松很多。

---

## 六、常见问题

**Q: 为什么 external API 是模拟的，不是真的天气接口？**  
A: 教学重点不是接入真实第三方服务，而是演示"服务不可用时代理如何反应"。换成真实 API 只需要替换 `query_weather_api` 里的请求逻辑。

**Q: 模型为什么会自动换路径？**  
A: 因为 system prompt 里明确写了"如果 query_weather_api 返回 503，必须改用 read_local_weather"，并且工具失败时我们把换路径提示回填给了模型。

**Q: 为什么错误不抛异常？**  
A: 这是 Failure-First Design 的核心原则：错误作为正常返回值，让上层循环自己决定是重试、换路径还是优雅失败。
