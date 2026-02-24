# Web 框架：FastAPI 中的 WebSocket

本文讲解 WebSocket 的基本概念和在 FastAPI 中的用法，帮助理解 CountBot 的实时聊天实现。

## 1、WebSocket 是什么

### （1）HTTP 的局限

普通 HTTP 是"一问一答"模式：客户端发请求，服务端返回响应，连接就断了。

```
HTTP 模式（短连接）：
  客户端: "现在几点？"  →  服务端: "10:30"  →  连接断开
  客户端: "天气如何？"  →  服务端: "晴天"   →  连接断开
  （每次都要重新建立连接）
```

这种模式有个问题：**服务端无法主动给客户端发消息**。如果 AI 生成回复需要 10 秒，用户只能干等着，不能看到逐字输出的"打字机效果"。

### （2）WebSocket 的作用

WebSocket 是一种**全双工长连接**协议：连接建立后一直保持，双方随时可以互发消息。

```
WebSocket 模式（长连接）：
  客户端 ←——建立连接——→ 服务端
     │                      │
     ├─ "你好"  ──────────→ │
     │                      ├─ "你"      ──→ 客户端
     │                      ├─ "好"      ──→ 客户端
     │                      ├─ "！"      ──→ 客户端
     │                      ├─ "有什么"  ──→ 客户端
     │                      ├─ "可以帮"  ──→ 客户端
     │                      └─ "你的？"  ──→ 客户端
     │                      │
     ├─ "查天气" ──────────→ │
     │                      ├─ [工具调用通知] ──→ 客户端
     │                      ├─ [工具结果通知] ──→ 客户端
     │                      ├─ "今天晴天" ─────→ 客户端
     ...（连接一直保持）...
```

### （3）与 Java 对比

| 概念           | Java                                                 | Python FastAPI                        |
| ------------ | ---------------------------------------------------- | ------------------------------------- |
| WebSocket 端点 | `@ServerEndpoint("/ws")` 或 Spring `WebSocketHandler` | `@app.websocket("/ws")`               |
| 接收消息         | `@OnMessage` 回调                                      | `await websocket.receive_text()`      |
| 发送消息         | `session.getBasicRemote().sendText()`                | `await websocket.send_text()`         |
| 连接建立         | `@OnOpen`                                            | `await websocket.accept()`            |
| 连接关闭         | `@OnClose`                                           | 函数 return 或 `await websocket.close()` |

## 2、FastAPI WebSocket 基本用法

### （1）最简示例

```python
from fastapi import FastAPI, WebSocket

app = FastAPI()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()              # 1. 接受连接（握手）

    while True:                            # 2. 消息循环
        data = await websocket.receive_text()   # 3. 等待客户端发消息
        await websocket.send_text(f"收到: {data}")  # 4. 给客户端发消息
```

关键点：
- `@app.websocket("/ws")` — 声明 WebSocket 端点（不是 `@app.get`）
- `await websocket.accept()` — 必须先接受连接，否则握手失败
- `while True` — 消息循环，函数不 return 连接就一直活着
- `receive_text()` — 阻塞等待，直到客户端发来消息

### （2）连接的生命周期

```python
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # ===== 连接建立阶段 =====
    await websocket.accept()
    print("客户端已连接")

    try:
        # ===== 消息通信阶段 =====
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f"回复: {data}")

    except WebSocketDisconnect:
        # ===== 连接断开阶段 =====
        print("客户端断开了连接")
```

对应的时序：

```
客户端                         服务端
  │                              │
  ├── HTTP 升级请求 ───────────→ │
  │                              ├── accept() 接受握手
  │ ←── 101 Switching Protocol ──┤
  │                              │
  ├── "你好" ──────────────────→ │  ← receive_text() 返回
  │ ←──────────── "回复: 你好" ──┤  ← send_text() 发送
  │                              │
  ├── "再见" ──────────────────→ │  ← receive_text() 返回
  │ ←──────────── "回复: 再见" ──┤  ← send_text() 发送
  │                              │
  ├── 关闭页面 ────────────────→ │  ← receive_text() 抛 WebSocketDisconnect
  │                              │
```

