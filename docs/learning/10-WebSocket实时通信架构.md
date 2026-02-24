# 十、WebSocket 实时通信架构

本文讲解 `ws/` 模块的内部架构，四个文件如何分工协作，实现从用户发消息到 AI 流式回复的完整链路。

> **前置知识**：建议先阅读 [知识库：FastAPI 中的 WebSocket](../knowledge-base/10-Web框架-FastAPI中的WebSocket.md)，了解 WebSocket 基本概念。

## 1、模块概览

```
backend/ws/
├── connection.py          # ★ 基础设施层：连接管理 + 消息模型 + 发送工具函数
├── events.py              # ★ 业务调度层：事件循环 + 事件路由 + 消息处理
├── streaming.py           # 发送优化层：缓冲流式输出，减少网络开销
├── tool_notifications.py  # 工具通知层：工具执行的实时状态推送
└── __init__.py
```

## 2、分层架构与依赖关系

四个文件形成清晰的分层结构，**上层依赖下层，下层不依赖上层**：

```
app.py websocket_endpoint()
    │
    ▼
┌─────────────────────────────────────────────────────┐
│  connection.py — 基础设施层                          │
│                                                      │
│  ConnectionManager  连接注册/注销/按会话分组           │
│  消息模型定义        MessageChunk, ToolCall 等        │
│  发送工具函数        send_message_chunk() 等          │
│  handle_websocket() 入口，接受连接后交给 events       │
└──────────────────────────┬──────────────────────────┘
                           │ 调用
                           ▼
┌─────────────────────────────────────────────────────┐
│  events.py — 业务调度层                              │
│                                                      │
│  websocket_event_loop()  while True 收消息           │
│  route_event()           根据 type 分发到 handler     │
│  handle_message_event()  核心：调 AgentLoop 处理消息   │
│  handle_ping_event()     心跳响应                     │
│  handle_subscribe_event() 绑定连接到会话              │
└───────────┬─────────────────────────┬───────────────┘
            │ 使用                     │ 使用
            ▼                          ▼
┌──────────────────────┐  ┌──────────────────────────┐
│  streaming.py        │  │  tool_notifications.py   │
│  发送优化层           │  │  工具通知层               │
│                      │  │                          │
│  BufferedStreaming-   │  │  ToolNotification-       │
│    Handler           │  │    Handler               │
│  攒 10 字符/10ms     │  │  开始/进度/完成/错误      │
│  再 flush 一次       │  │  通知前端                 │
└──────────┬───────────┘  └────────────┬─────────────┘
           │ 调用                       │ 调用
           ▼                            ▼
      connection.py                connection.py
      send_message_chunk()         send_to_session()
```

### （1）与 Java 分层的类比

| ws/ 文件 | 职责 | Java 对应概念 |
|----------|------|--------------|
| `connection.py` | 连接管理 + DTO | `WebSocketSessionRegistry` + 消息 DTO 类 |
| `events.py` | 业务分发 + 处理 | `@OnMessage` 中的 Controller / Dispatcher |
| `streaming.py` | 输出缓冲优化 | `BufferedOutputStream`，攒够再 flush |
| `tool_notifications.py` | 事件通知 | `ApplicationEventPublisher` 发布工具执行事件 |

## 3、connection.py — 基础设施层

核心文件：`backend/ws/connection.py`

这是整个 ws/ 模块的地基，其他三个文件都依赖它。包含三部分内容：

### （1）消息模型定义

用 Pydantic 定义了客户端和服务端的消息格式：

```python
# 客户端 → 服务端
class ClientMessage(BaseModel):
    type: str                    # 消息类型：message / ping / subscribe
    session_id: str              # 会话 ID
    content: str | None = None   # 消息内容

# 服务端 → 客户端（基类）
class ServerMessage(BaseModel):
    type: str                    # 消息类型

# 具体的服务端消息类型
class MessageChunk(ServerMessage):    # 流式文本片段
    content: str

class ToolCall(ServerMessage):        # 工具调用通知
    tool: str
    arguments: dict[str, Any]

class ToolResult(ServerMessage):      # 工具执行结果
    tool: str
    result: str
    duration: float | None

class MessageComplete(ServerMessage): # 回复结束信号
    message_id: str

class ErrorMessage(ServerMessage):    # 错误信息
    message: str
    code: str | None
```

Java 类比：这些就是 WebSocket 通信的 **DTO（Data Transfer Object）**，类似于 Java 中定义的 `@Data class ChatMessage { ... }`。

### （2）ConnectionManager 连接管理器

```python
class ConnectionManager:
    _connections: dict[str, WebSocket]           # connection_id → WebSocket 对象
    _session_connections: dict[str, set[str]]    # session_id → {connection_id, ...}
    _lock: asyncio.Lock                          # 并发锁
```

