# FastAPI 应用配置

CountBot 的 `backend/app.py` 是整个后端的入口文件。本文解释其中涉及的 Web 框架技术概念。

## 1、FastAPI 实例创建

```python
app = FastAPI(
    title="CountBot Desktop API",   # 应用名称，显示在自动生成的 API 文档页面标题上
    description="CountBot backend API",  # 应用描述，显示在 API 文档页面
    version="0.1.0",                # API 版本号
    lifespan=lifespan,              # 生命周期管理器，控制启动和关闭时的初始化/清理
)
```

### （1）这些参数有什么实际用途？

`title`、`description`、`version` 主要用于 FastAPI 自动生成的 API 文档页面。`lifespan` 控制应用启动时初始化资源、关闭时清理资源，详见 knowledge-base 01 号文档。

FastAPI 内置了两种 API 文档界面，服务启动后通过浏览器直接访问：

| 文档界面 | 访问地址 | 说明 |
|----------|----------|------|
| Swagger UI | `http://localhost:8000/docs` | 交互式文档，可以直接在页面上测试 API |
| ReDoc | `http://localhost:8000/redoc` | 阅读友好的文档，适合查阅 |

这是 FastAPI 的内置功能，不需要额外配置。端口号取决于启动时的设置，默认一般是 `8000`。

### （2）与 Java 对比

```java
// Spring Boot 的入口
@SpringBootApplication
public class Application {
    public static void main(String[] args) {
        SpringApplication.run(Application.class, args);
    }
}
```

Spring Boot 通过注解和配置文件设置应用信息，FastAPI 通过构造函数参数直接传入。

## 2、CORS 中间件

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # 允许所有来源（任何域名/端口）
    allow_credentials=True,    # 允许携带 Cookie 等认证信息
    allow_methods=["*"],       # 允许所有 HTTP 方法（GET、POST、PUT、DELETE 等）
    allow_headers=["*"],       # 允许所有请求头
)
```

### （1）什么是 CORS？

CORS（Cross-Origin Resource Sharing，跨域资源共享）是浏览器的安全机制。当前端页面从 `http://localhost:3000` 发请求到后端 `http://localhost:8000`，因为端口不同，浏览器认为是"跨域"请求，默认会拒绝。

```
前端 http://localhost:3000  ──请求──→  后端 http://localhost:8000
                                         │
                                    浏览器检查：端口不同，是跨域！
                                         │
                                    有 CORS 头？ → 允许
                                    没有 CORS 头？ → 拒绝
```

CORS 中间件的作用就是在后端的响应中加上允许跨域的 HTTP 头，告诉浏览器"这个请求我允许"。

### （2）参数说明

| 参数                  | 值       | 含义                              |
| ------------------- | ------- | ------------------------------- |
| `allow_origins`     | `["*"]` | 允许所有来源访问（`*` 表示通配）              |
| `allow_credentials` | `True`  | 允许请求携带 Cookie / Authorization 头 |
| `allow_methods`     | `["*"]` | 允许所有 HTTP 方法                    |
| `allow_headers`     | `["*"]` | 允许所有自定义请求头                      |

### （3）为什么全部设为 `*`？

这是最宽松的配置，适合开发环境和桌面应用（CountBot 是本地部署的桌面应用，前后端在同一台机器上）。生产环境的 Web 服务通常会限制 `allow_origins` 为具体的前端域名，避免被恶意网站跨域调用。

### （4）什么是中间件（Middleware）？

中间件是插在"请求进来"和"响应出去"之间的处理层，每个请求都会经过它：

```
用户请求 → CORS 中间件 → 认证中间件 → 路由处理函数 → 响应
                ↓              ↓
           添加 CORS 头    检查是否登录
```

`app.add_middleware()` 就是往这个处理链中插入一层。可以添加多个中间件，它们按注册顺序依次执行。

### （5）与 Spring Filter / Interceptor 的对比

FastAPI 的中间件和 Spring 中的 Filter（过滤器）、Interceptor（拦截器）概念相近，但定位不同：