### （3）发送 JSON 数据

实际项目中通常发送 JSON 而不是纯文本：

```python
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    while True:
        # 接收 JSON
        data = await websocket.receive_json()
        # data 自动从 JSON 字符串解析为 Python dict
        # 例如：{"type": "message", "content": "你好"}

        # 发送 JSON
        await websocket.send_json({
            "type": "reply",
            "content": f"收到: {data['content']}"
        })
```

### （4）主动关闭连接

```python
# 服务端主动关闭
await websocket.close(code=1000, reason="正常关闭")

# 常用关闭码
# 1000 — 正常关闭
# 1008 — 违反策略（如认证失败）
# 4001 — 自定义码（本项目中表示"需要认证"）
```

## 3、WebSocket vs HTTP API

| 维度 | HTTP API | WebSocket |
|------|----------|-----------|
| 连接方式 | 每次请求新建连接 | 一次连接持续使用 |
| 通信方向 | 客户端 → 服务端（单向请求） | 双方随时互发（全双工） |
| 服务端主动推送 | 不支持（需轮询） | 原生支持 |
| 适用场景 | CRUD、表单提交、数据查询 | 实时聊天、流式输出、通知推送 |
| 路由装饰器 | `@app.get` / `@app.post` | `@app.websocket` |

CountBot 中的分工：
- **HTTP API**：设置页面、历史消息查询、工具管理等（`/api/settings`、`/api/memory`）
- **WebSocket**：实时聊天（`/ws/chat`）— 需要流式输出和工具调用通知

## 4、CountBot 中的 WebSocket 消息协议

### （1）消息格式约定

CountBot 的 WebSocket 通信使用 JSON 格式，通过 `type` 字段区分不同消息类型。

### （2）客户端 → 服务端

```json
{
    "type": "message",
    "sessionId": "会话ID",
    "content": "用户输入的文本"
}
```

| type | 说明 |
|------|------|
| `message` | 用户发送聊天消息 |
| `ping` | 心跳检测（保持连接活跃） |
| `subscribe` | 订阅某个会话的通知 |
| `unsubscribe` | 取消订阅 |

### （3）服务端 → 客户端

服务端发送多种类型的消息：

```
用户发送 "查天气" 后，服务端的推送顺序：

① message_chunk:  "好的"
② message_chunk:  "，我来"
③ message_chunk:  "查一下"          ← 流式文本（打字机效果）
④ tool_call:      web_fetch(...)    ← AI 决定调用工具
⑤ tool_result:    "晴天 25°C"       ← 工具执行完成
⑥ message_chunk:  "今天天气"
⑦ message_chunk:  "晴朗，25°C"      ← 继续流式文本
⑧ message_complete: {messageId}     ← 本轮回复结束
```

| type               | 说明         | 对应 Pydantic 模型    |
| ------------------ | ---------- | ----------------- |
| `connected`        | 连接成功       | `ServerMessage`   |
| `message_chunk`    | 流式文本片段     | `MessageChunk`    |
| `tool_call`        | AI 调用了某个工具 | `ToolCall`        |
| `tool_result`      | 工具执行结果     | `ToolResult`      |
| `message_complete` | 回复结束       | `MessageComplete` |
| `error`            | 错误信息       | `ErrorMessage`    |

### （4）为什么要分这么多类型？

前端根据 `type` 做不同的 UI 展示：
- `message_chunk` → 逐字追加到聊天气泡中（打字机效果）
- `tool_call` → 显示"正在执行：读取文件..."
- `tool_result` → 显示工具执行结果和耗时
- `message_complete` → 停止打字动画，显示完整消息
- `error` → 显示红色错误提示

## 5、流式输出的实现原理

### （1）为什么需要流式输出

