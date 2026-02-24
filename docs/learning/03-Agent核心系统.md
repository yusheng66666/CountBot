# 三、Agent 核心系统

Agent 系统是 CountBot 的大脑，负责接收用户消息、调用 LLM、执行工具，并将结果返回给用户。

## 1、模块概览

```
backend/modules/agent/
├── loop.py            # ★ Agent 主循环（ReAct 模式）
├── context.py         # ★ 上下文构建器（系统提示词）
├── memory.py          # ★ 记忆存储管理
├── subagent.py        # ★ 子代理管理
├── skills.py          # 技能加载器
├── heartbeat.py       # 主动问候服务
├── personalities.py   # 12 种性格预设
├── analyzer.py        # 消息分析与总结触发
├── prompts.py         # 提示词模板
└── task_manager.py    # 取消令牌管理
```

## 2、相关文档导航

Agent 的核心子系统已各自独立成文档，建议按顺序阅读：

| 文档 | 内容 | 核心文件 |
|------|------|----------|
| [04 上下文构建器](04-上下文构建器.md) | ContextBuilder、系统提示词构建、性格加载、多模态支持 | `context.py`, `personalities.py` |
| [05 记忆系统](05-记忆系统.md) | MemoryStore、行式存储、关键词搜索、对话总结器 | `memory.py`, `analyzer.py`, `prompts.py` |
| [06 子代理系统](06-子代理系统.md) | SubagentManager、后台任务、SpawnTool、与主 Agent 对比 | `subagent.py`, `tools/spawn.py` |
| [07 工具系统设计](07-工具系统设计.md) | 工具基类、注册表、内置工具详解、自定义工具开发 | `tools/` 目录 |
| [08 技能系统](08-技能系统.md) | 技能加载器、SKILL.md、双目录加载、自定义技能开发 | `skills.py` |

## 3、AgentLoop 类设计

核心文件：`backend/modules/agent/loop.py`

### （1）构造参数

```python
class AgentLoop:
    def __init__(self,
        provider,          # LLM 提供商
        workspace,         # 工作区路径
        tools,             # 工具注册表
        context_builder,   # 上下文构建器
        subagent_manager,  # 子代理管理器
        model,             # 模型名称
        max_iterations=25, # 最大迭代次数
        max_retries=3,     # 工具重试次数
        retry_delay=1.0,   # 重试间隔
        temperature=0.7,   # 生成温度
        max_tokens=4096,   # 最大 token 数
    ):
```

`AgentLoop` 是一个**无状态的处理器**，每次调用 `process_message()` 都是独立的处理流程。它不保存历史消息，也不维护会话状态——这些由外部的会话管理和消息数据库负责。

### （2）关键参数说明

| 参数 | 作用 | 说明 |
|------|------|------|
| `max_iterations` | 防止无限循环 | 最多进行 25 轮 LLM 调用 |
| `max_retries` | 工具执行容错 | 单个工具失败时最多重试 3 次 |
| `retry_delay` | 重试等待 | 两次重试间等待 1 秒 |
| `temperature` | 生成随机性 | 0.7 是比较平衡的值 |

## 4、谁调用了 AgentLoop

AgentLoop 本身不监听任何消息，它是一个被动调用的处理器。有四条路径会触发 ReAct 循环：

### （1）WebSocket 聊天

用户在浏览器中通过 WebSocket 发送消息，调用 `process_message()` 流式输出：

```
用户点击发送
    ↓
app.py websocket_endpoint()            ← 每个 WebSocket 连接创建一个 AgentLoop
    ↓
ws/connection.py handle_websocket()    ← 注册连接，进入事件循环
    ↓
ws/events.py websocket_event_loop()    ← while True 收消息
    ↓
ws/events.py handle_message_event()    ← 收到 type="message"
    ↓
async for chunk in agent_loop.process_message():   ← 进入 ReAct 循环
    await streaming_handler.write(chunk)            ← 流式推给前端（WebSocket）
```

### （2）HTTP SSE 聊天

前端也可以通过 HTTP 的 SSE（Server-Sent Events）接口发送消息，同样调用 `process_message()` 流式输出：

