"""Cron 任务执行器

由 CronScheduler 调度触发，负责实际执行定时任务。
两种任务类型：
  - 心跳任务：message == "__heartbeat__" → HeartbeatService 生成问候语
  - 普通任务：其他消息 → AgentLoop.process_direct() 让 AI 处理

执行结果可以通过渠道（如飞书）投递给用户。
"""

from typing import Optional

from backend.modules.agent.loop import AgentLoop
from backend.modules.messaging.enterprise_queue import EnterpriseMessageQueue
from backend.modules.session.manager import SessionManager
from backend.modules.channels.manager import ChannelManager
from backend.utils.logger import logger

# 心跳任务的特殊标记：CronJob.message 等于此值时，走 HeartbeatService 而非 AgentLoop
HEARTBEAT_MESSAGE_MARKER = "__heartbeat__"



class CronExecutor:
    """定时任务执行器 - CronScheduler.on_execute 回调指向 execute() 方法"""

    def __init__(
        self,
        agent: AgentLoop,
        bus: EnterpriseMessageQueue,
        session_manager: SessionManager,
        channel_manager: Optional[ChannelManager] = None,
        heartbeat_service=None,
    ):
        self.agent = agent                      # AgentLoop 实例，用于执行普通任务
        self.bus = bus                           # 消息队列（当前未使用）
        self.session_manager = session_manager   # 会话管理器
        self.channel_manager = channel_manager   # 渠道管理器（飞书等），用于投递执行结果
        self.heartbeat_service = heartbeat_service  # 心跳服务，负责生成主动问候

    async def execute(
        self,
        job_id: str,
        message: str,
        channel: Optional[str] = None,
        chat_id: Optional[str] = None,
        deliver_response: bool = False
    ) -> str:
        """执行定时任务 —— CronScheduler._execute_job() 中通过 on_execute 调用此方法

        流程：
        1. 判断是心跳任务还是普通任务
        2. 执行任务获取结果
        3. 保存消息到数据库（与渠道消息保持一致的会话记录）
        4. 如果配置了 deliver_response，将结果通过渠道发送给用户
        """
        logger.info(f"Executing job {job_id}: {message[:100]}...")

        # ── 分支 1：心跳任务（message == "__heartbeat__"）──
        # 走 HeartbeatService，有多重条件判断（免打扰、次数限制等），不一定会生成内容
        if message == HEARTBEAT_MESSAGE_MARKER:
            return await self._execute_heartbeat(job_id, channel, chat_id, deliver_response)

        # ── 分支 2：普通定时任务 ──
        try:
            # 查找或创建会话（同一个 channel:chat_id 共用一个 session）
            if channel and chat_id:
                session_id = await self._get_or_create_session(channel, chat_id)
            else:
                session_id = f"cron:{job_id}"  # 没有渠道信息就用 job_id 作为虚拟会话

            # 调用 AgentLoop 处理（非流式，直接返回完整结果）
            response = await self.agent.process_direct(
                content=message,
                session_id=session_id,
                channel=channel or "cron",
                chat_id=chat_id or job_id
            )

            logger.info(f"Job {job_id} completed")

            # 保存消息记录到数据库（用户消息 + AI 回复），方便后续查看历史
            if channel and chat_id and response:
                await self._save_messages_to_db(session_id, message, response)

            # 将执行结果通过渠道发送给用户（如飞书消息）
            if deliver_response and response and channel and chat_id:
                await self._deliver_to_channel(
                    channel=channel,
                    chat_id=chat_id,
                    message=response,
                    job_id=job_id
                )

            return response or ""

        except Exception as e:
            logger.error(f"Cron job {job_id} failed: {e}")
            raise

    async def _execute_heartbeat(
        self,
        job_id: str,
        channel: Optional[str] = None,
        chat_id: Optional[str] = None,
        deliver_response: bool = False,
    ) -> str:
        """执行心跳问候任务

        HeartbeatService.execute() 内部有多重判断（免打扰、次数限制、空闲检测、随机概率），
        大部分时候会返回空字符串（不发问候），只有条件全部满足时才生成问候语。
        """
        if not self.heartbeat_service:
            logger.warning("Heartbeat service not configured, skipping")
            return ""

        try:
            # 调用心跳服务：内部判断是否应该问候，返回空则表示本次跳过
            greeting = await self.heartbeat_service.execute()
            if not greeting:
                return ""

            # 通过渠道发送问候消息（如飞书私聊）
            if channel and chat_id:
                await self._deliver_to_channel(
                    channel=channel,
                    chat_id=chat_id,
                    message=greeting,
                    job_id=job_id,
                )

                # 同时保存到会话历史，这样用户回复问候时 AI 能看到上下文
                # （否则 AI 不知道自己刚问候过用户）
                await self._save_greeting_to_session(
                    channel=channel,
                    chat_id=chat_id,
                    greeting=greeting,
                )
            else:
                logger.warning(
                    "Heartbeat: no channel/chat_id configured on heartbeat cron job, "
                    "greeting generated but not delivered. "
                    "Please configure channel and chat_id in the cron job settings."
                )

            return greeting
        except Exception as e:
            logger.error(f"Heartbeat execution failed: {e}")
            return ""

    async def _deliver_to_channel(
        self,
        channel: str,
        chat_id: str,
        message: str,
        job_id: str
    ):
        """通过渠道投递消息（如飞书）

        channel: 渠道名称（如 "feishu"）
        chat_id: 目标聊天 ID（飞书的 open_id 或 chat_id）
        """
        try:
            if not self.channel_manager:
                logger.warning(f"Channel manager unavailable")
                return

            # 通过名称获取渠道实例（如 FeishuChannel）
            channel_instance = self.channel_manager.get_channel(channel)
            if not channel_instance:
                logger.warning(f"Channel {channel} not found")
                return

            logger.info(f"Delivering to {channel}:{chat_id}")

            # 构造 OutboundMessage 并调用渠道的 send 方法
            from backend.modules.channels.base import OutboundMessage
            await channel_instance.send(
                OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=message
                )
            )

            logger.info(f"Delivered to {channel}:{chat_id}")

        except Exception as e:
            logger.error(f"Failed to deliver: {e}")

    async def _save_greeting_to_session(
        self,
        channel: str,
        chat_id: str,
        greeting: str,
    ):
        """将问候语保存到会话历史中

        为什么要保存？AI 主动发了问候，如果不记录到会话历史，
        用户回复时 AI 看不到自己之前说了什么，会导致对话不连贯。
        """
        try:
            from backend.database import get_db_session_factory
            from backend.models.message import Message

            session_id = await self._get_or_create_session(channel, chat_id)

            db_factory = get_db_session_factory()
            async with db_factory() as db:
                # 只保存 assistant 消息（问候是 AI 主动发起的，没有对应的 user 消息）
                message = Message(
                    session_id=session_id,
                    role="assistant",
                    content=greeting,
                )
                db.add(message)
                await db.commit()

                logger.info(f"Greeting saved to session {session_id}")

        except Exception as e:
            logger.error(f"Failed to save greeting to session: {e}")

    async def _get_or_create_session(self, channel: str, chat_id: str) -> str:
        """获取或创建频道会话

        会话命名规则："{channel}:{chat_id}"，如 "feishu:ou_abc123"。
        同一个 channel+chat_id 复用同一个会话，保持对话连续性。
        逻辑与 handler.py 中渠道消息处理的会话查找一致。
        """
        from backend.database import get_db_session_factory
        from backend.models.session import Session
        from sqlalchemy import select
        import uuid

        session_name = f"{channel}:{chat_id}"
        db_factory = get_db_session_factory()

        async with db_factory() as db:
            # 按名称查找已有会话（取最新的一个）
            result = await db.execute(
                select(Session)
                .where(Session.name == session_name)
                .order_by(Session.created_at.desc())
                .limit(1)
            )
            session = result.scalar_one_or_none()

            if session:
                return session.id

            # 不存在则创建新会话
            session = Session(id=str(uuid.uuid4()), name=session_name)
            db.add(session)
            await db.commit()
            await db.refresh(session)
            logger.info(f"Created session {session.id} for {session_name}")
            return session.id

    async def _save_messages_to_db(self, session_id: str, user_message: str, ai_response: str):
        """将定时任务的一问一答保存到数据库

        虽然定时任务没有真实的"用户消息"，但为了保持会话历史的一致性，
        把定时任务的 message（提示词）作为 user 消息、AI 回复作为 assistant 消息保存。
        """
        try:
            from backend.database import get_db_session_factory
            from backend.models.message import Message

            db_factory = get_db_session_factory()
            async with db_factory() as db:
                # 保存 "用户消息"（实际是定时任务的提示词 / message 字段）
                db.add(Message(
                    session_id=session_id,
                    role="user",
                    content=user_message,
                ))

                # 保存 AI 响应
                db.add(Message(
                    session_id=session_id,
                    role="assistant",
                    content=ai_response,
                ))

                await db.commit()
                logger.debug(f"Saved cron messages to session {session_id}")

        except Exception as e:
            logger.error(f"Failed to save cron messages to DB: {e}")


