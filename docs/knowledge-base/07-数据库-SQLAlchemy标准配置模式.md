# SQLAlchemy 标准配置模式

CountBot 的 `backend/database.py` 中使用了 SQLAlchemy 的标准配置模式。本文以 Q&A 形式逐一讲解这些模式，帮助理解"为什么要这样写"。

## 1、数据库连接 URL 是什么格式？

```python
DATABASE_URL = f"sqlite+aiosqlite:///{DATABASE_PATH}"
SYNC_DATABASE_URL = f"sqlite:///{DATABASE_PATH}"
```

SQLAlchemy 的连接 URL 遵循统一格式：

```
dialect+driver://username:password@host:port/database
```

各部分含义：

| 部分                | 说明         | 示例                                   |
| ----------------- | ---------- | ------------------------------------ |
| dialect           | 数据库类型      | `sqlite`、`mysql`、`postgresql`        |
| driver            | Python 驱动库 | `aiosqlite`（异步）、`pymysql`、`psycopg2` |
| username:password | 认证信息       | SQLite 不需要                           |
| host:port         | 服务器地址      | SQLite 不需要（是本地文件）                    |
| database          | 数据库名/路径    | `/path/to/countbot.db`               |

### （1）为什么有两个 URL？

```python
# 异步 URL — 用 aiosqlite 驱动
"sqlite+aiosqlite:///path/to/db"

# 同步 URL — 用 sqlite3 内置驱动（不写 driver 部分）
"sqlite:///path/to/db"
```

异步引擎需要异步驱动（`aiosqlite`），同步引擎用默认驱动（Python 内置的 `sqlite3`）。两个 URL 指向同一个数据库文件，只是访问方式不同。

### （2）三个斜杠 `///` 是什么意思？

`sqlite:///path` 中的三个斜杠：前两个是协议分隔符 `://`，第三个是绝对路径的开头 `/`。如果是相对路径，就只有两个斜杠后跟路径：`sqlite://relative/path`。

## 2、Base 基类是什么？

```python
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    """数据库模型基类"""
    pass
```

### （1）DeclarativeBase 做了什么？

`DeclarativeBase` 是 SQLAlchemy 2.0 引入的声明式基类。继承它的类会自动获得：

- **元数据注册**：`Base.metadata` 记录了所有表的结构信息
- **ORM 映射能力**：子类可以用 Python 类属性定义数据库列

```python
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String)
```

### （2）为什么 Base 类本身是空的？

`Base` 只是一个"注册中心"，它的作用是把所有继承它的模型类关联到同一个 `metadata`。后续调用 `Base.metadata.create_all()` 时，SQLAlchemy 就知道要创建哪些表。

### （3）旧版写法对比

```python
# SQLAlchemy 1.x 旧写法
from sqlalchemy.ext.declarative import declarative_base
Base = declarative_base()

# SQLAlchemy 2.0 新写法（本项目使用）
from sqlalchemy.orm import DeclarativeBase
class Base(DeclarativeBase):
    pass
```

新写法的好处是支持类型检查（mypy / pyright），IDE 补全更好。

## 3、引擎（Engine）是什么？

```python
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import create_engine

# 异步引擎
engine = create_async_engine(DATABASE_URL, echo=False, future=True)

# 同步引擎
sync_engine = create_engine(SYNC_DATABASE_URL, echo=False, future=True)
```

### （1）引擎的角色

引擎是**数据库连接的管理者**，负责：

- 维护一个**连接池**（不用每次查询都新建连接）
- 管理数据库驱动
- 提供执行 SQL 的底层接口

可以类比为"数据库连接的工厂"——你不直接操作连接，而是通过引擎来获取和管理连接。

### （2）参数说明

| 参数       | 值       | 说明                               |
| -------- | ------- | -------------------------------- |
| `echo`   | `False` | 是否在控制台打印所有 SQL 语句（调试时可设为 `True`） |
| `future` | `True`  | 使用 SQLAlchemy 2.0 风格 API（向前兼容）   |

### （3）为什么需要同步引擎和异步引擎？

项目主体是异步的（FastAPI），所以主要用异步引擎。但某些场景无法使用异步（比如在同步回调、Alembic 迁移脚本中），就需要同步引擎作为补充。两个引擎连接同一个数据库文件，只是访问方式不同。

