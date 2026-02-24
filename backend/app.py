"""FastAPI 应用入口"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger

from backend.utils.logger import setup_logger

setup_logger()


def _create_shared_components(config):
    """创建共享组件（WebSocket 和渠道处理器共用）

    这个函数在应用启动时调用一次，创建所有核心组件的实例。
    这些组件会通过 app.state.shared 共享给 WebSocket 端点和渠道消息处理器，
    避免每次请求都重复创建。

    参数：
        config: AppConfig 对象，从数据库加载的完整配置

    返回：
        dict，包含以下共享组件：
        - provider: LLM 调用客户端
        - workspace: 工作空间路径
        - context_builder: 上下文构建器（构造发送给 LLM 的提示词）
        - subagent_manager: 子代理管理器（处理复杂任务时拆分子任务）
        - tool_registry: 工具注册表（Agent 可调用的所有工具）
        - tool_params: 工具参数字典（创建新工具注册表时复用）
        - memory: 记忆存储（对话摘要、关键信息）
        - skills: 技能加载器（自定义技能插件）
    """
    # 延迟导入：避免模块级循环依赖，且只在实际需要时才加载
    from loguru import logger
    from backend.modules.providers.litellm_provider import LiteLLMProvider  # LLM 统一调用层（支持多家 LLM）
    from backend.modules.providers.registry import get_provider_metadata     # 获取 provider 的元信息（默认 API 地址等）
    from backend.modules.agent.context import ContextBuilder                 # 构建发送给 LLM 的上下文（系统提示词、历史消息等）
    from backend.modules.agent.memory import MemoryStore                     # 记忆存储（对话总结、关键信息）
    from backend.modules.agent.skills import SkillsLoader                    # 技能插件加载器
    from backend.modules.agent.subagent import SubagentManager               # 子代理管理器
    from backend.modules.tools.setup import register_all_tools               # 注册所有内置工具
    from backend.utils.paths import WORKSPACE_DIR                            # 默认工作空间路径

    # ===== 1. 解析当前使用的 LLM Provider 信息 =====
    logger.info("Getting provider metadata...")
    provider_id = config.model.provider                     # 当前选择的 provider，如 "zhipu"、"openai"
    provider_config = config.providers.get(provider_id)     # 该 provider 的配置（api_key、api_base 等）
    provider_meta = get_provider_metadata(provider_id)      # 该 provider 的元信息（名称、默认 API 地址等）

    # 获取 API 密钥
    api_key = provider_config.api_key if provider_config else None
    # 获取 API 地址：优先用用户配置的，没有就用 provider 的默认地址
    api_base = (
        provider_config.api_base
        if provider_config and provider_config.api_base
        else (provider_meta.default_api_base if provider_meta else None)
    )

    # ===== 2. 设置工作空间目录 =====
    logger.info("Setting up workspace...")
    # 工作空间是 Agent 执行文件操作（读写文件、执行命令等）的根目录
    if config.workspace.path:
        workspace = Path(config.workspace.path)     # 用户在配置中指定了自定义路径
    else:
        workspace = WORKSPACE_DIR                   # 使用默认路径（项目根目录）
    workspace.mkdir(parents=True, exist_ok=True)    # 确保目录存在

    # ===== 3. 创建 LLM 调用客户端 =====
    logger.info("Creating LiteLLM provider...")
    # LiteLLMProvider 封装了对各家 LLM API 的调用，统一接口
    # 底层使用 litellm 库，支持 OpenAI、智谱、Anthropic 等多种 LLM
    provider = LiteLLMProvider(
        api_key=api_key,
        api_base=api_base,
        default_model=config.model.model,           # 默认使用的模型，如 "glm-5"
        timeout=120.0,                              # 单次 API 调用超时（秒），LLM 生成较慢所以设得长
        max_retries=3,                              # API 调用失败时的重试次数
        provider_id=provider_id,
    )

    # ===== 4. 创建记忆和技能目录 =====
    logger.info("Creating memory and skills directories...")
    memory_dir = workspace / "memory"               # 记忆文件存放目录
    memory_dir.mkdir(parents=True, exist_ok=True)
    skills_dir = workspace / "skills"               # 技能插件存放目录
    skills_dir.mkdir(parents=True, exist_ok=True)

    # ===== 5. 初始化记忆和技能 =====
    logger.info("Initializing memory store...")
    memory = MemoryStore(memory_dir)                # 管理 AI 的长期记忆（对话摘要、用户偏好等）

    logger.info("Loading skills...")
    skills = SkillsLoader(skills_dir)               # 加载用户自定义的技能插件

    # ===== 6. 创建上下文构建器 =====
    logger.info("Building context builder...")
    # ContextBuilder 负责拼装发送给 LLM 的完整提示词：
    # 系统提示词 + 性格设定 + 记忆 + 技能说明 + 历史消息 + 用户输入
    context_builder = ContextBuilder(
        workspace=workspace,
        memory=memory,
        skills=skills,
        persona_config=config.persona,              # AI 人设配置（名字、性格等）
    )

    # ===== 7. 创建子代理管理器 =====
    logger.info("Creating subagent manager...")
    # SubagentManager 用于处理复杂任务时创建"子代理"
    # 主 Agent 可以将子任务委托给子代理独立执行
    subagent_manager = SubagentManager(
        provider=provider,
        workspace=workspace,
        model=config.model.model,
        temperature=config.model.temperature,
        max_tokens=config.model.max_tokens,
    )

    # ===== 8. 准备工具参数 =====
    logger.info("Preparing tool parameters...")
    # tool_params 是创建工具注册表所需的所有参数
    # 单独保存为 dict，是因为后续 WebSocket 端点和 Cron 定时任务需要用相同参数创建各自的工具注册表
    tool_params = dict(
        workspace=workspace,                                            # 工具操作的根目录
        command_timeout=config.security.command_timeout,                 # 命令执行超时
        max_output_length=config.security.max_output_length,            # 命令输出最大长度
        allow_dangerous=not config.security.dangerous_commands_blocked,  # 是否允许危险命令（取反）
        restrict_to_workspace=config.security.restrict_to_workspace,    # 是否限制在工作空间内
        custom_deny_patterns=config.security.custom_deny_patterns,      # 自定义禁止命令
        custom_allow_patterns=(                                         # 自定义允许命令（白名单模式）
            config.security.custom_allow_patterns
            if config.security.command_whitelist_enabled                 # 只有启用白名单时才传入
            else None
        ),
        audit_log_enabled=config.security.audit_log_enabled,            # 是否记录审计日志
        subagent_manager=subagent_manager,
        skills_loader=skills,
    )

    # ===== 9. 注册所有工具 =====
    logger.info("Registering all tools...")
    # register_all_tools 会创建所有内置工具（文件操作、命令执行、网络搜索等）
    # 并返回一个工具注册表（dict），key 是工具名，value 是工具实例
    tool_registry = register_all_tools(**tool_params, memory_store=memory)
    logger.info(f"Registered {len(tool_registry)} tools")

    # ===== 10. 返回所有共享组件 =====
    # 返回的 dict 会存入 app.state.shared，供 WebSocket、渠道处理器、定时任务等使用
    return dict(
        provider=provider,                  # LLM 调用客户端
        workspace=workspace,                # 工作空间路径
        context_builder=context_builder,    # 上下文构建器
        subagent_manager=subagent_manager,  # 子代理管理器
        tool_registry=tool_registry,        # 工具注册表（渠道处理器使用）
        tool_params=tool_params,            # 工具参数（WebSocket/Cron 用它创建独立的工具注册表）
        memory=memory,                      # 记忆存储
        skills=skills,                      # 技能加载器
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理

    这是 FastAPI 的 lifespan 异步上下文管理器（Async Context Manager），
    用于控制应用在启动和关闭时的行为。

    工作原理：
        - yield 之前的代码 → 应用启动时执行（初始化资源）
        - yield 之后的代码 → 应用关闭时执行（释放资源）
        - 类似于 Java Spring 的 @PostConstruct / @PreDestroy，
          或者 Spring Boot 的 ApplicationRunner + DisposableBean

    与 Java 对比：
        Java Spring Boot:                    FastAPI:
        @Bean ApplicationRunner              lifespan() yield 之前
        @PreDestroy / DisposableBean         lifespan() yield 之后
        @Autowired 注入 Bean                 app.state.xxx 挂载共享对象

    为什么 import 放在函数内部？
        - 避免模块级别的循环导入（Python 特有问题）
        - 这些模块只在启动时需要，延迟导入不影响运行时性能
        - 类似于 Java 的按需加载（lazy initialization）
    """
    from backend.database import init_db, get_db_session_factory
    from backend.modules.config.loader import config_loader
    from backend.modules.channels.manager import ChannelManager
    from backend.modules.messaging.enterprise_queue import EnterpriseMessageQueue
    from backend.modules.messaging.rate_limiter import RateLimiter
    from backend.modules.channels.handler import ChannelMessageHandler
    from backend.modules.cron.executor import CronExecutor
    from backend.modules.cron.scheduler import CronScheduler
    from backend.modules.cron.service import CronService
    from backend.modules.agent.loop import AgentLoop
    from backend.modules.session.manager import SessionManager
    from backend.modules.tools.setup import register_all_tools
    from backend.api.channels import set_channel_manager

    # ===== 第 1 步：初始化基础设施（数据库 + 配置） =====
    # 这两个是所有后续组件的前提：没有数据库就没法读配置，没有配置就不知道用哪个模型
    logger.info("Starting CountBot backend...")
    await init_db()                    # 创建数据库表（如果不存在），类似于 Flyway/Liquibase 的 migrate
    logger.info("Database initialized")
    await config_loader.load()         # 从 settings 表加载配置到内存中的 AppConfig 对象
    logger.info("Configuration loaded")
    config = config_loader.config      # 取出 AppConfig 对象，后续所有组件从这里读取配置

    # ===== 第 2 步：创建核心共享组件 =====
    # _create_shared_components() 创建 LLM Provider、工具注册表、上下文构建器等
    # 这些组件被多个模块共享（WebSocket、渠道处理器、定时任务等都要用）
    logger.info("Creating shared components...")
    shared = _create_shared_components(config)
    app.state.shared = shared          # 挂载到 app.state 上，让 API 路由也能访问这些组件
                                       # 类似于 Java Spring 中把 Bean 注册到 ApplicationContext
    logger.info("Shared components created")

    # ===== 第 3 步：创建消息队列和限流器 =====
    # 消息队列：渠道收到的消息先入队，再由处理器统一消费（生产者-消费者模式）
    # 限流器：防止短时间内处理过多消息（如被刷消息时）
    logger.info("Creating message queue and rate limiter...")
    message_queue = EnterpriseMessageQueue(
        enable_dedup=True,             # 启用消息去重，防止同一条消息被处理两次
        dedup_window=10                # 去重时间窗口：10 秒内的重复消息会被丢弃
    )
    rate_limiter = RateLimiter(rate=10, per=60)  # 限流：每 60 秒最多处理 10 条消息
    logger.info("Message queue and rate limiter created")

    # ===== 第 4 步：创建渠道消息处理器 =====
    # ChannelMessageHandler 是渠道消息的核心处理器：
    # 从消息队列中取出消息 → 调用 Agent 处理 → 将回复发回对应渠道
    # 它内部会为每条消息创建 AgentLoop 来处理
    logger.info("Creating message handler...")
    message_handler = ChannelMessageHandler(
        provider=shared["provider"],           # LLM 提供商（用于调用大模型）
        workspace=shared["workspace"],         # 工作空间路径
        model=config.model.model,              # 模型名称（如 "glm-4-flash"）
        bus=message_queue,                     # 消息队列（消费者端）
        context_builder=shared["context_builder"],  # 上下文构建器（构建系统提示词）
        tool_params=shared["tool_params"],     # 工具参数（用于创建工具注册表）
        subagent_manager=shared["subagent_manager"],  # 子代理管理器
        max_iterations=config.model.max_iterations,   # Agent 最大迭代次数
        rate_limiter=rate_limiter,             # 限流器
        temperature=config.model.temperature,  # 生成温度
        max_tokens=config.model.max_tokens,    # 最大 token 数
        max_history_messages=config.persona.max_history_messages,  # 历史消息保留条数
        memory_store=shared["memory"],         # 记忆存储
    )
    app.state.message_handler = message_handler  # 挂载到 app.state，供 API 使用
    logger.info("Message handler created")

    # ===== 第 5 步：创建渠道管理器 =====
    # ChannelManager 管理所有消息渠道（Telegram、钉钉、微信等）
    # 它负责连接各个平台的 API，接收消息后投递到消息队列
    logger.info("Creating channel manager...")
    channel_manager = ChannelManager(config, message_queue)
    set_channel_manager(channel_manager)              # 设置全局引用，供 channels API 路由使用
    message_handler.set_channel_manager(channel_manager)  # 处理器需要知道渠道管理器，以便发送回复
    logger.info("Channel manager created")

    # ===== 第 6 步：初始化 OSS 上传器（可选） =====
    # 某些渠道（如 QQ）发送图片需要先上传到 OSS（对象存储服务），获取 URL 后再发送
    # 这是可选功能，初始化失败不影响核心服务
    logger.info("Initializing OSS uploader (optional)...")
    try:
        from backend.modules.tools.image_uploader import init_oss_uploader
        oss_config = None
        if hasattr(config.channels, "qq") and hasattr(config.channels.qq, "oss"):
            oss_config = config.channels.qq.oss.model_dump()
        init_oss_uploader(oss_config)
        logger.info("OSS uploader initialized")
    except Exception as e:
        logger.warning(f"OSS uploader init failed (optional): {e}")

    # ===== 第 7 步：启动后台常驻任务 =====
    # asyncio.create_task() 在后台启动协程，不阻塞当前执行
    # 类似于 Java 中 new Thread(runnable).start()，但更轻量（协程 vs 线程）
    # 这些任务会一直运行到应用关闭：
    #   - channel_manager.start_all()：保持与各平台的长连接（如 Telegram 轮询）
    #   - message_handler.start_processing()：持续从消息队列中消费消息并处理
    app.state.background_tasks = []    # 保存 Task 引用，防止被垃圾回收
    if channel_manager.enabled_channels:
        task = asyncio.create_task(channel_manager.start_all())
        app.state.background_tasks.append(task)
        logger.info(f"Started {len(channel_manager.enabled_channels)} channel(s) in background")

    task = asyncio.create_task(message_handler.start_processing())
    app.state.background_tasks.append(task)
    logger.info("Started message handler in background")

    # ===== 第 8 步：初始化定时任务系统 =====
    # 定时任务（Cron）有自己独立的 AgentLoop，与用户聊天的 AgentLoop 分开
    # 这样定时任务执行时不会干扰用户对话
    logger.info("Initializing cron system...")
    cron_tool_registry = register_all_tools(   # 为 cron 创建独立的工具注册表
        **shared["tool_params"],
    )
    cron_agent = AgentLoop(                    # cron 专用的 Agent 循环
        provider=shared["provider"],           # 共享同一个 LLM 提供商
        workspace=shared["workspace"],
        tools=cron_tool_registry,              # 使用独立的工具注册表（隔离状态）
        context_builder=shared["context_builder"],
        subagent_manager=shared["subagent_manager"],
        model=config.model.model,
        max_iterations=config.model.max_iterations,
        temperature=config.model.temperature,
        max_tokens=config.model.max_tokens,
    )
    session_manager = SessionManager(shared["workspace"])  # 会话管理器，管理 cron 任务的对话上下文
    logger.info("Cron agent and session manager created")

    # ===== 第 9 步：初始化心跳服务 =====
    # 心跳服务让 AI 能够主动给用户发消息（如早安问候、空闲提醒）
    # 它是一种特殊的定时任务，需要感知用户的活跃状态
    logger.info("Initializing heartbeat service...")
    db_session_factory = get_db_session_factory()  # 获取数据库会话工厂（用于查询用户活跃记录）

    from backend.modules.agent.heartbeat import HeartbeatService, ensure_heartbeat_job
    heartbeat_config = config.persona.heartbeat    # 心跳配置（是否启用、调度时间、免打扰时段等）
    heartbeat_service = HeartbeatService(
        provider=shared["provider"],
        model=config.model.model,
        workspace=shared["workspace"],
        db_session_factory=db_session_factory,
        ai_name=config.persona.ai_name or "小C",             # AI 的名字
        user_name=config.persona.user_name or "主人",          # 对用户的称呼
        user_address=config.persona.user_address or "",        # 用户所在地（用于天气等）
        personality=config.persona.personality or "professional",  # AI 性格 ID
        custom_personality=config.persona.custom_personality or "",
        idle_threshold_hours=heartbeat_config.idle_threshold_hours,  # 用户多久没聊天才问候
        quiet_start=heartbeat_config.quiet_start,              # 免打扰开始时间（如 21 点）
        quiet_end=heartbeat_config.quiet_end,                  # 免打扰结束时间（如 8 点）
        max_greets_per_day=heartbeat_config.max_greets_per_day,  # 每天最多问候几次
    )
    logger.info("Heartbeat service created")

    # ===== 第 10 步：创建 Cron 执行器 =====
    # CronExecutor 是定时任务的实际执行者：
    # 调度器触发 → 执行器调用 cron_agent 处理任务 → 将结果通过消息队列投递到指定渠道
    logger.info("Creating cron executor...")
    cron_executor = CronExecutor(
        agent=cron_agent,                      # cron 专用 Agent
        bus=message_queue,                     # 消息队列（用于投递任务结果到渠道）
        session_manager=session_manager,       # 会话管理（维护 cron 任务的对话上下文）
        channel_manager=channel_manager,       # 渠道管理（知道往哪个渠道发送结果）
        heartbeat_service=heartbeat_service,   # 心跳服务（心跳任务需要用到）
    )
    logger.info("Cron executor created")

    # 定义回调函数：调度器触发时调用执行器
    # 这是一个闭包（closure），捕获了外层的 cron_executor 变量
    # 类似于 Java 中的 Lambda 表达式 / 函数式接口
    async def on_cron_execute(
        job_id: str, message: str, channel: str, chat_id: str, deliver_response: bool
    ) -> str:
        return await cron_executor.execute(
            job_id, message, channel, chat_id, deliver_response
        )

    # ===== 第 11 步：启动 Cron 调度器 =====
    # CronScheduler 负责按 cron 表达式定时触发任务
    # 类似于 Java 的 ScheduledExecutorService 或 Spring @Scheduled
    logger.info("Creating cron scheduler...")
    scheduler = CronScheduler(
        db_session_factory=db_session_factory,  # 从数据库读取 cron 任务定义
        on_execute=on_cron_execute,             # 任务触发时的回调函数
    )
    await scheduler.start()                     # 启动调度器（开始定时轮询和执行）
    logger.info("Cron scheduler started")

    # ===== 第 12 步：注册内置心跳任务 =====
    # 确保数据库中有心跳任务的记录（如果没有就创建）
    # 然后触发调度器重新加载任务列表
    logger.info("Ensuring heartbeat job...")
    await ensure_heartbeat_job(db_session_factory, heartbeat_config=heartbeat_config)
    await scheduler.trigger_reschedule()       # 通知调度器重新读取数据库中的任务列表
    logger.info("Heartbeat job ensured")

    # 将调度器和执行器挂载到 app.state，供 API 路由使用（如创建/删除定时任务的接口）
    app.state.cron_scheduler = scheduler
    app.state.cron_executor = cron_executor

    # 工厂函数：为工具调用创建 CronService 实例
    # 使用工厂模式而非直接暴露实例，是因为每次调用需要独立的数据库会话
    async def get_cron_service_for_tool():
        async with db_session_factory() as db:
            return CronService(db, scheduler=scheduler)

    app.state.get_cron_service = get_cron_service_for_tool

    # ===== 第 13 步：注册进程退出清理处理器（备用机制） =====
    # atexit 是 Python 内置模块，注册的函数在进程退出时自动调用
    # 这是一个"兜底"机制：正常情况下 yield 之后的代码会执行清理，
    # 但如果进程被强制杀死（如 kill -9），yield 之后的代码不会执行，
    # 这时 atexit 注册的函数就是最后的清理机会
    #
    # 注意：atexit 回调是同步的，但 channel_manager.stop_all() 是异步的，
    # 所以需要手动创建事件循环来运行异步代码（这在正常情况下不推荐，但作为兜底机制可以接受）
    import atexit

    def cleanup_on_exit() -> None:
        """进程退出时的清理函数

        为什么只清理 channel_manager 而没有 scheduler？
        - channel_manager 维护着与外部平台的 TCP 长连接（Telegram 轮询、钉钉 WebSocket 等），
          不主动断开的话对方还在等心跳，会一直占用连接资源直到超时
        - scheduler 是纯内存中的定时器，进程退出后协程和定时任务随进程一起消失，无需显式清理

        简单说：atexit 兜底只做"必须显式断开的外部连接清理"，内部资源进程死了自然回收。
        """
        logger.info("atexit cleanup triggered")
        try:
            loop = asyncio.new_event_loop()        # 创建新的事件循环（原来的可能已关闭）
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(channel_manager.stop_all())  # 同步等待异步清理完成
            finally:
                loop.close()
        except RuntimeError as e:
            logger.debug(f"Event loop already closed: {e}")
        except Exception as e:
            logger.error(f"Error in atexit cleanup: {e}")

    atexit.register(cleanup_on_exit)

    logger.info("Backend started successfully")

    # ===== yield：分隔启动和关闭 =====
    # yield 是 Python 异步上下文管理器的关键：
    #   - yield 之前 = __aenter__()  = 应用启动时执行
    #   - yield 之后 = __aexit__()   = 应用关闭时执行
    # FastAPI 在收到 yield 后认为启动完成，开始接收 HTTP 请求
    # 当收到关闭信号（Ctrl+C / SIGTERM）时，继续执行 yield 之后的代码
    yield

    # ===== 正常关闭流程（Graceful Shutdown） =====
    # 类似于 Java Spring 的 @PreDestroy 或 DisposableBean.destroy()
    # 按依赖关系的反序关闭：先关渠道连接，再关调度器
    logger.info("Initiating graceful shutdown...")
    await channel_manager.stop_all()           # 断开所有渠道的连接（Telegram、钉钉等）
    await scheduler.stop()                     # 停止定时任务调度器
    logger.info("Backend shutdown complete")


