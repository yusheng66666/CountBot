"""配置数据模型

使用 Pydantic BaseModel 定义所有配置项的结构、类型和默认值。
这些模型有两个作用：
1. 定义配置的"模板"——有哪些字段、什么类型、默认值是什么
2. 数据校验——从数据库加载配置时，自动验证值是否合法（如 temperature 必须在 0~2 之间）

层级关系：
AppConfig（总配置）
├── providers: dict[str, ProviderConfig]   # LLM 提供商配置（可多个）
├── model: ModelConfig                      # 当前使用的模型配置
├── workspace: WorkspaceConfig              # 工作空间配置
├── security: SecurityConfig                # 安全配置
├── channels: ChannelsConfig                # 消息渠道配置
│   ├── telegram: TelegramConfig
│   ├── discord: DiscordConfig
│   ├── qq: QQConfig
│   ├── wechat: WeChatConfig
│   ├── dingtalk: DingTalkConfig
│   └── feishu: FeishuConfig
├── persona: PersonaConfig                  # AI 人设和用户信息
│   └── heartbeat: HeartbeatConfig          # 主动问候配置
├── theme / language / font_size            # 前端 UI 配置
"""

from typing import Optional

from pydantic import BaseModel, Field


class ProviderConfig(BaseModel):
    """LLM 提供商配置（如 OpenAI、智谱、Anthropic 等）

    每个 provider 都有独立的 API 密钥和接口地址。
    在 AppConfig.providers 中以 dict 形式存储，key 是 provider_id。
    """
    api_key: str = ""                       # API 密钥，空字符串表示未配置
    api_base: Optional[str] = None          # API 接口地址，None 表示使用默认地址
    enabled: bool = False                   # 是否启用该提供商


class ModelConfig(BaseModel):
    """模型配置——控制 AI 对话行为的核心参数"""
    provider: str = "zhipu"                 # 当前使用的 LLM 提供商 ID
    model: str = "glm-5"                    # 当前使用的模型名称
    temperature: float = Field(             # 生成随机性，越高越随机（0=确定性，2=最随机）
        default=0.7, ge=0.0, le=2.0         # ge=大于等于, le=小于等于（Pydantic 校验约束）
    )
    max_tokens: int = Field(                # 单次回复最大 token 数，0 表示不限制
        default=0, ge=0, le=100000
    )
    max_iterations: int = Field(            # Agent 最大循环次数（防止工具调用死循环）
        default=25, ge=1, le=150
    )


class WorkspaceConfig(BaseModel):
    """工作空间配置"""
    path: str = ""                          # 工作空间路径，空字符串表示使用默认路径


class HeartbeatConfig(BaseModel):
    """主动问候配置——AI 在用户长时间不活跃时主动发起对话"""
    enabled: bool = Field(default=False, description="是否启用主动问候")
    channel: str = Field(default="", description="推送渠道（feishu/telegram/dingtalk/wechat/qq）")
    chat_id: str = Field(default="", description="推送目标 ID（群组或用户）")
    schedule: str = Field(default="0 * * * *", description="检查频率 cron 表达式")  # 默认每小时检查一次
    idle_threshold_hours: int = Field(      # 用户空闲超过这个时间才触发问候
        default=4, ge=1, le=24, description="用户空闲多少小时后触发"
    )
    quiet_start: int = Field(               # 免打扰时段开始（如 21 点），此时段内不会问候
        default=21, ge=0, le=23, description="免打扰开始时间（小时，北京时间）"
    )
    quiet_end: int = Field(                 # 免打扰时段结束（如 8 点），与 quiet_start 配合使用
        default=8, ge=0, le=23, description="免打扰结束时间（小时，北京时间）"
    )
    max_greets_per_day: int = Field(        # 防止频繁打扰用户
        default=2, ge=1, le=5, description="每天最多问候次数"
    )


class PersonaConfig(BaseModel):
    """用户信息和 AI 人设配置——决定 AI 的"性格"和"身份"""""
    ai_name: str = Field(default="小C", description="AI的名字")
    user_name: str = Field(default="主人", description="用户的称呼")
    user_address: str = Field(default="", description="用户的常用地址（可选，用于天气等查询）")
    personality: str = Field(default="grumpy", description="AI的性格类型")             # 对应 personalities 表中的 id
    custom_personality: str = Field(default="", description="自定义性格描述")            # personality="custom" 时使用
    max_history_messages: int = Field(      # 对话上下文保留条数，影响 AI 的"记忆长度"
        default=100, ge=-1, le=500, description="最大对话历史条数，-1表示不限"
    )
    heartbeat: HeartbeatConfig = Field(     # 嵌套的子配置
        default_factory=HeartbeatConfig, description="主动问候配置"
    )


class SecurityConfig(BaseModel):
    """安全配置——控制命令执行、API 密钥保护等安全策略"""
    # API 密钥加密（是否对数据库中的 api_key 进行加密存储）
    api_key_encryption_enabled: bool = Field(default=False)

    # 危险命令检测（如 rm -rf、格式化磁盘等）
    dangerous_commands_blocked: bool = Field(default=True)      # 是否拦截危险命令
    custom_deny_patterns: list[str] = Field(default_factory=list)  # 自定义的禁止命令模式（正则）

    # 命令白名单（启用后只允许白名单中的命令执行）
    command_whitelist_enabled: bool = Field(default=False)
    custom_allow_patterns: list[str] = Field(default_factory=list)  # 自定义的允许命令模式（正则）

    # 审计日志（记录所有工具调用和命令执行）
    audit_log_enabled: bool = Field(default=True)

    # 其他安全选项
    command_timeout: int = Field(default=60, ge=1, le=300)         # 命令执行超时时间（秒）
    max_output_length: int = Field(default=10000, ge=100, le=1000000)  # 命令输出最大长度（字符）
    restrict_to_workspace: bool = Field(default=False)             # 是否限制文件操作只能在工作空间内