```
前端 POST /api/chat/send
    ↓
api/chat.py send_message()             ← HTTP SSE 端点
    ↓
async for chunk in agent_loop.process_message():   ← 进入 ReAct 循环
    yield f"event: chunk\ndata: ..."                ← 流式推给前端（SSE）
```

与 WebSocket 路径的区别：WebSocket 是长连接双向通信，SSE 是单次 HTTP 请求的流式响应。

### （3）渠道消息（飞书/钉钉/Telegram 等）

用户从第三方渠道发送消息，调用 `process_message()` 收集为完整字符串：

```
渠道收到用户消息
    ↓
channels/handler.py handle_message()              ← 消息处理器
    ↓
async for chunk in agent_loop.process_message():  ← 进入 ReAct 循环
    parts.append(chunk)                            ← 收集所有 chunk
    ↓
"".join(parts)                                     ← 拼成完整字符串，一次性发回渠道
```

### （4）定时任务（Cron）

系统定时触发的任务，通过 `process_direct()` 调用：

```
调度器触发定时任务
    ↓
cron/executor.py execute()                         ← 任务执行器
    ↓
agent_loop.process_direct(content)                 ← 便捷方法（内部调用 process_message）
    ↓
收集全部 chunk → 返回完整字符串
```

### （5）四条路径对比

| 路径           | 入口文件                  | 调用方式                | 输出方式                    |
| ------------ | --------------------- | ------------------- | ----------------------- |
| WebSocket 聊天 | `ws/events.py`        | `process_message()` | 逐块 yield → WebSocket 推送 |
| HTTP SSE 聊天  | `api/chat.py`         | `process_message()` | 逐块 yield → SSE 推送       |
| 渠道消息         | `channels/handler.py` | `process_message()` | 收集全部 → 一次性返回            |
| 定时任务         | `cron/executor.py`    | `process_direct()`  | 收集全部 → 返回字符串            |

四条路径最终都进入同一个 `process_message()` 的 ReAct 循环，区别只在于调用者如何消费 yield 出来的 chunk。

### （6）为什么同时设计 WebSocket 和 SSE 两种接入方式？

前端既可以走 WebSocket（`ws/events.py`），也可以走 HTTP SSE（`api/chat.py`），两者最终都调用同一个 `process_message()`，但定位不同：

**WebSocket 是主力通道，功能最完整。** 浏览器前端主要使用 WebSocket，因为它是长连接双向通信，除了聊天之外还能支持：
- 工具调用的实时状态通知（"正在读取文件..."）
- 用户取消正在生成的回复（通过取消令牌）
- ping/pong 心跳保活
- 多会话订阅（subscribe/unsubscribe）

**SSE 是轻量级的兜底方案，** 解决 WebSocket 搞不定或不方便的场景：
- **简单客户端调用**：用 `curl` 或 Postman 测试时，SSE 就是一个普通的 POST 请求，不需要 WebSocket 客户端
- **代理/防火墙兼容**：有些企业网络环境或反向代理（Nginx、CDN）对 WebSocket 支持不好，会断连或不允许协议升级。SSE 走的是普通 HTTP，兼容性高得多
- **无状态场景**：不需要心跳、不需要工具通知，只要"发一条消息、拿流式回复"，SSE 更轻量

两者的具体差异：

| 对比项  | WebSocket（`ws/events.py`）                             | HTTP SSE（`api/chat.py`）                 |
| ---- | ----------------------------------------------------- | --------------------------------------- |
| 协议   | 长连接，双向通信                                              | 普通 HTTP 请求，单向流                          |
| 生命周期 | 打开页面就连上，关闭才断                                          | 发一条消息建一次连接，收完就断                         |
| 流式推送 | 通过 `BufferedStreamingHandler` 推 WebSocket 帧           | 通过 `event: message\ndata: ...` 推 SSE 事件 |
| 额外能力 | 心跳、订阅、工具通知、取消生成                                       | 只有聊天，无额外能力                              |
| 状态管理 | `ConnectionManager` 维护连接映射                            | 无状态，每次请求独立                              |
| 代码组织 | 逻辑分散在 `connection.py`、`events.py`、`streaming.py` 协作完成 | 自包含在 `chat.py` 一个文件中，不依赖 `ws/` 模块       |