| | FastAPI 中间件 | Spring Filter（过滤器） | Spring Interceptor（拦截器） |
|---|---|---|---|
| 层级 | Web 框架层 | Servlet 容器层 | Spring MVC 层 |
| 执行时机 | 每个请求进出都经过 | 每个请求进出都经过 | 只拦截经过 DispatcherServlet 的请求 |
| 能力 | 可修改请求和响应 | 可修改请求和响应 | 可访问 Controller 信息（Handler） |
| 典型用途 | CORS、认证、日志 | 编码转换、安全过滤 | 权限校验、日志、性能监控 |

FastAPI 的中间件更接近 Spring 的 **Filter**——都是在整个请求链路的最外层，对所有请求生效，不关心具体由哪个路由/Controller 处理。

### （6）CORS 的 Java 对比

```java
// Spring Boot 的 CORS 配置
@Configuration
public class CorsConfig implements WebMvcConfigurer {
    @Override
    public void addCorsMappings(CorsRegistry registry) {
        registry.addMapping("/**")
            .allowedOrigins("*")
            .allowedMethods("*")
            .allowedHeaders("*");
    }
}
```

本质一样，只是 FastAPI 用 `add_middleware()` 函数调用，Spring Boot 用配置类 + 注解。

## 3、认证中间件

```python
from backend.modules.auth.middleware import RemoteAuthMiddleware
from backend.modules.auth.router import get_password_hash

app.add_middleware(RemoteAuthMiddleware, get_password_hash_fn=get_password_hash)
```

这是项目自定义的中间件，用于远程访问时的认证检查。本地访问（`127.0.0.1`）不需要认证，远程访问需要验证密码。

## 4、路由注册

```python
from backend.api.chat import router as chat_router
from backend.api.settings import router as settings_router
# ... 其他路由 ...

app.include_router(chat_router)
app.include_router(settings_router)
# ... 注册其他路由 ...
```

### （1）什么是 Router？

Router 是路由的分组管理器。把相关的 API 接口定义在同一个 Router 中，再统一注册到 `app`。

```python
# backend/api/chat.py 中
from fastapi import APIRouter
router = APIRouter(prefix="/api/chat", tags=["chat"])

@router.post("/send")
async def send_message(...):
    ...

@router.get("/history")
async def get_history(...):
    ...
```

### （2）为什么要分多个 Router？

和 Java 中把不同功能的 Controller 放在不同类中是一样的道理——**职责分离**。每个 Router 文件管一类 API：

| Router | 职责 |
|--------|------|
| `chat_router` | 聊天相关 API |
| `settings_router` | 设置相关 API |
| `tools_router` | 工具相关 API |
| `cron_router` | 定时任务相关 API |
| `auth_router` | 认证相关 API |

### （3）与 Java 对比

```java
// Spring Boot 用 @RestController 分组
@RestController
@RequestMapping("/api/chat")
public class ChatController {
    @PostMapping("/send")
    public ResponseEntity<?> sendMessage(...) { ... }
}
```

FastAPI 的 `APIRouter` + `include_router` 对应 Spring Boot 的 `@RestController` + `@RequestMapping`。

## 5、WebSocket 端点

```python
from fastapi import WebSocket

@app.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    # ... 处理 WebSocket 连接 ...
    await handle_websocket(websocket, agent_loop=agent_loop)
```

### （1）什么是 WebSocket？

HTTP 是"一问一答"模式——客户端发请求，服务端返回响应，连接就断了。WebSocket 是**双向持久连接**——建立连接后，双方可以随时互发消息。

```
HTTP：
客户端 ──请求──→ 服务端
客户端 ←──响应── 服务端
（连接断开）

WebSocket：
客户端 ←──────→ 服务端   （持久连接，双方随时互发）
```

### （2）CountBot 为什么需要 WebSocket？

AI 聊天需要**流式响应**——LLM 逐字生成回复，前端需要实时展示"打字机效果"。如果用 HTTP，必须等整个回复生成完才能返回。用 WebSocket，服务端可以一边生成一边推送给前端。

```
WebSocket 流式响应：
服务端 ──"你"──→ 前端显示：你
服务端 ──"好，"──→ 前端显示：你好，
服务端 ──"我是"──→ 前端显示：你好，我是
服务端 ──"小C"──→ 前端显示：你好，我是小C
```

### （3）为什么不用 SSE（Server-Sent Events）？

SSE 也能实现流式响应，两者对比：