核心区别在于**异步引擎的操作需要 `await`，同步引擎不需要**：

```python
# 异步引擎 — 所有操作都是协程，需要 await
async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)

async with AsyncSessionLocal() as session:
    result = await session.execute(select(User))
    await session.commit()

# 同步引擎 — 普通函数调用，不需要 await
with sync_engine.begin() as conn:
    Base.metadata.create_all(conn)

with SessionLocal() as session:
    result = session.execute(select(User))
    session.commit()
```

|        | 异步引擎                         | 同步引擎                   |
| ------ | ---------------------------- | ---------------------- |
| 上下文管理器 | `async with`                 | `with`                 |
| 执行查询   | `await session.execute(...)` | `session.execute(...)` |
| 提交事务   | `await session.commit()`     | `session.commit()`     |
| 使用场景   | `async def` 函数内              | 普通 `def` 函数内           |
| 等待时    | 释放控制权，不阻塞事件循环                | 阻塞当前线程                 |

## 4、会话工厂（Session Factory）是什么？

```python
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import sessionmaker

# 异步会话工厂
AsyncSessionLocal = async_sessionmaker(
    engine,                    # 绑定异步引擎，会话通过这个引擎获取数据库连接
    class_=AsyncSession,       # 指定创建 AsyncSession 类型（支持 await 的会话）
    expire_on_commit=False,    # commit 后不过期对象，避免关闭会话后访问属性报错
)

# 同步会话工厂
SessionLocal = sessionmaker(
    sync_engine,               # 绑定同步引擎
    expire_on_commit=False,    # 同上，commit 后对象属性仍可直接访问
)
```

### （1）为什么用工厂而不是直接创建会话？

工厂模式的好处是**把配置和创建分开**：

```python
# 不用工厂 — 每次都要写一堆参数
session1 = AsyncSession(engine, expire_on_commit=False)
session2 = AsyncSession(engine, expire_on_commit=False)

# 用工厂 — 配置一次，后续调用很简洁
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
session1 = AsyncSessionLocal()
session2 = AsyncSessionLocal()
```

配置只写一次，后面每次 `AsyncSessionLocal()` 就能得到一个配置好的会话实例。

### （2）Session 是什么？

Session（会话）是**与数据库交互的工作区**，负责：

- 执行查询和写入操作
- 跟踪对象的变更（脏检查）
- 管理事务（commit / rollback）

一个 Session 通常对应一次"业务操作"（比如处理一个 HTTP 请求）。

### （3）expire_on_commit=False 是什么意思？

`expire_on_commit` 控制的是：**commit 之后、Session 还活着的期间**，对象属性是否标记为"过期"。

```python
async with AsyncSessionLocal() as session:
    user = await session.get(User, 1)   # 查询，user.name = "张三"
    user.name = "李四"
    await session.commit()              # ← expire_on_commit 在这里生效

    # Session 还活着，commit 已经执行
    print(user.name)
    # expire_on_commit=True（默认） → 对象已过期，访问属性会自动查一次数据库
    # expire_on_commit=False       → 直接返回内存中的 "李四"，不查数据库

# Session 已关闭
print(user.name)
# 如果对象已过期（True）  → 尝试查数据库 → 连接已关闭 → 报错！
# 如果对象未过期（False） → 直接返回内存值 → 正常工作
```

#### （3.1）SQLAlchemy 为什么默认设为 True？

为了**数据一致性**。在同步 Web 框架中，一个请求内可能多次 commit，两次 commit 之间其他请求可能修改了同一条数据。标记过期后，下次访问会重新查询，保证拿到最新值。

#### （3.2）CountBot 为什么设为 False？

FastAPI 的异步模式中，通常 commit 后很快就关闭 Session。如果对象被标记为过期，在 Session 关闭后访问属性就会尝试用已关闭的连接查询，直接报错。设为 `False` 避免了这个问题——代价是内存中的值可能不是数据库最新的，但对于"commit 后立即关闭 Session"的使用模式来说，这个取舍是合理的。

### （4）class_=AsyncSession 是什么？

