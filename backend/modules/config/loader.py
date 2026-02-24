"""配置加载器

负责从数据库加载配置、保存配置到数据库。
配置以扁平化的 key-value 形式存储在 settings 表中，加载时还原为嵌套的 AppConfig 对象。

存储格式示例：
    数据库中：config.model.provider = "openai"
              config.model.model = "gpt-4"
              config.providers.openai.api_key = "sk-xxx"
    还原后：  AppConfig(model=ModelConfig(provider="openai", model="gpt-4"), ...)
"""

import json
from typing import Any

from loguru import logger
from sqlalchemy import select

from backend.database import AsyncSessionLocal          # 异步会话工厂，用于自行创建数据库会话
from backend.models.setting import Setting              # Setting ORM 模型，对应 settings 表（key-value 结构）
from backend.modules.config.schema import AppConfig     # 配置的 Pydantic 模型，定义了所有配置项的结构和默认值


class ConfigLoader:
    """配置加载器

    职责：
    1. 从数据库读取扁平化的 key-value 配置，还原为嵌套的 AppConfig 对象
    2. 将 AppConfig 对象扁平化后保存到数据库
    3. 处理 API 密钥的加密/解密
    """

    def __init__(self) -> None:
        # 初始化时使用默认配置（AppConfig 的默认值），后续 load() 会从数据库覆盖
        self.config: AppConfig = AppConfig()

    async def load(self) -> AppConfig:
        """从数据库加载配置

        流程：
        1. 查询所有 key 以 "config." 开头的 Setting 记录
        2. 将扁平的 key-value 还原为嵌套字典
        3. 用嵌套字典构造 AppConfig 对象
        4. 如果启用了加密，解密 API 密钥
        """
        async with AsyncSessionLocal() as session:
            # 查询所有配置项：SELECT * FROM settings WHERE key LIKE 'config.%'
            # .like() 是 SQLAlchemy 的模糊匹配，等价于 SQL 的 LIKE 操作符
            result = await session.execute(
                select(Setting).where(Setting.key.like("config.%"))
            )
            # .scalars() 将 Row 转为 ORM 对象，.all() 取所有结果为列表
            settings = result.scalars().all()

            # 数据库中没有任何配置 → 首次启动，把默认配置写入数据库
            if not settings:
                logger.info("未找到配置，使用默认配置")
                await self.save()           # 将默认的 self.config 保存到数据库
                return self.config

            # ===== 完整示例：从数据库到 AppConfig 的还原过程 =====
            #
            # 【第 1 步】数据库 settings 表中存的是扁平的 key-value：
            #
            #   | key                              | value      |
            #   |----------------------------------|------------|
            #   | config.model.provider            | "zhipu"    |
            #   | config.model.model               | "glm-5"   |
            #   | config.model.temperature         | 0.7        |
            #   | config.providers.zhipu.api_key    | "sk-xxx"   |
            #   | config.providers.zhipu.api_base   | "https://..."  |
            #   | config.security.command_timeout   | 60         |
            #
            # 【第 2 步】上面的 SQL 查询返回 result，经过 .scalars().all() 后得到 settings：
            #   settings 是一个列表，每个元素是一个 Setting ORM 对象：
            #
            #   settings = [
            #       Setting(key="config.model.provider",           value='"zhipu"'),
            #       Setting(key="config.model.model",              value='"glm-5"'),
            #       Setting(key="config.model.temperature",        value='0.7'),
            #       Setting(key="config.providers.zhipu.api_key",  value='"sk-xxx"'),
            #       Setting(key="config.providers.zhipu.api_base", value='"https://..."'),
            #       Setting(key="config.security.command_timeout",  value='60'),
            #       ...
            #   ]
            #
            #   注意：setting.value 是 JSON 字符串！
            #         字符串 "zhipu" 在数据库中存的是 '"zhipu"'（带引号）
            #         数字 0.7 在数据库中存的是 '0.7'（不带引号）
            #
            # 【第 3 步】下面的 for 循环遍历每个 setting，还原为嵌套字典：

            config_dict: dict[str, Any] = {}
            for setting in settings:
                # 单个 setting 示例：
                #   setting.key   = "config.model.provider"
                #   setting.value = '"zhipu"'

                # 去掉 "config." 前缀，得到实际的配置路径
                # "config.model.provider" → "model.provider"
                key_path = setting.key.replace("config.", "")

                # JSON 反序列化：把 JSON 字符串还原为 Python 对象
                # json.loads('"zhipu"') → "zhipu"（str）
                # json.loads('0.7')     → 0.7（float）
                # json.loads('true')    → True（bool）
                # json.loads('null')    → None
                value = json.loads(setting.value)

                # 防御性处理：api_key 为 null 时替换为空字符串，避免后续代码出现 None 异常
                if value is None and "api_key" in key_path:
                    value = ""

                # 将扁平路径设置到嵌套字典中
                # _set_nested_value(config_dict, "model.provider", "zhipu")
                #   → config_dict = {"model": {"provider": "zhipu"}}
                #
                # _set_nested_value(config_dict, "model.model", "glm-5")
                #   → config_dict = {"model": {"provider": "zhipu", "model": "glm-5"}}
                #
                # _set_nested_value(config_dict, "providers.zhipu.api_key", "sk-xxx")
                #   → config_dict = {"model": {...}, "providers": {"zhipu": {"api_key": "sk-xxx"}}}
                self._set_nested_value(config_dict, key_path, value)

            # 【第 4 步】循环结束后，config_dict 变成了完整的嵌套字典：
            #   {
            #       "model": {"provider": "zhipu", "model": "glm-5", "temperature": 0.7},
            #       "providers": {"zhipu": {"api_key": "sk-xxx", "api_base": "https://..."}},
            #       "security": {"command_timeout": 60},
            #       ...
            #   }
            #
            # 【第 5 步】后面用 AppConfig(**config_dict) 构造 Pydantic 模型（见下方代码）

            # 二次防御：确保所有 provider 的 api_key 不为 None
            if "providers" in config_dict:
                for provider_name, provider_data in config_dict["providers"].items():
                    if isinstance(provider_data, dict) and provider_data.get("api_key") is None:
                        provider_data["api_key"] = ""

            # 用嵌套字典构造 Pydantic 模型（会自动校验类型和填充默认值）
            # ** 是字典解包，等价于 AppConfig(model=..., providers=..., security=..., ...)
            self.config = AppConfig(**config_dict)

            # 如果启用了加密，解密 API 密钥
            # 数据库中存的是加密后的密文，加载到内存后需要解密成明文供程序使用
            if self.config.security.api_key_encryption_enabled:
                self._decrypt_api_keys()
                logger.info("API 密钥加密已启用")
            else:
                logger.warning("API 密钥加密未启用，建议在生产环境中启用")

            logger.info("配置加载完成")
            return self.config

    async def save(self) -> None:
        """保存配置到数据库

        流程：
        1. 将 AppConfig 对象转为嵌套字典
        2. 如果启用了加密，加密 API 密钥
        3. 递归地将嵌套字典扁平化后写入 settings 表
        """
        async with AsyncSessionLocal() as session:
            # model_dump() 是 Pydantic v2 的方法，将模型对象转为普通字典
            # 等价于 Pydantic v1 的 .dict()
            config_dict = self.config.model_dump()

            # 保存前加密 API 密钥（内存中是明文，数据库中存密文）
            if self.config.security.api_key_encryption_enabled:
                config_dict = self._encrypt_api_keys_in_dict(config_dict)

            # 递归保存，前缀 "config" 会变成数据库中 key 的开头
            # {"model": {"provider": "openai"}} → settings 表中 key="config.model.provider", value='"openai"'
            await self._save_nested_dict(session, config_dict, "config")
            await session.commit()          # 提交事务，真正写入数据库
            logger.info("配置保存完成")

    async def save_config(self, config: AppConfig) -> None:
        """外部调用的保存接口：先更新内存中的配置，再持久化到数据库"""
        self.config = config
        await self.save()

    async def _save_nested_dict(
        self, session: Any, data: dict[str, Any], prefix: str
    ) -> None:
        """递归保存嵌套字典为扁平的 key-value

        示例：
            输入：data={"model": {"provider": "openai", "model": "gpt-4"}}, prefix="config"
            递归过程：
                key="model", value={"provider": "openai", ...} → 是 dict，继续递归，prefix="config.model"
                    key="provider", value="openai" → 不是 dict，写入 key="config.model.provider", value='"openai"'
                    key="model", value="gpt-4" → 不是 dict，写入 key="config.model.model", value='"gpt-4"'
        """
        for key, value in data.items():
            full_key = f"{prefix}.{key}"        # 拼接完整的 key 路径
            if isinstance(value, dict):
                # 值是字典 → 还有嵌套层级，继续递归
                await self._save_nested_dict(session, value, full_key)
            else:
                # 值是叶子节点 → 序列化为 JSON 字符串后写入数据库
                # json.dumps("openai") → '"openai"'（带引号的字符串）
                # json.dumps(True) → 'true'
                setting = Setting(key=full_key, value=json.dumps(value))
                # merge() 的作用：如果 key 已存在则更新（UPDATE），不存在则插入（INSERT）
                # 类似于 SQL 的 INSERT ... ON CONFLICT UPDATE（即 upsert）
                await session.merge(setting)

    def _set_nested_value(self, data: dict[str, Any], key_path: str, value: Any) -> None:
        """将扁平的 key 路径设置到嵌套字典中

        示例：
            data = {}
            _set_nested_value(data, "model.provider", "openai")
            结果：data = {"model": {"provider": "openai"}}

        原理：按 "." 分割 key，逐层创建/进入字典，最后一层赋值
        """
        keys = key_path.split(".")         # "model.provider" → ["model", "provider"]
        current = data
        for key in keys[:-1]:              # 遍历除最后一个以外的所有 key（中间层级）
            if key not in current:
                current[key] = {}           # 中间层级不存在则创建空字典
            current = current[key]          # 进入下一层
        current[keys[-1]] = value           # 最后一个 key 直接赋值

    async def get(self, key: str, default: Any = None) -> Any:
        """通过点分路径获取配置值

        示例：
            config_loader.get("model.provider")
            → 等价于 config_loader.config.model.provider

            config_loader.get("model.nonexistent", "fallback")
            → 属性不存在时返回默认值 "fallback"
        """
        keys = key.split(".")
        value = self.config
        for k in keys:
            # getattr(obj, name, default) — 获取对象属性，不存在时返回 default
            # 这里逐层深入：config → config.model → config.model.provider
            value = getattr(value, k, None)
            if value is None:
                return default              # 任何一层为 None 就提前返回默认值
        return value

    async def set(self, key: str, value: Any) -> None:
        """通过点分路径设置配置值，并立即持久化到数据库

        示例：
            await config_loader.set("model.provider", "anthropic")
            → 等价于 config_loader.config.model.provider = "anthropic"
            → 然后自动保存到数据库
        """
        keys = key.split(".")
        obj = self.config
        for k in keys[:-1]:
            # 逐层进入：config → config.model（获取倒数第二层的对象）
            obj = getattr(obj, k)
        # setattr(obj, name, value) — 设置对象属性
        # 在最后一层设置值：config.model.provider = "anthropic"
        setattr(obj, keys[-1], value)
        await self.save()                   # 修改后立即持久化

    def _decrypt_api_keys(self) -> None:
        """解密所有 provider 的 API 密钥

        在 load() 中调用。数据库存的是密文，加载到内存后解密为明文。
        解密失败时不抛异常，只打 warning 日志（可能是密钥格式不对或未加密）。
        """
        from backend.modules.config.security import get_security_manager  # 延迟导入，避免循环依赖
        security_manager = get_security_manager()

        # 遍历所有已配置的 provider（如 openai、anthropic 等）
        for provider_name, provider_config in self.config.providers.items():
            if provider_config.api_key:     # 有 api_key 才需要解密
                try:
                    decrypted = security_manager.decrypt(provider_config.api_key)
                    if decrypted:
                        # 直接修改 Pydantic 模型对象的属性，将密文替换为明文
                        provider_config.api_key = decrypted
                        logger.debug(f"解密 {provider_name} API 密钥")
                except Exception as e:
                    # 解密失败不影响启动，可能是密钥本身就是明文（未加密时）
                    logger.warning(f"解密 {provider_name} API 密钥失败: {e}")

    def _encrypt_api_keys_in_dict(self, config_dict: dict[str, Any]) -> dict[str, Any]:
        """加密配置字典中的 API 密钥

        在 save() 中调用。内存中是明文，保存到数据库前加密。
        注意：操作的是普通字典（model_dump() 的结果），不是 Pydantic 模型对象。
        """
        from backend.modules.config.security import get_security_manager  # 延迟导入，避免循环依赖
        security_manager = get_security_manager()

        if "providers" in config_dict:
            for provider_name, provider_data in config_dict["providers"].items():
                # isinstance 检查是防御性编程，确保 provider_data 确实是字典
                if isinstance(provider_data, dict) and provider_data.get("api_key"):
                    try:
                        encrypted = security_manager.encrypt(provider_data["api_key"])
                        if encrypted:
                            # 将明文替换为密文（修改的是字典，不影响内存中的 self.config）
                            provider_data["api_key"] = encrypted
                            logger.debug(f"加密 {provider_name} API 密钥")
                    except Exception as e:
                        # 加密失败则保留明文存储，不阻塞保存流程
                        logger.warning(f"加密 {provider_name} API 密钥失败: {e}")
        return config_dict


# 模块级单例：整个应用共享同一个 ConfigLoader 实例
# 其他模块通过 from backend.modules.config.loader import config_loader 引用
# 这是 Python 中常见的单例模式——模块在首次 import 时执行，后续 import 复用同一个对象
config_loader = ConfigLoader()