|       | WebSocket          | SSE                    |
| ----- | ------------------ | ---------------------- |
| 通信方向  | 双向（客户端和服务端都能主动发消息） | 单向（只有服务端推送给客户端）        |
| 协议    | 独立的 `ws://` 协议     | 基于普通 HTTP              |
| 连接管理  | 需要处理连接/断开/心跳       | 浏览器自动重连                |
| 实现复杂度 | 较高                 | 较低                     |
| 数据格式  | 二进制或文本都支持          | 仅文本（text/event-stream） |
| 适用场景  | 聊天、游戏、协作编辑         | 通知推送、日志流、LLM 流式输出      |

如果**只考虑流式输出**，SSE 完全够用（ChatGPT 网页版就用的 SSE）。但 CountBot 选择 WebSocket 是因为：

- **双向通信**：客户端可以随时发送中断指令（如"停止生成"），不需要额外开一个 HTTP 请求
- **工具执行反馈**：Agent 执行工具时，服务端需要持续推送工具调用状态、中间结果等多种类型的消息
- **会话状态管理**：WebSocket 连接本身就是一个有状态的会话，天然适合聊天场景

简单来说：SSE 适合"服务端单向推流"，WebSocket 适合"双方需要持续交互"的场景。CountBot 的 Agent 聊天属于后者。

## 6、静态文件挂载

```python
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import mimetypes

frontend_dist = APPLICATION_ROOT / "frontend" / "dist"
if frontend_dist.exists():
    # 确保 Windows 上正确识别 JavaScript 模块的 MIME 类型
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    mimetypes.add_type("image/svg+xml", ".svg")

    # SPA 路由回退（必须在 StaticFiles 之前注册）
    @app.get("/login")
    async def spa_login():
        return FileResponse(str(frontend_dist / "index.html"))

    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="static")
```

### （1）这段代码做了什么？

把前端打包后的静态文件（HTML、JS、CSS）挂载到后端服务上，这样**一个服务同时提供 API 和前端页面**：

```
http://localhost:8000/            → 返回前端 index.html
http://localhost:8000/assets/xx.js → 返回前端打包的 JS 文件
http://localhost:8000/api/chat    → 后端 API 接口
http://localhost:8000/ws/chat     → WebSocket 连接
```

这种模式在桌面应用中很常见——不需要单独启动一个前端开发服务器，后端直接托管前端打包产物。

### （2）逐行解析

#### （2.1）`frontend_dist = APPLICATION_ROOT / "frontend" / "dist"`

这里用的是 Python `pathlib` 的路径拼接语法。`/` 运算符被 `Path` 类重载了，等价于拼接路径分隔符：

```python
from pathlib import Path

# 以下两种写法等价
Path("/app") / "frontend" / "dist"   # → /app/frontend/dist
Path("/app/frontend/dist")           # → /app/frontend/dist
```

`dist` 目录是前端项目（如 Vue、React）执行 `npm run build` 后生成的打包产物目录，里面通常包含：

```
frontend/dist/
├── index.html          # 入口 HTML
├── assets/
│   ├── index-abc123.js   # 打包后的 JS（文件名含哈希，用于缓存控制）
│   ├── index-def456.css  # 打包后的 CSS
│   └── logo.svg          # 静态资源
```

#### （2.2）`if frontend_dist.exists()`

检查前端打包目录是否存在。**开发阶段**可能还没执行 `npm run build`，这时 `dist` 目录不存在，就跳过挂载，后端只提供 API 服务。这样开发时前后端可以各自独立运行。

#### （2.3）MIME 类型注册

```python
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
```

浏览器通过响应头中的 `Content-Type`（即 MIME 类型）来决定如何处理文件。如果 `.js` 文件的 Content-Type 不是 `application/javascript`，浏览器会拒绝执行。

Python 的 `mimetypes` 模块依赖操作系统的 MIME 配置。**Windows 上有时会把 `.js` 识别为 `text/plain`**，导致浏览器拒绝加载脚本。这三行代码强制注册正确的 MIME 类型，确保跨平台兼容。

```
浏览器请求 /assets/index.js
    │
    ├── Content-Type: application/javascript → 正常执行 ✓
    └── Content-Type: text/plain            → 拒绝执行 ✗（MIME type 不匹配）
```

#### （2.4）SPA 路由回退

```python
@app.get("/login")
async def spa_login():
    return FileResponse(str(frontend_dist / "index.html"))
```