告诉工厂"创建会话时，请使用 `AsyncSession` 类型"。`AsyncSession` 是 `Session` 的异步版本，所有数据库操作都返回协程，需要 `await`。

## 5、get_db() 依赖注入是什么模式？

```python
async def get_db() -> AsyncSession:
    """获取数据库会话"""
    async with AsyncSessionLocal() as session:
        yield session
```

### （1）这个函数做了什么？

1. 创建一个异步会话（`AsyncSessionLocal()`）
2. 通过 `yield` 把会话交给调用者使用
3. 调用者用完后，`async with` 自动关闭会话

### （2）为什么用 yield 而不是 return？

这是 FastAPI 的**依赖注入**模式。用 `yield` 可以在请求处理完成后执行清理操作：

```python
async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session          # ← 请求处理期间，调用者使用这个 session
    # ← async with 退出，session 自动关闭（清理）
```

FastAPI 会自动识别带 `yield` 的依赖，在请求结束后执行 `yield` 之后的代码（这里是 `async with` 的退出清理）。

### （3）在 FastAPI 中怎么使用？

```python
from fastapi import Depends

@app.get("/users")
async def get_users(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User))
    return result.scalars().all()
```

每个请求都会自动获得一个独立的数据库会话，请求结束后自动关闭。

### （4）Depends 是什么？

`Depends` 是 FastAPI 的**依赖注入**机制。`Depends(get_db)` 告诉 FastAPI："调用这个接口之前，先执行 `get_db()` 函数，把它的返回值注入到参数中。"

整个流程：

```
用户请求 GET /users
    │
    ├── 1. FastAPI 看到 Depends(get_db)
    ├── 2. 自动调用 get_db()，创建一个数据库会话
    ├── 3. 把会话注入到 db 参数
    ├── 4. 执行 get_users() 函数体
    └── 5. 函数返回后，get_db() 中的 async with 退出，自动关闭会话
```

好处是**解耦**——`get_users` 不需要知道会话怎么创建、怎么关闭，只管用。如果以后换了数据库配置，只改 `get_db()` 就行，所有接口函数不用动。

这和 Java Spring 的 `@Autowired` 是类似的思想，只是 FastAPI 用函数参数默认值的方式实现，不需要注解和容器。

## 6、get_db_session_factory() 是做什么的？

```python
def get_db_session_factory():
    """获取数据库会话工厂"""
    return AsyncSessionLocal
```

### （1）为什么需要这个函数？

`get_db()` 通过 `yield` 提供单个会话，适合"一个请求一个会话"的场景。但有些场景需要**自己控制会话的创建**，比如 Cron 定时任务调度器——它不在 HTTP 请求上下文中，需要在任务执行时自己创建和管理会话。

```python
# Cron 调度器的用法
factory = get_db_session_factory()  # 拿到 AsyncSessionLocal（工厂对象）

async def run_cron_job():
    # factory() → 调用工厂，返回一个 AsyncSession 实例
    # async with  → 对这个实例使用上下文管理器（退出时自动关闭）
    async with factory() as session:
        # 执行定时任务...
        await session.commit()
```

注意 `factory()` 有括号——是先**调用工厂**得到一个 `AsyncSession` 实例，再对实例做 `async with`，不是对工厂本身做 `async with`。

和 `get_db()` 对比：

```python
# get_db() — FastAPI 自动调用，自动管理会话生命周期
async def get_users(db: AsyncSession = Depends(get_db)):
    await db.execute(...)

# get_db_session_factory() — 拿到工厂，自己决定何时创建、何时关闭
factory = get_db_session_factory()
async with factory() as session:
    await session.execute(...)
```

### （2）为什么不直接 import AsyncSessionLocal？

通过函数封装，可以在不改调用方代码的情况下切换实现（比如测试时替换为内存数据库的工厂）。这是一种松耦合的设计。

## 7、init_db() 建表模式是什么？

```python
async def init_db() -> None:
    """初始化数据库"""
    # 延迟导入所有模型类，import 时它们会自动注册到 Base.metadata
    from backend.models import CronJob, Message, Personality, Session, Setting, Task, ToolConversation

    # engine.begin() → 获取一个数据库连接并开启事务（正常退出自动 commit，异常自动 rollback）
    async with engine.begin() as conn:
        # create_all 是同步方法，run_sync 把它放到线程池执行，不阻塞事件循环
        await conn.run_sync(Base.metadata.create_all)

    # 建表完成后，插入内置的性格预设数据
    await init_personalities()
```