核心数据结构用两个字典维护了 "连接" 和 "会话" 的映射关系：

```
用户张三打开了两个浏览器标签页，都在会话 session-1 中聊天：

_connections:
  "conn-A" → WebSocket对象A（标签页1）
  "conn-B" → WebSocket对象B（标签页2）
  "conn-C" → WebSocket对象C（李四的标签页）

_session_connections:
  "session-1" → {"conn-A", "conn-B"}    ← 张三的两个标签页
  "session-2" → {"conn-C"}              ← 李四
```

| 方法 | 作用 | Java 类比 |
|------|------|-----------|
| `connect(websocket)` | 注册连接，调用 `accept()` | `@OnOpen` |
| `disconnect(connection_id)` | 注销连接，清理会话映射 | `@OnClose` |
| `bind_session(conn_id, session_id)` | 绑定连接到会话 | `session.getUserProperties().put()` |
| `send_message(conn_id, msg)` | 发给单个连接 | `session.getBasicRemote().sendText()` |
| `send_to_session(session_id, msg)` | 发给同一会话的所有连接 | `@SendTo("/topic/session")` |
| `broadcast(msg)` | 发给所有连接 | `SimpMessagingTemplate.convertAndSend("/topic/all")` |

注意 `_lock` 的存在——虽然 Python 协程不会真正并行，但 `async with self._lock` 防止了多个协程交错修改字典时的不一致问题。Java 中类似 `ConcurrentHashMap` 或 `synchronized` 块。

### （3）发送工具函数

connection.py 还提供了一组便捷函数，封装了"构造消息模型 + 调用 send_to_session"的重复逻辑：

```python
async def send_message_chunk(session_id, content)    # 发流式文本
async def send_tool_call(session_id, tool, arguments) # 发工具调用通知
async def send_tool_result(session_id, tool, result)   # 发工具结果
async def send_message_complete(session_id, message_id) # 发完成信号
async def send_error(session_id, message, code)         # 发错误
```

这些函数被 events.py、streaming.py、tool_notifications.py 广泛使用，是统一的消息发送出口。

### （4）handle_websocket() 入口函数

```python
async def handle_websocket(websocket, agent_loop=None):
    connection_id = await connection_manager.connect(websocket)  # 注册连接
    await connection_manager.send_message(                       # 发送 connected 消息
        connection_id, ServerMessage(type="connected"))

    if agent_loop:
        await websocket_event_loop(websocket, connection_id, agent_loop)  # 进入事件循环
    else:
        # 简单回显模式（用于测试）
```

这个函数是 `app.py` 的 `websocket_endpoint()` 调用的入口。它做两件事：注册连接，然后把控制权交给 events.py 的事件循环。

## 4、events.py — 业务调度层

核心文件：`backend/ws/events.py`

这是业务逻辑的核心，负责"收到消息后做什么"。

### （1）websocket_event_loop() — 消息接收循环

```python
async def websocket_event_loop(websocket, connection_id, agent_loop):
    while True:
        data = await websocket.receive_text()     # 阻塞等消息
        message_dict = json.loads(data)            # 解析 JSON
        event_type = message_dict.get("type")      # 取出消息类型

        async for db in get_db():                  # 获取数据库会话
            await route_event(connection_id, event_type, message_dict, agent_loop, db)
```

这就是之前讨论过的 `while True` 循环。它只做一件事：收消息 → 解析 → 交给 `route_event` 分发。

### （2）route_event() — 事件路由

```python
async def route_event(connection_id, event_type, event_data, agent_loop, db):
    if event_type == "message":       → handle_message_event()    # 用户发聊天消息
    elif event_type == "tool_execute": → handle_tool_execution()  # 前端请求执行工具
    elif event_type == "ping":         → handle_ping_event()      # 心跳检测
    elif event_type == "subscribe":    → handle_subscribe_event() # 订阅会话
    elif event_type == "unsubscribe":  → handle_unsubscribe_event() # 取消订阅
    else:                              → send_error()             # 未知类型
```

Java 类比：类似于 Spring MVC 的 `DispatcherServlet`，根据请求类型分发到不同的 Handler。

### （3）handle_message_event() — 核心消息处理

这是最重要的 handler，处理用户发来的聊天消息。流程如下：