大模型生成文本是逐字产出的，如果等全部生成完再返回，用户可能要等 10 秒。流式输出让用户看到"AI 正在打字"的效果，体验更好。

### （2）数据流转路径

```
LLM API（逐字返回）
    ↓ async for chunk
AgentLoop.process_message()（异步生成器，yield 每个 chunk）
    ↓ async for chunk
BufferedStreamingHandler.write()（缓冲 10 个字符或 10ms 后发送）
    ↓ flush
ConnectionManager.send_to_session()（发送 JSON 到 WebSocket）
    ↓
前端浏览器（追加到聊天气泡）
```

### （3）缓冲策略

直接每个字符都发一次 WebSocket 消息太浪费带宽，所以用了 `BufferedStreamingHandler`：

```python
streaming_handler = BufferedStreamingHandler(
    session_id=session_id,
    buffer_size=10,         # 累积 10 个字符后发送
    flush_interval_ms=10,   # 或者每 10ms 发送一次（取先到的条件）
)
```

效果：
- LLM 返回 "你" "好" "！" "有" "什" "么" "可" "以" "帮" "你" → 缓冲后一次发 "你好！有什么可以帮你"
- 避免了 10 次 WebSocket 发送，减少网络开销

## 6、WebSocket 认证

### （1）为什么要单独做认证

HTTP 请求的认证由中间件统一处理，但 **WebSocket 升级请求不经过 HTTP 中间件**，所以需要在 WebSocket 端点内部手动做认证。

### （2）认证流程

```
WebSocket 连接请求
    ↓
获取客户端 TCP 层 IP
    ↓
检测是否有反向代理头（x-forwarded-for 等）
    ↓
    ├── 本地 IP + 无代理头 → 直接放行（本地开发环境）
    └── 远程 IP 或有代理头 → 需要验证 token
                                ↓
                            从 URL 参数或 Cookie 获取 token
                                ↓
                            验证失败 → close(4001) 关闭连接
                            验证成功 → 继续
```

### （3）前端怎么传 token

```javascript
// 方式 1：URL 查询参数
const ws = new WebSocket("ws://example.com/ws/chat?token=abc123")

// 方式 2：Cookie（浏览器自动携带）
// 前端登录后 Cookie 中有 CountBot_token=abc123
// WebSocket 握手时浏览器自动带上
const ws = new WebSocket("ws://example.com/ws/chat")
```

## 7、连接管理器（ConnectionManager）

### （1）为什么需要连接管理器

一个服务端可能同时有多个 WebSocket 连接（多个用户/多个标签页），需要统一管理：

```
ConnectionManager
├── 连接 A（用户张三，会话 session-1）
├── 连接 B（用户李四，会话 session-2）
└── 连接 C（用户张三，会话 session-1）  ← 同一用户开了两个标签页
```

### （2）核心数据结构

```python
class ConnectionManager:
    _connections: dict[str, WebSocket]           # connection_id → WebSocket 对象
    _session_connections: dict[str, set[str]]    # session_id → {connection_id, ...}
```

### （3）消息发送方式

| 方法 | 作用 | 类比 |
|------|------|------|
| `send_message(connection_id, msg)` | 发给单个连接 | 私聊 |
| `send_to_session(session_id, msg)` | 发给同一会话的所有连接 | 群发给同一用户 |
| `broadcast(msg)` | 发给所有连接 | 全局广播 |

### （4）与 Java 对比

| Java (Spring WebSocket) | Python (FastAPI) |
|--------------------------|------------------|
| `WebSocketSession` | `WebSocket` 对象 |
| `SessionHandlerRegistry` | `ConnectionManager` |
| `session.sendMessage(TextMessage)` | `await websocket.send_json(data)` |
| `@SendTo("/topic/chat")` | `send_to_session(session_id, msg)` |
| `SimpMessagingTemplate.convertAndSend()` | `broadcast(msg)` |

## 8、前端 WebSocket 基本用法