### （1）为什么在函数内部 import 模型？

这是**延迟导入**模式。模型类在被 import 时会自动注册到 `Base.metadata`。在 `init_db()` 内部导入，确保调用 `create_all()` 时所有模型已注册。

如果在文件顶部导入，可能因为循环依赖导致问题。

### （2）engine.begin() 是什么？

`engine.begin()` 开启一个数据库连接并自动管理事务：

```python
async with engine.begin() as conn:
    # 在事务中执行操作
    await conn.run_sync(Base.metadata.create_all)
# 正常退出 → 自动 commit
# 异常退出 → 自动 rollback
```

### （3）run_sync 是什么？

`Base.metadata.create_all()` 是一个同步方法（SQLAlchemy 的元数据 API 不支持异步）。`conn.run_sync()` 的作用是在异步上下文中安全地调用同步方法：

```python
# 不能这样写（create_all 是同步的，会阻塞事件循环）
Base.metadata.create_all(conn)

# 正确写法 — run_sync 把同步调用放到线程池中执行
await conn.run_sync(Base.metadata.create_all)
```

`run_sync` 内部的执行流程：

```
await conn.run_sync(Base.metadata.create_all)

1. run_sync 收到同步函数 create_all
2. 把 create_all 提交到线程池执行（内部调用 loop.run_in_executor）
3. 返回一个协程
4. await 这个协程 → 当前协程挂起，事件循环去处理其他任务
5. 线程池中 create_all 执行完毕 → 协程恢复，继续往下走
```

虽然 `await` 通常用于等待协程，但这里等待的本质是"线程池中的同步任务完成"。`run_sync` 把同步操作包装成了协程的形式，让调用者可以用统一的 `await` 语法来等待。这是异步编程中处理"不得不调用同步代码"的标准做法。

## 8、init_personalities() 数据初始化模式

```python
async def init_personalities() -> None:
    """初始化内置性格数据（如果表为空）"""
    from backend.models.personality import Personality  # 延迟导入，避免循环依赖
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:         # 自己创建会话（不在 HTTP 请求中）
        try:
            # 先查一下表里有没有数据
            result = await session.execute(select(Personality))
            existing = result.scalars().first()

            if existing:
                return  # 已有数据，跳过，避免重复插入

            # 从预设配置中导入内置性格数据
            from backend.modules.agent.personalities import PERSONALITY_PRESETS

            # 逐条创建 ORM 对象并加入会话
            for pid, data in PERSONALITY_PRESETS.items():
                personality = Personality(
                    id=pid,
                    name=data["name"],
                    description=data["description"],
                    # ...其他字段...
                )
                session.add(personality)       # 添加到会话（暂未写入数据库）

            await session.commit()             # 一次性提交所有新增记录

        except Exception:
            await session.rollback()           # 出错时回滚，撤销所有未提交的变更
            pass                               # 静默失败，不影响应用启动
```

### （1）这个函数体现了哪些 SQLAlchemy 模式？

**查询模式** — `select` + `execute` + `scalars`：

```python
result = await session.execute(select(Personality))  # 执行 SELECT 查询
existing = result.scalars().first()                   # 取第一条结果
```

- `select(Personality)` → 构造 SQL：`SELECT * FROM personality`
- `session.execute()` → 执行查询，返回 `Result` 对象
- `.scalars()` → 把结果从 `Row` 元组转为 ORM 对象
- `.first()` → 取第一条，没有则返回 `None`

**写入模式** — `add` + `commit`：

```python
session.add(personality)    # 告诉 Session "我要新增这条记录"
await session.commit()      # 真正写入数据库
```

`session.add()` 只是把对象放入 Session 的"待处理队列"，不会立即执行 SQL。`commit()` 时才会一次性生成 `INSERT` 语句并写入数据库。

**错误处理模式** — `try` + `rollback`：

```python
try:
    # 执行多步数据库操作...
    await session.commit()
except Exception:
    await session.rollback()   # 出错时撤销所有变更，保证数据一致性
```

