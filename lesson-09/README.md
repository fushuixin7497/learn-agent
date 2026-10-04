# 🚪 模块 9：Human-in-the-Loop Gates —— 人为参与的门

## 一、这个任务在干什么？

模块 8 解决了"谁来决定能不能做"（权限光谱）。本课处理真实部署中必然出现的下一个问题：

> **Agent 跑长任务时用户不在场，中途遇到不可逆操作（删文件、发邮件），怎么办？**

两种坏做法：

- **干等**：阻塞进程等用户回话，一晚白跑；
- **跳过**：不等人批准继续干，违背 Human-in-the-Loop 原则。

正确做法是**确认门（Confirmation Gate）**——不可逆操作到来时，Agent 不等待、不越权，而是**把现场完整封存后退出**，等人回来开门：

```
Agent 运行 ──→ 遇到不可逆操作 ──→ 门：暂停、落盘、退出
                                      │ （用户可能几小时后才回来）
                                      ▼
                resume 恢复现场 ──→ 人在门处决定 ──→ 从暂停点无缝继续
```

核心代码只有一个文件：`hitl_gate.py`（纯标准库）。

---

## 二、核心概念

### 1. 门的触发依据是"可逆性"，不是"危险性"

这是和模块 7/8 的关键区别：

| 类别 | 例子 | 处理方式 |
|---|---|---|
| 只读 | `read_file` | 自动执行，不经过门 |
| 危险 | `execute_shell` | 任何模式下**永久禁止**，不会到门这里 |
| 不可逆但合理 | `delete_file`、`send_email` | **在门前暂停**，等人批准 |

危险操作直接禁掉；门只服务于"合理但不可逆"的操作——这正是需要人类判断力的位置。

### 2. 悬挂状态（Suspended State）：门的全部现场

暂停时落盘的是一个 `SuspendedState`，它必须自足——恢复任务只需要这一个文件：

- `messages`：**完整对话上下文**。模型是只认上下文的（本课的 `fake_model` 就是证明：恢复后从同一套 messages 出发，会做出和暂停前一致的决定），少存一条消息恢复出来的就是另一个 Agent。
- `trace`：**执行轨迹**。人批准前有权回看"你怎么走到这一步的"。
- `pending_tool / pending_args`：卡在门上的那个调用——批准后要执行的就是它。
- `reason / suspended_at / session_id`：为什么暂停、何时暂停、怎么找回它。

### 3. 恢复（Resume）的两条出路

```bash
python3 hitl_gate.py run          # 启动任务，在 delete_file 前暂停落盘
python3 hitl_gate.py resume <id>  # 恢复现场，人在门处做决定
```

- **批准**：执行挂起的调用，结果作为 tool 消息喂回模型，循环继续；
- **拒绝**：把拒绝作为 user 消息喂回模型——**拒绝不是终点**，模型据此换条路把任务安全收尾（本课演示的就是：不删了，给出保留文件的收尾报告）。

---

## 三、运行与输出解读

```bash
cd ~/learn-agent/lesson-09
python3 hitl_gate.py run
```

输出分两段。第一段：Agent 正常执行 `read_file`，遇到 `delete_file` 时在门前暂停：

```
【确认门】不可逆操作 'delete_file' 需要人工批准
  操作: delete_file {'path': '~/tmp/old_report.txt'}
  完整状态已保存: .../.lesson09_suspensions/2b56be35.json
  进程退出。回来后执行：
    python3 hitl_gate.py resume 2b56be35
```

注意**进程真的退出了**——此时没有进程在等你，`cat .lesson09_suspensions/2b56be35.json` 能看到完整封存的 JSON 状态。

第二段：`resume` 恢复现场，先看现场摘要（暂停时间、待决定操作、轨迹/对话规模），然后人在门处做决定。建议两个都试：

- 输入 `y` → 轨迹 `gate_resume approved=True` → 最终回答"任务完成"
- 输入 `n` → 轨迹 `gate_resume approved=False` → 最终回答"删除被拒绝，文件保留"

完整的执行轨迹展示了门的完整生命周期：

```
1. tool_call      read_file      ← 门之前：正常执行
2. gate_suspend   delete_file    ← 门：暂停落盘
3. gate_resume    approved=True  ← 门：人做决定（可能隔了几小时）
4. final                         ← 门之后：任务收尾
```

---

## 四、与其他模块的关系

- **模块 7（最小权限）**：提供"哪些操作不可逆"的分类依据；模块 7/8 的确认是**即时的**（用户盯着屏幕按 y/n），本课解决的是**异步的**确认——用户不在场时确认如何不丢、不堵、不越权。
- **模块 8（权限光谱）**：门处的决定可以接任何模式——本课用的是"人在场时等于 CONFIRM 模式"；接到 AUTO 模式时，只有 slow path 判为不确定的不可逆操作才会到门前。
- **后续可扩展**：
  - 把 `SuspendedState` 里的 JSON 换成加密存储 + 审批系统 webhook，就是企业级审批流；
  - 门支持批量：一次 resume 处理队列里多个挂起的决定；
  - 配合 cron/CI，让 Agent 在无人值守时段跑到门前自动封存，人白天批量审。