> **Java 类比**：WebSocket 相当于 Netty 长连接通道，SSE 相当于 Spring MVC 的 `SseEmitter`。如果你的项目同时有 App（用长连接）和 H5 页面（用 HTTP），也会提供两种接入方式。

## 5、ReAct 循环核心逻辑

ReAct（Reasoning + Acting）是 Agent 的核心运行模式。代码位于 `loop.py:50-361`。

### （1）整体流程

```
process_message() 入口
    │
    ├── 1. 设置会话上下文
    │   ├── 设置工具注册表的 session_id
    │   └── 设置 spawn 工具的上下文
    │
    ├── 2. 构建消息列表
    │   └── context_builder.build_messages(history, current_message, media, channel)
    │
    └── 3. 进入迭代循环（最多 max_iterations 次）
        │
        ├── 检查取消令牌 → 已取消则退出
        │
        ├── 获取工具定义列表
        │
        ├── 调用 LLM（流式）
        │   ├── provider.chat_stream(messages, tools, model, temperature, max_tokens)
        │   │
        │   ├── 解析流式响应：
        │   │   ├── chunk.is_content    → 追加到 content_buffer，yield 给调用者
        │   │   ├── chunk.is_tool_call  → 追加到 tool_calls_buffer
        │   │   ├── chunk.is_reasoning  → 追加到 reasoning_buffer
        │   │   ├── chunk.is_done       → 记录 finish_reason
        │   │   └── chunk.is_error      → yield 错误信息，return
        │
        ├── 如果有文本内容 → 记录完整响应
        │
        ├── 如果有工具调用 → 进入工具执行流程
        │   │
        │   ├── 将 assistant 消息（含 tool_calls）加入消息列表
        │   │
        │   └── 遍历每个工具调用：
        │       ├── 检查是否超过最大调用次数
        │       ├── 检查取消令牌
        │       ├── 发送工具调用通知（WebSocket）
        │       ├── 记录开始时间
        │       │
        │       ├── 重试循环（最多 max_retries 次）：
        │       │   ├── 执行工具 execute_tool(name, args)
        │       │   ├── 成功 → break
        │       │   └── 失败 → 等待 retry_delay 后重试
        │       │
        │       ├── 记录工具对话历史
        │       ├── 发送工具执行通知（WebSocket）
        │       └── 将工具结果加入消息列表（role: "tool"）
        │
        └── 如果没有工具调用 → 结束循环
```

### （2）循环终止条件

循环在以下三种情况下会终止：
- **LLM 不再调用工具**：正常结束，意味着 LLM 认为已经完成任务
- **达到最大迭代次数**：安全保护，防止 AI 陷入无限调用循环
- **用户取消**：通过取消令牌机制，用户可以随时中断处理

## 6、流式响应处理

### （1）async for + yield 模式

```python
# loop.py:112-135
async for chunk in self.provider.chat_stream(
    messages=messages,
    tools=tool_definitions,
    model=self.model,
    temperature=self.temperature,
    max_tokens=self.max_tokens,
):
    if chunk.is_content and chunk.content:
        content_buffer += chunk.content
        yield chunk.content              # ★ 实时流式返回文本给用户

    if chunk.is_tool_call and chunk.tool_call:
        tool_calls_buffer.append(chunk.tool_call)  # 收集工具调用

    if chunk.is_reasoning and chunk.reasoning_content:
        reasoning_buffer += chunk.reasoning_content  # 收集推理内容（DeepSeek 等）

    if chunk.is_error:
        yield chunk.error                # 错误也流式返回
        return
```

`process_message` 本身是一个**异步生成器**，使用了 `async for` + `yield` 模式。调用者可以逐块接收 LLM 的响应文本，实现打字机效果。

### （2）四种 chunk 类型

| 类型             | 说明   | 处理方式                          |
| -------------- | ---- | ----------------------------- |
| `is_content`   | 文本内容 | 追加到 buffer + yield 给调用者       |
| `is_tool_call` | 工具调用 | 收集到列表，等流式结束后统一执行              |
| `is_reasoning` | 推理内容 | 追加到 buffer（部分模型如 DeepSeek 支持） |
| `is_error`     | 错误信息 | yield 后立即 return              |

### （3）流式的好处

