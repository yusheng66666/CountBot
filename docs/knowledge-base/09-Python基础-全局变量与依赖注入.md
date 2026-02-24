# Python 基础：全局变量与依赖注入

本文介绍 Python 中使用模块级全局变量实现"延迟注入"的常见模式，以及与 Java Spring 依赖注入的对比。

## 1、问题场景

在 Web 应用中，经常遇到这样的时序问题：

```
应用启动顺序：
  1. Python 导入模块 → 注册 API 路由（此时核心对象还未创建）
  2. lifespan() 执行 → 创建核心对象实例
  3. 用户发请求 → API 路由需要使用核心对象
```

路由定义在模块导入时就注册了，但核心对象（如 `ChannelManager`）要等到应用启动时才创建。两者时机不一致，路由函数如何拿到这个实例？

## 2、解决方案：模块级全局变量 + Setter

```python
# api/channels.py

# 先声明一个 None 的全局变量（模块导入时）
_channel_manager: ChannelManager | None = None


def set_channel_manager(manager: ChannelManager):
    """设置全局渠道管理器实例"""
    global _channel_manager        # global 关键字：声明要修改的是模块级变量
    _channel_manager = manager


@router.get("/api/channels")
async def list_channels():
    # 路由执行时（用户请求时），实例已经被 set 进来了
    return _channel_manager.list()
```

```python
# app.py lifespan() 中——应用启动时注入
channel_manager = ChannelManager(config, message_queue)
set_channel_manager(channel_manager)   # ← 将实例注入到 API 模块
```

### 时序图

```
模块导入阶段              lifespan 启动阶段              请求处理阶段
─────────────           ─────────────────           ──────────────
channels.py 被导入       ChannelManager 创建          用户发 GET /api/channels
  ↓                       ↓                           ↓
_channel_manager = None  set_channel_manager(实例)    _channel_manager.list()
@router.get 注册路由       ↓                           ↓
                        _channel_manager = 实例       返回结果
```

## 3、global 关键字详解

### （1）为什么需要 global？

Python 的变量查找规则（LEGB）：函数内部赋值默认创建**局部变量**，不会修改外部同名变量。

```python
count = 0

def increment():
    count = count + 1    # ❌ UnboundLocalError!
    # Python 看到 count = ... 赋值，认为 count 是局部变量
    # 但右边的 count + 1 又要先读取它，此时局部变量还没赋值

def increment_correct():
    global count         # ✅ 声明：我要修改的是模块级的 count
    count = count + 1    # 现在读写的都是模块级变量
```

### （2）global 只在赋值时需要

如果只是**读取**或**调用方法**，不需要 global：

```python
_manager = SomeManager()

def use_manager():
    _manager.do_something()    # ✅ 读取 + 调用方法，不需要 global
    result = _manager.query()  # ✅ 同上

def replace_manager(new_manager):
    global _manager            # ⚠️ 重新赋值才需要 global
    _manager = new_manager
```

### （3）下划线前缀的约定

```python
_channel_manager = None    # 单下划线开头 → "私有"变量
```

Python 没有真正的 private 关键字。单下划线 `_` 是一种约定，表示"这是模块内部使用的变量，外部不应该直接访问"。类似于 Java 中的 `private` 字段 + 公开的 getter/setter。

## 4、替代方案对比

### （1）方案 A：通过 request.app.state 访问

FastAPI 的 `app.state` 也可以存放共享对象：

```python
# app.py lifespan() 中
app.state.channel_manager = channel_manager

# api/channels.py 路由中
@router.get("/api/channels")
async def list_channels(request: Request):
    manager = request.app.state.channel_manager
    return manager.list()
```

**优点**：不需要全局变量，更显式
**缺点**：每个路由都要声明 `request: Request` 参数，稍显啰嗦

### （2）方案 B：FastAPI 的 Depends 依赖注入

```python
def get_channel_manager():
    """依赖提供函数"""
    if _channel_manager is None:
        raise RuntimeError("ChannelManager not initialized")
    return _channel_manager

@router.get("/api/channels")
async def list_channels(manager: ChannelManager = Depends(get_channel_manager)):
    return manager.list()
```

**优点**：更符合 FastAPI 的依赖注入风格，方便测试时 mock
**缺点**：多一层间接，简单场景下有些过度设计

### （3）方案 C：全局变量 + Setter（项目实际采用）

```python
_channel_manager = None

def set_channel_manager(manager):
    global _channel_manager
    _channel_manager = manager

@router.get("/api/channels")
async def list_channels():
    return _channel_manager.list()
```

**优点**：最简洁，路由函数签名干净
**缺点**：隐式依赖，测试时需要手动 set

### 三种方案总结

| 方案 | 简洁度 | 可测试性 | 显式程度 |
|------|--------|----------|----------|
| app.state | 中 | 好 | 最显式 |
| Depends | 中 | 最好 | 显式 |
| 全局变量 + Setter | 最简洁 | 一般 | 隐式 |

## 5、与 Java Spring 的对比

### （1）Spring 的依赖注入

在 Java Spring 中，依赖注入由 IoC 容器自动完成：

```java
@RestController
public class ChannelController {

    @Autowired
    private ChannelManager channelManager;  // Spring 自动注入，开发者不关心时机

    @GetMapping("/api/channels")
    public List<Channel> listChannels() {
        return channelManager.list();
    }
}
```

Spring 容器负责：
1. 扫描所有 `@Component` / `@Service` / `@Bean`
2. 按依赖关系排序创建顺序
3. 自动将实例注入到 `@Autowired` 字段

### （2）Python 的手动注入

Python（特别是 FastAPI）没有 Spring 那样的 IoC 容器，需要手动管理：

```python
# 相当于 Java 的 @Autowired private ChannelManager channelManager
_channel_manager = None

# 相当于 Spring 容器调用 setter 注入
def set_channel_manager(manager):
    global _channel_manager
    _channel_manager = manager
```

### （3）对照表

| Java Spring | Python (FastAPI) |
|-------------|------------------|
| `@Autowired` 字段声明 | `_variable = None` 全局变量声明 |
| Spring 容器自动注入 | `lifespan()` 中手动调用 `set_xxx()` |
| `@Bean` / `@Component` 定义 Bean | `lifespan()` 中 `new` 出实例 |
| `ApplicationContext.getBean()` | 直接访问模块级全局变量 |
| `@Qualifier` 指定注入哪个 Bean | Python 中直接指定变量名 |
| Bean 的生命周期由容器管理 | 变量的生命周期由模块（进程）管理 |

### （4）本质相同

两者的本质都是解决同一个问题：**对象的创建者和使用者不在同一个地方**。

- Java 用 IoC 容器 + 注解自动化了这个过程
- Python 用模块级变量 + setter 手动完成这个过程

Python 之所以不用框架级 IoC，是因为 Python 模块本身就是单例（首次 import 执行，后续 import 复用），配合全局变量已经能满足大部分需求。

## 6、Python 模块就是单例

理解这个模式的关键是：**Python 模块在首次 import 时执行一次，后续 import 复用同一个模块对象**。

```python
# config.py
print("模块被加载")           # 只在首次 import 时打印
_config = None

# a.py
from config import _config    # 第一次 import → 执行 config.py，打印 "模块被加载"

# b.py
from config import _config    # 第二次 import → 不再执行，复用同一个模块对象
```

所以当 `set_channel_manager()` 修改了 `_channel_manager` 时，所有 import 了这个变量的地方看到的都是同一个值——因为它们引用的是同一个模块对象中的同一个变量。

这就是 Python 中最简单的"单例模式"——不需要任何特殊设计，模块天然就是单例。