```
handle_message_event(connection_id, message, agent_loop, db)
    │
    ├── 1. 获取取消令牌（用于用户中断生成）
    │
    ├── 2. 验证会话存在性
    │       └── session_manager.get_session(session_id)
    │
    ├── 3. 保存用户消息到数据库
    │       └── session_manager.add_message(role="user", content=...)
    │
    ├── 4. 加载历史消息（最近 50 条）
    │       └── session_manager.get_messages(limit=50)
    │
    ├── 5. 创建 BufferedStreamingHandler（缓冲 10 字符 / 10ms）
    │
    ├── 6. 调用 AgentLoop 处理，流式输出
    │       async for chunk in agent_loop.process_message(...):
    │           if cancel_token.is_cancelled: break
    │           assistant_content += chunk
    │           await streaming_handler.write(chunk)    ← 写入缓冲区
    │
    ├── 7. flush 剩余缓冲内容
    │
    ├── 8. 保存 AI 回复到数据库
    │       └── session_manager.add_message(role="assistant", content=...)
    │
    ├── 9. 回填工具调用记录的 message_id
    │
    └── 10. 发送 message_complete 通知
```

### （4）错误友好化

```python
def _friendly_processing_error(raw: str) -> str:
    if "429" or "quota" in raw:    → "AI 服务配额不足，请检查 API 账户余额。"
    if "401" or "unauthorized":     → "API 认证失败，请检查密钥配置。"
    if "timeout" or "network":      → "网络连接异常，请稍后重试。"
    else:                           → "消息处理出错，请稍后重试。"
```

把 LLM API 返回的技术性错误（如 `429 Too Many Requests`）转换为用户看得懂的中文提示。

## 5、streaming.py — 发送优化层

核心文件：`backend/ws/streaming.py`

### （1）为什么需要缓冲

LLM 是逐字返回的，如果每个字都发一次 WebSocket 消息：

```
LLM 返回 "你" → send_message_chunk("你")     ← 1 次网络发送
LLM 返回 "好" → send_message_chunk("好")     ← 1 次网络发送
LLM 返回 "！" → send_message_chunk("！")     ← 1 次网络发送
...
10 个字 = 10 次 WebSocket 发送，太浪费
```

有了 `BufferedStreamingHandler`：

```
LLM 返回 "你" → buffer = "你"         （攒着）
LLM 返回 "好" → buffer = "你好"       （攒着）
LLM 返回 "！" → buffer = "你好！"     （攒着）
...
buffer 达到 10 个字符 → send_message_chunk("你好！有什么可以帮你")  ← 1 次发送
```

### （2）两个触发 flush 的条件

```python
async def write(self, text: str):
    self.buffer += text

    if (
        len(self.buffer) >= self.buffer_size       # 条件1：攒够 10 个字符
        or time_since_flush >= self.flush_interval_ms  # 条件2：距上次发送超过 10ms
    ):
        await self.flush()
```

两个条件取**先到的那个**：
- 如果 LLM 输出很快，字符很快攒够 10 个 → 按字符数触发
- 如果 LLM 输出很慢，10ms 内只来了 2 个字 → 按时间触发（不让用户等太久）

Java 类比：类似 `BufferedOutputStream` 的 flush 策略，只不过加了时间维度。

### （3）两个 Handler 的区别

| 类 | 适用场景 | 策略 |
|----|----------|------|
| `StreamingResponseHandler` | 已有完整文本需要分块发送 | 按固定 chunk_size 切割 + 可选延迟 |
| `BufferedStreamingHandler` | 实时流式场景（LLM 逐字返回） | 攒 buffer_size 个字符或 flush_interval_ms 后发送 |

项目中 `handle_message_event()` 使用的是 `BufferedStreamingHandler`，因为 LLM 的输出是逐字到达的。

## 6、tool_notifications.py — 工具通知层

核心文件：`backend/ws/tool_notifications.py`

### （1）工具执行的生命周期通知

当 AI 决定调用工具时，前端需要知道工具的执行状态。`ToolNotificationHandler` 管理单个工具的完整通知生命周期：

```
工具执行开始
    │
    ├── notify_start()     → 发 ToolStartMessage（工具名 + 参数 + 时间戳）
    │
    ├── notify_progress()  → 发 ToolProgressMessage（进度 0-100%，可选）
    │
    ├── 执行成功：
    │   └── notify_complete() → 发 ToolCompleteMessage（结果 + 耗时）
    │
    └── 执行失败：
        └── notify_error()    → 发 ToolErrorMessage（错误信息 + 耗时）
```

### （2）execute_tool_with_notifications() — 核心包装函数

```python
async def execute_tool_with_notifications(session_id, tool_name, arguments, executor):
    handler = ToolNotificationHandler(session_id, tool_name)

    try:
        await handler.notify_start(arguments)      # 通知前端：开始了
        result = await executor(tool_name, arguments)  # 实际执行工具
        await handler.notify_complete(result)       # 通知前端：完成了
        return result
    except Exception as e:
        await handler.notify_error(str(e))          # 通知前端：出错了
        raise
```

这是一个典型的**装饰器/包装器模式**：在实际工具执行的前后插入通知逻辑。Java 类比：类似 AOP 的 `@Around` 切面，在方法执行前后做额外操作。