- 用户不需要等整个回复生成完毕，就能看到开头部分
- WebSocket 可以逐块推送给前端，渠道也可以逐段发送
- 如果中途出错，已经发送的内容不会丢失

## 7、工具执行与重试

### （1）重试机制

```python
# loop.py:213-229
for attempt in range(self.max_retries):
    try:
        result = await self.execute_tool(tool_name, tool_args)
        break
    except Exception as e:
        last_error = e
        if attempt < self.max_retries - 1:
            await asyncio.sleep(self.retry_delay)  # 等待后重试
```

工具执行失败时自动重试，但**不改变参数**（假设是临时性错误如网络超时）。如果是参数错误，重试也不会成功，但这种情况下工具的 `execute()` 方法通常返回错误字符串而非抛出异常。

### （2）工具调用通知

每次工具执行前后都会通过 WebSocket 发送通知：

```python
# 工具调用开始通知
await notify_tool_execution(session_id, tool_name, tool_args)

# 工具执行完成通知（含结果或错误）
await notify_tool_execution(session_id, tool_name, tool_args, result=result)
```

前端可以据此展示工具调用的实时状态（"正在读取文件..."、"正在执行命令..."）。

### （3）工具结果进入消息列表

工具执行完成后，结果以 `role: "tool"` 的格式加入消息列表，供 LLM 在下一轮迭代中参考：

```python
messages.append({
    "role": "tool",
    "tool_call_id": tool_call_id,
    "name": tool_name,
    "content": result
})
```

## 8、消息列表管理

Agent 循环中最重要的数据结构是 `messages` 列表，它维护了完整的对话上下文：

```python
messages = [
    {"role": "system", "content": "系统提示词..."},
    {"role": "user", "content": "历史消息1"},
    {"role": "assistant", "content": "历史回复1"},
    ...
    {"role": "user", "content": "当前用户消息"},
    # --- Agent 循环中动态添加 ---
    {"role": "assistant", "content": "...", "tool_calls": [...]},
    {"role": "tool", "tool_call_id": "...", "name": "read_file", "content": "文件内容..."},
    {"role": "assistant", "content": "最终回复"},
]
```

注意这个列表在循环过程中会不断增长：每次 LLM 的工具调用和工具的执行结果都会追加进去。这样 LLM 在下一轮迭代中就能看到之前的所有操作和结果。

## 9、process_direct 方法

```python
# loop.py:395-424
async def process_direct(self, content, session_id="cli:direct", ...):
    """直接处理消息（用于 CLI 或 cron 使用）"""
    response_parts = []
    async for chunk in self.process_message(message=content, session_id=session_id, ...):
        response_parts.append(chunk)
    return "".join(response_parts)
```

这是一个**便捷方法**，将流式生成器转换为完整字符串。适用于不需要流式输出的场景：
- 定时任务执行（cron）
- CLI 命令行调用
- 子代理处理

## 10、取消令牌机制

取消令牌（Cancel Token）允许用户中断正在进行的处理。在 Agent 循环中的多个检查点都会检查取消状态：

- 循环开始前
- 每轮迭代开始时
- 每个工具执行前

这确保了取消操作能及时生效，不会让用户等待一个不需要的长时间操作。

## 11、关键设计要点

（1）Agent Loop 的流式生成器设计使得文本可以实时推送给用户，无需等待完整响应
（2）工具调用的重试机制提高了可靠性，但不会无限重试（最多 3 次）
（3）取消令牌在循环的多个检查点被检查，确保取消操作能及时生效
（4）ContextBuilder 的降级策略保证了即使数据库异常也不会影响基本功能
（5）记忆系统采用文本文件而非数据库，是一个有意的简化决策

## 12、对应代码阅读指引

| 阅读顺序 | 文件 | 重点关注 |
|----------|------|----------|
| 1 | `agent/loop.py` 全文 | `process_message()` 的 ReAct 循环流程 |
| 2 | `agent/loop.py:112-135` | 流式响应处理的 async for + yield |
| 3 | `agent/loop.py:213-229` | 工具执行重试逻辑 |
| 4 | `agent/loop.py:395-424` | `process_direct()` 便捷方法 |
| 5 | `agent/task_manager.py` | 取消令牌管理 |