class TelegramConfig(BaseModel):
    """Telegram 渠道配置"""
    enabled: bool = False                   # 是否启用 Telegram 渠道
    token: str = ""                         # Telegram Bot Token（从 @BotFather 获取）
    proxy: Optional[str] = None             # 代理地址（国内访问 Telegram 可能需要）
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户/群组 ID 白名单，空表示不限


class DiscordConfig(BaseModel):
    """Discord 渠道配置"""
    enabled: bool = False
    token: str = ""                         # Discord Bot Token
    allow_from: list[str] = Field(default_factory=list)


class TencentOSSConfig(BaseModel):
    """腾讯云 OSS 配置（可选，用于 QQ 渠道的图片上传）"""
    secret_id: str = ""                     # 腾讯云 API 密钥 ID
    secret_key: str = ""                    # 腾讯云 API 密钥 Secret
    bucket: str = ""                        # 存储桶名称
    region: str = "ap-guangzhou"            # 存储桶所在地域


class QQConfig(BaseModel):
    """QQ 渠道配置"""
    enabled: bool = False
    app_id: str = ""                        # QQ 机器人 AppID
    secret: str = ""                        # QQ 机器人 Secret
    allow_from: list[str] = Field(default_factory=list)
    markdown_enabled: bool = True           # 是否启用私聊 Markdown 格式
    group_markdown_enabled: bool = True     # 是否启用群聊 Markdown 格式
    oss: Optional[TencentOSSConfig] = Field(default_factory=TencentOSSConfig)  # 图片上传配置


class WeChatConfig(BaseModel):
    """微信渠道配置（微信公众号）"""
    enabled: bool = False
    app_id: str = ""                        # 微信公众号 AppID
    app_secret: str = ""                    # 微信公众号 AppSecret
    token: str = ""                         # 消息接口 Token（公众号后台设置的）
    encoding_aes_key: str = ""              # 消息加密密钥
    allow_from: list[str] = Field(default_factory=list)


class DingTalkConfig(BaseModel):
    """钉钉渠道配置"""
    enabled: bool = False
    client_id: str = ""                     # 钉钉应用 ClientID（即 AppKey）
    client_secret: str = ""                 # 钉钉应用 ClientSecret（即 AppSecret）
    allow_from: list[str] = Field(default_factory=list)


class FeishuConfig(BaseModel):
    """飞书渠道配置"""
    enabled: bool = False
    app_id: str = ""                        # 飞书应用 App ID
    app_secret: str = ""                    # 飞书应用 App Secret
    encrypt_key: str = ""                   # 事件订阅的加密密钥
    verification_token: str = ""            # 事件订阅的验证 Token
    allow_from: list[str] = Field(default_factory=list)


class ChannelsConfig(BaseModel):
    """渠道总配置——聚合所有消息渠道的配置"""
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    qq: QQConfig = Field(default_factory=QQConfig)
    wechat: WeChatConfig = Field(default_factory=WeChatConfig)
    dingtalk: DingTalkConfig = Field(default_factory=DingTalkConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)


class AppConfig(BaseModel):
    """应用总配置——所有配置的根节点

    这是 ConfigLoader 加载/保存的对象。
    每个字段都使用 Field(default_factory=...) 提供默认值，
    这样即使数据库中没有某个配置项，也能用默认值正常运行。
    """
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)  # LLM 提供商配置，key 为 provider_id
    model: ModelConfig = Field(default_factory=ModelConfig)              # 当前模型配置
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)  # 工作空间配置
    security: SecurityConfig = Field(default_factory=SecurityConfig)     # 安全策略配置
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)     # 消息渠道配置
    persona: PersonaConfig = Field(default_factory=PersonaConfig)        # AI 人设配置
    theme: str = "auto"                     # 前端主题（auto/light/dark）
    language: str = "auto"                  # 前端语言（auto/zh/en）
    font_size: str = "medium"               # 前端字号（small/medium/large）

    def __init__(self, **data):
        """初始化配置，自动补全未配置的 provider

        Pydantic 的 __init__ 会先执行 super().__init__(**data) 完成字段赋值和校验，
        然后遍历系统中注册的所有 provider，对于 self.providers 中不存在的 provider，
        用默认值补全。这样即使用户只配置了一个 provider，其他 provider 也有默认条目。
        """
        super().__init__(**data)

        # 延迟导入，避免循环依赖（registry 模块可能又引用了 schema）
        from backend.modules.providers.registry import get_provider_ids, get_provider_metadata

        for provider_id in get_provider_ids():          # 遍历系统中注册的所有 provider ID
            if provider_id not in self.providers:       # 只补全不存在的
                metadata = get_provider_metadata(provider_id)

                if provider_id == "zhipu":
                    # 智谱是默认 provider，预填 api_base 并启用
                    self.providers[provider_id] = ProviderConfig(
                        api_key="",
                        api_base="https://open.bigmodel.cn/api/paas/v4",
                        enabled=True
                    )
                else:
                    # 其他 provider 使用元数据中的默认 api_base，默认不启用
                    self.providers[provider_id] = ProviderConfig(
                        api_base=metadata.default_api_base if metadata else None
                    )