浏览器原生支持 WebSocket API：

```javascript
// 1. 建立连接
const ws = new WebSocket("ws://localhost:8000/ws/chat")

// 2. 连接成功回调
ws.onopen = () => {
    console.log("已连接")
    // 发送消息
    ws.send(JSON.stringify({
        type: "message",
        sessionId: "session-123",
        content: "你好"
    }))
}

// 3. 接收消息回调
ws.onmessage = (event) => {
    const data = JSON.parse(event.data)

    switch (data.type) {
        case "message_chunk":
            // 追加文本到聊天气泡（打字机效果）
            appendText(data.content)
            break
        case "tool_call":
            // 显示"正在调用工具..."
            showToolCall(data.tool, data.arguments)
            break
        case "tool_result":
            // 显示工具执行结果
            showToolResult(data.tool, data.result)
            break
        case "message_complete":
            // 回复结束
            finishMessage()
            break
        case "error":
            // 显示错误
            showError(data.message)
            break
    }
}

// 4. 连接断开回调
ws.onclose = (event) => {
    console.log(`连接断开: code=${event.code}, reason=${event.reason}`)
}

// 5. 错误回调
ws.onerror = (error) => {
    console.error("WebSocket 错误:", error)
}
```

## 9、完整数据流总结

```
用户在浏览器输入 "查天气" 并点击发送

前端
  ├─ ws.send({"type":"message", "sessionId":"s1", "content":"查天气"})
  │
  ▼
后端 websocket_endpoint()
  ├─ 认证通过
  ├─ 创建 AgentLoop
  └─ handle_websocket() → websocket_event_loop()
      ├─ receive_text() 收到消息
      ├─ 保存用户消息到数据库
      ├─ agent_loop.process_message() 开始处理
      │   ├─ 调用 LLM → LLM 流式返回 "好的，我来查"
      │   │   └─ yield "好的" → yield "，我来查"
      │   │       └─ send_to_session: {"type":"message_chunk","content":"好的，我来查"}
      │   │
      │   ├─ LLM 决定调用 web_fetch 工具
      │   │   └─ send_to_session: {"type":"tool_call","tool":"web_fetch",...}
      │   │
      │   ├─ 执行工具，得到天气数据
      │   │   └─ send_to_session: {"type":"tool_result","tool":"web_fetch","result":"晴天25°C"}
      │   │
      │   ├─ 调用 LLM → LLM 根据工具结果生成回复
      │   │   └─ yield "今天天气晴朗" → yield "，温度25°C"
      │   │       └─ send_to_session: {"type":"message_chunk","content":"今天天气晴朗，温度25°C"}
      │   │
      │   └─ LLM 不再调用工具 → 循环结束
      │
      ├─ 保存 AI 回复到数据库
      └─ send_to_session: {"type":"message_complete","messageId":"msg-123"}

前端
  ├─ onmessage: message_chunk → 逐步显示文字
  ├─ onmessage: tool_call → 显示工具调用状态
  ├─ onmessage: tool_result → 显示工具结果
  ├─ onmessage: message_chunk → 继续显示文字
  └─ onmessage: message_complete → 回复完成，停止打字动画
```

## 10、对应代码阅读指引

| 阅读顺序 | 文件 | 重点关注 |
|----------|------|----------|
| 1 | `app.py` websocket_endpoint | WebSocket 端点入口、认证、组件创建 |
| 2 | `ws/connection.py` ConnectionManager | 连接管理、消息发送 |
| 3 | `ws/connection.py` handle_websocket | WebSocket 连接处理入口 |
| 4 | `ws/events.py` websocket_event_loop | while True 消息接收循环 |
| 5 | `ws/events.py` handle_message_event | 用户消息处理（调用 AgentLoop） |
| 6 | `ws/streaming.py` BufferedStreamingHandler | 流式输出的缓冲策略 |
| 7 | `ws/tool_notifications.py` | 工具调用/结果的 WebSocket 通知 |