`rollback()` 会撤销本次事务中所有未 commit 的变更，防止"插了一半数据"的情况。

### （2）为什么用 AsyncSessionLocal() 而不是 get_db()？

`init_personalities()` 在应用启动时调用（`lifespan` 阶段），此时还没有 HTTP 请求，不在 FastAPI 的依赖注入上下文中。所以直接用 `AsyncSessionLocal()` 自己创建会话，和前面 `get_db_session_factory()` 的使用场景类似。

### （3）Personality 模型定义解读

`init_personalities()` 操作的就是 `Personality` 模型，看看它的定义：

```python
class Personality(Base):                    # 继承 Base，自动注册到 metadata
    """性格表"""
    __tablename__ = "personalities"          # 对应数据库中的表名

    # Mapped[str] — 类型注解，告诉 IDE 和类型检查器这个字段是 str 类型
    # mapped_column(...) — 定义数据库列的具体约束

    id: Mapped[str] = mapped_column(
        String(50),          # SQL 类型：VARCHAR(50)
        primary_key=True     # 主键
    )
    name: Mapped[str] = mapped_column(
        String(100),         # VARCHAR(100)
        nullable=False       # NOT NULL，不允许为空
    )
    description: Mapped[str] = mapped_column(
        Text,                # TEXT 类型，不限长度
        nullable=False
    )
    traits: Mapped[List[str]] = mapped_column(
        JSON,                # JSON 类型，存储列表 ["暴躁", "嘴硬心软"]
        nullable=False
    )
    icon: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="Smile"      # 默认值：不传时自动填入 "Smile"
    )
    is_builtin: Mapped[bool] = mapped_column(
        Boolean,             # BOOLEAN 类型
        nullable=False,
        default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow    # 创建时自动填入当前时间
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow   # 每次更新记录时自动刷新时间
    )
```

这是 SQLAlchemy 2.0 的声明式模型写法。`Mapped[str]` 是 Python 类型注解，`mapped_column(String(50))` 是数据库列定义，两者配合让代码同时具备**类型检查**和**数据库映射**能力。

### （4）为什么静默失败？

性格预设数据是"有了更好，没有也不影响核心功能"的数据。如果初始化失败（比如数据格式有误），不应该阻止整个应用启动。所以用 `except Exception: pass` 吞掉异常。

## 9、整体架构图

```
database.py 的对象关系：

┌─────────────────────────────────────────────┐
│              数据库文件                        │
│          countbot.db                         │
└──────────┬────────────────────┬──────────────┘
           │                    │
    ┌──────┴──────┐      ┌─────┴──────┐
    │  异步引擎    │      │  同步引擎   │
    │  engine     │      │  sync_engine│
    └──────┬──────┘      └─────┬──────┘
           │                    │
    ┌──────┴──────────┐  ┌─────┴──────────┐
    │ AsyncSessionLocal│  │  SessionLocal  │
    │  (异步会话工厂)  │  │  (同步会话工厂) │
    └──────┬──────────┘  └────────────────┘
           │
    ┌──────┴──────────────────────────┐
    │                                  │
    ▼                                  ▼
get_db()                    get_db_session_factory()
(FastAPI 依赖注入)           (Cron 等自管理场景)
```

## 9、常见 Q&A

### （1）能不能只用异步引擎，不要同步引擎？

理论上可以，但实际中有些场景必须用同步：
- Alembic 数据库迁移默认是同步的
- 某些第三方库只支持同步调用
- `run_sync` 桥接虽然方便，但不是所有操作都能桥接

保留同步引擎是一种务实的做法。

### （2）Base 可以定义公共字段吗？

可以。比如让所有表都有 `created_at` 和 `updated_at`：

```python
class Base(DeclarativeBase):
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())
```

本项目保持 `Base` 为空，各模型各自定义字段。

### （3）Session 和 Connection 有什么区别？

- **Connection**：底层数据库连接，直接执行 SQL
- **Session**：高层 ORM 接口，跟踪对象状态，管理事务

日常开发中，绑大多数情况使用 Session。只有在需要直接执行 DDL（如建表）或原始 SQL 时才用 Connection。