SPA（Single Page Application，单页应用）的路由是**前端 JavaScript 控制**的，不是后端路由。问题在于：用户直接在浏览器地址栏输入 `http://localhost:8000/login` 时，请求先到后端，后端没有 `/login` 这个路由，会返回 404。

SPA 路由回退的做法是：把前端定义的路由路径（如 `/login`）在后端也注册一下，统一返回 `index.html`，让前端 JS 自己根据 URL 渲染对应页面。

```
用户访问 /login
    │
    ├── 后端有 /login 路由吗？ → 有（spa_login）
    ├── 返回 index.html
    ├── 浏览器加载 index.html 和 JS
    └── 前端 JS 看到 URL 是 /login → 渲染登录页面
```

注释中写的"必须在 StaticFiles 之前注册"很重要——`app.mount("/", StaticFiles(...))` 会捕获所有 `/` 开头的请求。如果 StaticFiles 先注册，`/login` 请求会被 StaticFiles 拦截，找不到 `login` 文件就返回 404，轮不到 `spa_login` 处理。

#### （2.5）StaticFiles 挂载

```python
app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="static")
```

| 参数          | 值               | 说明                                                    |
| ----------- | --------------- | ----------------------------------------------------- |
| `"/"`       | 挂载路径            | 所有 `/` 开头的请求（未被其他路由匹配的）都交给 StaticFiles                |
| `directory` | `frontend_dist` | 静态文件所在目录                                              |
| `html=True` | 启用 HTML 模式      | 访问 `/` 时自动找 `index.html`；访问 `/about` 时先找 `about.html` |
| `name`      | `"static"`      | 挂载点的名称标识，用于 URL 反向生成（见下方说明）                          |

`name` 的用途是通过名称生成 URL，而不是在代码中硬编码路径：

```python
# 通过 name 反向生成 URL
url = app.url_path_for("static", path="assets/logo.png")
# → "/assets/logo.png"
```

好处是如果以后把挂载路径从 `"/"` 改成 `"/static"`，只需要改 `mount()` 一处，所有通过 `url_path_for("static", ...)` 生成的 URL 会自动更新。实际项目中用得不多，CountBot 里也没有用到，可以理解为"给挂载点起个名字，方便以后引用"。

`app.mount()` 和 `app.include_router()` 不同——`mount` 是把一整个子应用挂到某个路径前缀下，所有匹配的请求都交给子应用处理，FastAPI 不再介入。

### （3）请求匹配优先级

因为有 API 路由、WebSocket、SPA 回退、StaticFiles 同时存在，理解匹配优先级很重要：

```
请求进入
    │
    ├── 1. 精确匹配 API 路由（/api/health、/api/chat/... 等）→ 命中就处理
    ├── 2. 精确匹配 WebSocket 路由（/ws/chat）→ 命中就处理
    ├── 3. 精确匹配 SPA 回退路由（/login）→ 返回 index.html
    └── 4. 都没命中 → 交给 StaticFiles
              ├── 找到文件（如 /assets/index.js）→ 返回文件
              ├── html=True 且路径是 / → 返回 index.html
              └── 找不到 → 返回 404
```

FastAPI 中，`@app.get()` 等装饰器注册的路由优先级高于 `app.mount()` 挂载的子应用。所以 `/api/*` 路由不会被 StaticFiles 抢走。

## 7、app.state 是什么？

代码中大量使用了 `app.state`：

```python
app.state.shared = shared
app.state.message_handler = message_handler
app.state.cron_scheduler = scheduler
```

### （1）作用

`app.state` 是 FastAPI 提供的一个全局存储对象，用于在应用的不同部分之间共享数据。在 `lifespan` 中创建的组件，通过 `app.state` 传递给路由处理函数和 WebSocket 端点。

```python
# lifespan 中存入
app.state.shared = shared

# WebSocket 端点中取出
shared = websocket.app.state.shared
```

### （2）与 Java 对比

类似于 Spring Boot 中把 Bean 注册到 IoC 容器，然后在其他地方通过 `@Autowired` 注入。`app.state` 是一种更简单直接的全局共享方式。

## 8、健康检查接口

```python
@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": "0.1.0"}
```

这是一个最简单的 API 接口，用于检测服务是否正常运行。运维工具、负载均衡器、Docker 健康检查等会定期调用这个接口，如果返回正常就认为服务健康。