app = FastAPI(
    title="CountBot Desktop API",   # 应用名称，显示在自动生成的 API 文档页面标题上
    description="CountBot backend API",  # 应用描述，显示在 API 文档页面
    version="0.1.0",                # API 版本号，显示在 API 文档页面
    lifespan=lifespan,              # 生命周期管理器，控制应用启动和关闭时的初始化/清理逻辑
)

# 保存绑定地址用于认证判断
import os as _os
app.state.bind_host = _os.getenv("HOST", "127.0.0.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 远程访问认证中间件
from backend.modules.auth.middleware import RemoteAuthMiddleware
from backend.modules.auth.router import get_password_hash

app.add_middleware(RemoteAuthMiddleware, get_password_hash_fn=get_password_hash)

# 注册 API 路由
from backend.api.chat import router as chat_router
from backend.api.settings import router as settings_router
from backend.api.tools import router as tools_router
from backend.api.memory import router as memory_router
from backend.api.skills import router as skills_router
from backend.api.cron import router as cron_router
from backend.api.tasks import router as tasks_router
from backend.api.audio import router as audio_router
from backend.api.system import router as system_router
from backend.api.channels import router as channels_router
from backend.api.queue import router as queue_router
from backend.api.auth import router as auth_router
from backend.api.personalities import router as personalities_router

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(settings_router)
app.include_router(tools_router)
app.include_router(memory_router)
app.include_router(skills_router)
app.include_router(cron_router)
app.include_router(tasks_router)
app.include_router(audio_router)
app.include_router(system_router)
app.include_router(channels_router)
app.include_router(queue_router)
app.include_router(personalities_router)


# WebSocket 端点
from fastapi import WebSocket
from backend.ws.connection import handle_websocket


@app.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 聊天端点

    这是前端与 Agent 实时通信的入口。每个用户在浏览器中打开聊天页面时，
    前端会建立一个 WebSocket 连接到这里。

    整体流程：
        1. 认证检查 —— 判断是本地还是远程连接，远程需要验证 token
        2. 创建独立组件 —— 每个连接有自己的工具注册表、LLM Provider、AgentLoop
        3. 交给 handle_websocket 处理 —— 进入消息收发循环

    与 Java 对比：
        - 类似 Spring WebSocket 的 @ServerEndpoint 或 WebSocketHandler
        - 每个连接进入这个函数，函数不 return 就一直保持连接
        - 函数 return 后连接自动关闭

    为什么每个连接都要创建独立的 AgentLoop？
        - 工具注册表需要会话隔离（每个用户的工具状态独立）
        - Provider 需要从最新配置创建（用户可能刚在设置页面切换了模型）
        - AgentLoop 本身是无状态的，创建成本很低
    """
    from backend.modules.agent.loop import AgentLoop
    from backend.modules.providers.litellm_provider import LiteLLMProvider
    from backend.modules.providers.registry import get_provider_metadata
    from backend.modules.tools.setup import register_all_tools

    from backend.modules.auth.middleware import LOCAL_IPS
    from backend.modules.auth.utils import validate_session as validate_ws_session
    from backend.modules.auth.router import get_password_hash as get_ws_password_hash

    # ===== 第 1 步：认证检查 =====
    # WebSocket 不走 HTTP 中间件的认证流程（中间件只拦截 HTTP 请求），
    # 所以需要在这里手动做认证。

    # 1.1 获取客户端 IP（从 TCP 连接层面获取，不可伪造）
    client_ip = websocket.client.host if websocket.client else None

    if not client_ip:
        logger.warning("WebSocket connection rejected: unable to determine client IP")
        await websocket.close(code=1008, reason="Unable to determine client IP")
        return

    # 1.2 检测是否经过了反向代理（Nginx 等）
    # 如果请求头中包含代理相关的头，说明经过了反向代理，
    # 即使 TCP 层面看起来是 127.0.0.1（代理在本机），实际用户可能在远程
    proxy_headers = {
        "x-forwarded-for", "x-real-ip", "x-forwarded-host",
        "x-forwarded-proto", "forwarded", "via", "x-forwarded-server",
        "x-cluster-client-ip", "cf-connecting-ip", "true-client-ip"
    }
    request_headers = {k.lower() for k in websocket.headers.keys()}
    has_proxy = bool(proxy_headers & request_headers)  # 集合交集：检查是否有任何代理头

    # 1.3 判断是否为本地连接
    # 必须同时满足：TCP 层 IP 是本地 且 没有代理头
    is_local = client_ip in LOCAL_IPS and not has_proxy

    if has_proxy:
        logger.info(f"WebSocket proxy headers detected, treating as remote (socket IP: {client_ip})")

    logger.info(f"WebSocket connection from {client_ip} ({'local' if is_local else 'remote'})")

    # 1.4 远程连接需要验证 token
    # 本地连接（localhost）直接放行，远程连接必须携带有效的认证 token
    if not is_local:
        pw_hash = ""
        try:
            pw_hash = await get_ws_password_hash()  # 查询数据库中是否设置了密码
        except Exception:
            pass

        if pw_hash:  # 设置了密码 → 需要验证 token
            # token 可以从 URL 查询参数或 Cookie 中获取
            # 例如 ws://localhost:8000/ws?token=xxx 或 Cookie: CountBot_token=xxx
            token = websocket.query_params.get("token") or websocket.cookies.get("CountBot_token")
            if not token or not validate_ws_session(token):
                await websocket.close(code=4001, reason="Authentication required")
                return

    # ===== 第 2 步：获取共享组件 =====
    # 从 app.state 获取 lifespan 中创建的共享组件
    shared = websocket.app.state.shared

    # ===== 第 3 步：创建会话独立的工具注册表 =====
    # 每个 WebSocket 连接创建独立的工具注册表实例
    # 原因：工具注册表内部有会话状态（如 session_id），不同用户的会话不能共享
    # 类似于 Java Spring 中 scope=prototype 的 Bean（每次注入新实例）
    tool_registry = register_all_tools(
        **shared["tool_params"],         # 解包共享的工具参数（workspace、subagent_manager 等）
        memory_store=shared["memory"],
    )

    # ===== 第 4 步：从最新配置创建 LLM Provider =====
    # 每次连接都重新读取配置，这样用户在设置页面切换模型后，
    # 新建的聊天会话就能立即使用新模型（无需重启服务）
    from backend.modules.config.loader import config_loader
    config = config_loader.config

    provider_id = config.model.provider                      # 当前使用的 provider ID（如 "zhipu"）
    provider_config = config.providers.get(provider_id)      # 该 provider 的配置（api_key、api_base）
    provider_meta = get_provider_metadata(provider_id)       # 该 provider 的元数据（默认 api_base 等）

    api_key = provider_config.api_key if provider_config else None
    api_base = (
        provider_config.api_base                             # 优先使用用户自定义的 api_base
        if provider_config and provider_config.api_base
        else (provider_meta.default_api_base if provider_meta else None)  # 没有则用注册表中的默认值
    )

    provider = LiteLLMProvider(
        api_key=api_key,
        api_base=api_base,
        default_model=config.model.model,
        timeout=120.0,                                       # LLM 调用超时 120 秒（大模型生成可能较慢）
        max_retries=3,                                       # 网络异常时最多重试 3 次
        provider_id=provider_id,
    )

    # ===== 第 5 步：创建 AgentLoop =====
    # AgentLoop 是无状态处理器，每个连接创建一个，用完即弃
    agent_loop = AgentLoop(
        provider=provider,                                   # 这个连接专用的 LLM Provider
        workspace=shared["workspace"],                       # 共享的工作空间（所有用户共用）
        tools=tool_registry,                                 # 这个连接独立的工具注册表
        context_builder=shared["context_builder"],           # 共享的上下文构建器
        subagent_manager=shared["subagent_manager"],         # 共享的子代理管理器
        model=config.model.model,
        max_iterations=config.model.max_iterations,
        temperature=config.model.temperature,
        max_tokens=config.model.max_tokens,
    )

    # ===== 第 6 步：进入 WebSocket 消息处理循环 =====
    # handle_websocket 内部是一个 while True 循环：
    #   接收前端消息 → 调用 agent_loop.process_message() → 流式推送回复给前端
    # 这个函数不会 return，直到连接断开（用户关闭页面、网络中断等）
    await handle_websocket(websocket, agent_loop=agent_loop)


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": "0.1.0"}


# 挂载前端静态文件
from backend.utils.paths import APPLICATION_ROOT

frontend_dist = APPLICATION_ROOT / "frontend" / "dist"
if frontend_dist.exists():
    from fastapi.responses import FileResponse
    import mimetypes
    
    # 确保 Windows 上正确识别 JavaScript 模块的 MIME 类型
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    mimetypes.add_type("image/svg+xml", ".svg")

    # SPA 路由回退（必须在 StaticFiles 之前注册）
    @app.get("/login")
    async def spa_login():
        return FileResponse(str(frontend_dist / "index.html"))

    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="static")
