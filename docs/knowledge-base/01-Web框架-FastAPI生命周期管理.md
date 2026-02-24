# FastAPI Lifespan 生命周期管理

## 1、什么是 Lifespan

Lifespan 是 FastAPI 提供的一种机制，用于在**应用启动时初始化资源**、在**应用关闭时清理资源**。

## 2、基本用法

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ===== 应用启动时执行（yield 之前）=====
    print("应用启动，初始化资源...")
    db = await connect_database()

    yield  # 应用在此处运行，处理请求

    # ===== 应用关闭时执行（yield 之后）=====
    print("应用关闭，清理资源...")
    await db.close()

app = FastAPI(lifespan=lifespan)
```

## 3、执行流程

```
应用启动
    ↓
执行 yield 之前的代码（初始化数据库、加载配置等）
    ↓
yield —— 应用正常运行，处理 HTTP 请求
    ↓
执行 yield 之后的代码（关闭连接、释放资源等）
    ↓
应用退出
```

## 4、@asynccontextmanager 装饰器

`@asynccontextmanager` 来自 Python 标准库 `contextlib`，作用是把一个**异步生成器函数**转换为**异步上下文管理器**（可以用 `async with` 的对象）。

没有这个装饰器的话，你需要写一个类来实现 `__aenter__` 和 `__aexit__` 方法，代码会更冗长。

### （1）对比

不用装饰器（繁琐写法）：

```python
class Lifespan:
    async def __aenter__(self):
        # 启动逻辑
        pass

    async def __aexit__(self, *args):
        # 关闭逻辑
        pass
```

用装饰器（简洁写法）：

```python
@asynccontextmanager
async def lifespan(app):
    # 启动逻辑
    yield
    # 关闭逻辑
```

## 5、本项目中的用法

在 `backend/app.py` 中，lifespan 函数做了以下事情：

**启动时（yield 之前）：**
- 初始化数据库
- 加载配置文件
- 创建共享组件（消息队列、限流器等）
- 注册工具、启动频道管理器、启动定时任务调度器

**关闭时（yield 之后）：**
- 停止所有频道
- 停止调度器
- 输出关闭日志

## 6、yield 之后的代码何时执行

yield 之后的代码（关闭逻辑）在应用收到**正常关闭信号**时执行：

```
Ctrl+C / kill / docker stop
    ↓
Uvicorn 收到 SIGTERM 或 SIGINT 信号
    ↓
Uvicorn 通知 FastAPI："要关闭了"
    ↓
FastAPI 从 yield 处恢复，执行 yield 之后的代码
    ↓
await channel_manager.stop_all()
await scheduler.stop()
    ↓
Uvicorn 关闭 HTTP 服务
```

### （1）不同关闭方式的对比

| 触发方式 | yield 之后执行？ | 说明 |
|----------|:-:|------|
| 终端 `Ctrl+C`（SIGINT） | 是 | 最常见的开发期关闭方式 |
| `kill <pid>`（SIGTERM） | 是 | 默认的 kill 信号，Uvicorn 能捕获 |
| `docker stop`（发 SIGTERM） | 是 | Docker 先发 SIGTERM，等 10 秒后才发 SIGKILL |
| `kill -9 <pid>`（SIGKILL） | **否** | 操作系统直接终结进程，程序无法捕获 |
| 进程崩溃（段错误等） | **否** | 进程已经异常退出 |

### （2）SIGTERM vs SIGKILL

- **SIGTERM**（信号 15）：礼貌地请求进程退出，进程可以捕获并做清理
- **SIGKILL**（信号 9）：操作系统强制杀死进程，进程没有任何反应机会

```bash
kill 1234          # 发送 SIGTERM → 进程有机会优雅关闭
kill -9 1234       # 发送 SIGKILL → 进程直接死亡，不执行任何清理代码
```

### （3）Java 对比

| 概念 | Python FastAPI | Java Spring Boot |
|------|----------------|------------------|
| 正常关闭时的清理 | lifespan yield 之后 | `@PreDestroy` / `DisposableBean.destroy()` |
| 兜底清理机制 | `atexit.register()` | `Runtime.addShutdownHook()` |
| 不可捕获的强制退出 | `kill -9` | `Runtime.halt()` 或 `kill -9` |

### （4）atexit 兜底机制

本项目中除了 yield 之后的关闭代码，还注册了 `atexit` 回调作为兜底：

```python
import atexit

def cleanup_on_exit():
    # 在新的事件循环中运行异步清理代码
    loop = asyncio.new_event_loop()
    loop.run_until_complete(channel_manager.stop_all())
    loop.close()

atexit.register(cleanup_on_exit)
```

正常关闭时两者都会执行（先 yield 之后的代码，再 atexit）。atexit 是为了覆盖一些边缘情况，比如 Uvicorn 自身异常导致 lifespan 关闭流程没被正确触发。

`kill -9` 时两者**都不会执行**——这是操作系统的限制，任何编程语言都无法绕过。

## 7、为什么不用 @app.on_event

旧版 FastAPI 用 `@app.on_event("startup")` 和 `@app.on_event("shutdown")`，但这种写法已被废弃。Lifespan 方式更好，因为：

1. 启动和关闭逻辑写在一个函数里，上下文更清晰
2. 可以方便地共享变量（启动时创建的对象，关闭时可以直接引用）
3. 是 FastAPI 官方推荐的方式