### （3）BatchToolNotificationHandler — 批量工具通知

当 AI 同时调用多个工具时，用 `BatchToolNotificationHandler` 管理：

```python
batch = BatchToolNotificationHandler(session_id)
handler_a = batch.create_handler("web_fetch")    # 为每个工具创建独立 handler
handler_b = batch.create_handler("read_file")
# 各自独立通知开始/完成
await batch.notify_batch_complete()               # 所有工具完成后统一通知
```

## 7、完整调用链路

用户发送 "查天气" 后的完整数据流：

```
① app.py websocket_endpoint()
   └── 认证通过，创建 AgentLoop

② connection.py handle_websocket()
   ├── connection_manager.connect(websocket)     → 注册连接，分配 conn-id
   ├── send_message({type: "connected"})         → 告诉前端连接成功
   └── 调用 events.py websocket_event_loop()

③ events.py websocket_event_loop()
   └── await websocket.receive_text()            → 收到 {"type":"message","content":"查天气"}

④ events.py route_event()
   └── type == "message" → handle_message_event()

⑤ events.py handle_message_event()
   ├── 保存用户消息到数据库
   ├── 加载历史消息
   ├── 创建 BufferedStreamingHandler(buffer_size=10, flush_interval_ms=10)
   │
   ├── async for chunk in agent_loop.process_message():
   │   │
   │   ├── chunk = "好的"
   │   │   └── streaming.py write("好的") → buffer = "好的"（未满，攒着）
   │   │
   │   ├── chunk = "，我来查一下"
   │   │   └── streaming.py write("，我来查一下") → buffer 超 10 字符
   │   │       └── flush() → connection.py send_message_chunk("好的，我来查一下")
   │   │                       └── connection_manager.send_to_session()
   │   │                           └── websocket.send_text(JSON) → 前端收到
   │   │
   │   ├── [AgentLoop 内部决定调用工具]
   │   │   └── tool_notifications.py execute_tool_with_notifications()
   │   │       ├── notify_start({tool: "web_fetch", args: ...})
   │   │       │   └── connection.py send_to_session() → 前端显示 "正在查询天气..."
   │   │       ├── 执行 web_fetch 工具 → 返回 "晴天 25°C"
   │   │       └── notify_complete("晴天 25°C")
   │   │           └── connection.py send_to_session() → 前端显示工具结果
   │   │
   │   └── chunk = "今天天气晴朗，温度25°C"
   │       └── streaming.py → flush → connection.py → 前端追加文字
   │
   ├── streaming_handler.flush()                  → 发送缓冲区剩余内容
   ├── 保存 AI 回复到数据库
   └── send_message_complete()                    → 前端停止打字动画
```

## 8、关键设计要点

### （1）分层解耦

每个文件职责单一：connection.py 不关心业务逻辑，events.py 不关心发送优化，streaming.py 不关心事件路由。修改缓冲策略只需改 streaming.py，不影响其他文件。

### （2）会话级消息推送

通过 `session_id → {connection_id}` 的映射，实现了"同一用户开多个标签页，都能收到 AI 回复"的效果。Java 中需要用 Spring 的 `@SendTo("/topic/session-{id}")` 实现类似功能。

### （3）取消令牌贯穿全链路

取消令牌在 connection.py 中管理（`get_cancel_token` / `cancel_session`），在 events.py 中检查（`cancel_token.is_cancelled`），在 AgentLoop 中也检查。用户点击"停止生成"时，令牌被标记为已取消，整条链路都能及时响应。

### （4）错误不会打断连接

WebSocket 连接是长连接，一次处理出错不应该断开连接。events.py 中所有 handler 都有 try-except，出错时通过 `send_error()` 告知前端，但连接保持不变，用户可以继续发消息。

## 9、对应代码阅读指引

| 阅读顺序 | 文件 | 重点关注 |
|----------|------|----------|
| 1 | `ws/connection.py` 消息模型部分 | ClientMessage、ServerMessage 及其子类的字段定义 |
| 2 | `ws/connection.py` ConnectionManager | `_connections` 和 `_session_connections` 两个字典的关系 |
| 3 | `ws/connection.py` handle_websocket | 连接入口，如何交给 events.py |
| 4 | `ws/events.py` websocket_event_loop | while True 循环 + JSON 解析 + 事件路由 |
| 5 | `ws/events.py` handle_message_event | 最核心：数据库操作 + AgentLoop 调用 + 流式输出 |
| 6 | `ws/streaming.py` BufferedStreamingHandler | `write()` 的缓冲判断逻辑和 `flush()` 的发送逻辑 |
| 7 | `ws/tool_notifications.py` execute_tool_with_notifications | 工具执行的 AOP 式包装 |
