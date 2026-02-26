"""飞书频道模块

使用 lark-oapi SDK 通过 WebSocket 长连接接收事件，无需公网 IP。
WebSocket 连接运行在独立子进程中，通过 multiprocessing.Queue 通信。
支持文本、图片、文件消息，以及 markdown + 表格的卡片消息。
"""

import asyncio
import json
import re
from collections import OrderedDict
from multiprocessing import Process, Queue
from pathlib import Path
from typing import Any

from loguru import logger

from backend.modules.channels.base import BaseChannel, OutboundMessage

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        CreateImageRequest,
        CreateImageRequestBody,
        Emoji,
    )

    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None
    Emoji = None

# 消息类型显示映射
_MSG_TYPE_MAP = {
    "image": "[图片]",
    "audio": "[语音]",
    "file": "[文件]",
    "sticker": "[表情]",
}


class FeishuChannel(BaseChannel):
    """飞书频道

    通过独立子进程运行 WebSocket 连接，避免事件循环冲突。
    支持文本、图片、文件消息收发，出站消息使用卡片格式。
    """

    name = "feishu"

    # markdown 表格匹配正则
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)"
        r"(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)"
        r"(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    def __init__(self, config: Any):
        super().__init__(config)
        self._client = None                # 飞书 API 客户端（用于发送消息、下载图片等）
        self._ws_process: Process | None = None       # WebSocket 子进程
        self._message_queue: Queue | None = None      # 进程间通信队列
        self._processed_ids: OrderedDict[str, None] = OrderedDict()  # 消息去重缓存（最多 1000 条）
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动飞书机器人（WebSocket 子进程模式）。"""
        if not FEISHU_AVAILABLE:
            logger.error("Feishu SDK not installed. Run: pip install lark-oapi")
            return

        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return

        self._running = True
        self._loop = asyncio.get_running_loop()

        # 步骤1: 创建飞书 API 客户端（用于发送消息、下载图片等，运行在主进程）
        self._client = (
            lark.Client.builder()
            .app_id(self.config.app_id)
            .app_secret(self.config.app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )

        # 步骤2: 创建进程间通信队列（子进程收到消息后放入队列，主进程从队列读取）
        from multiprocessing import get_context

        ctx = get_context("spawn")  # 使用 spawn 模式，避免 fork 导致的事件循环问题
        self._message_queue = ctx.Queue(maxsize=1000)

        # 步骤3: 启动 WebSocket 子进程（飞书 SDK 的 WebSocket 会创建自己的事件循环，必须隔离到独立进程）
        from backend.modules.channels.feishu_websocket_worker import run_worker

        self._ws_process = ctx.Process(
            target=run_worker,  # 子进程入口函数
            args=(self.config.app_id, self.config.app_secret, self._message_queue),
            daemon=True,        # 主进程退出时自动终止子进程
            name="feishu-websocket-worker",
        )
        self._ws_process.start()
        logger.info(f"Feishu WebSocket worker started (PID: {self._ws_process.pid})")

        # 步骤4: 启动消息队列读取任务（异步轮询 Queue，将消息分发到 _process_message）
        asyncio.create_task(self._read_ws_messages())

        # 步骤5: 保持 start() 不返回（ChannelManager 用 start() 是否返回来判断渠道是否断开）
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """停止飞书机器人。"""
        self._running = False  # 通知 start() 中的 while 循环退出

        # 1. 停止 WebSocket 子进程：先 terminate，5秒超时后 kill
        if self._ws_process:
            try:
                logger.info("Terminating WebSocket worker process...")
                self._ws_process.terminate()
                self._ws_process.join(timeout=5)
                if self._ws_process.is_alive():
                    logger.warning("Worker did not terminate, killing...")
                    self._ws_process.kill()
                    self._ws_process.join(timeout=2)
                logger.info("WebSocket worker process stopped")
            except Exception as e:
                logger.error(f"Error stopping WebSocket process: {e}")

        # 2. 清空并关闭消息队列
        if self._message_queue:
            try:
                while not self._message_queue.empty():
                    try:
                        self._message_queue.get_nowait()
                    except Exception:
                        break
                self._message_queue.close()
                self._message_queue.join_thread()
            except Exception as e:
                logger.debug(f"Error cleaning up queue: {e}")

        logger.info("Feishu bot stopped")

    # ------------------------------------------------------------------
    # 消息队列读取
    # ------------------------------------------------------------------

    async def _read_ws_messages(self) -> None:
        """从 WebSocket 子进程的队列读取消息。"""
        if not self._message_queue:
            return

        logger.info("Message queue reader started")
        try:
            while self._running:
                try:
                    # Queue.get() 是阻塞操作，用 run_in_executor 放到线程池执行，避免阻塞 asyncio 事件循环
                    msg_data = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: self._message_queue.get(timeout=1.0)
                    )
                    if msg_data and msg_data.get("type") == "message":
                        await self._process_message(msg_data)
                except Exception as e:
                    # Queue 超时是正常的（没有新消息），不需要打日志
                    if "Empty" not in str(e) and "timeout" not in str(e).lower():
                        logger.debug(f"Queue read error: {e}")
                    await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Message queue reader error: {e}")
        finally:
            logger.info("Message queue reader stopped")

    # ------------------------------------------------------------------
    # 入站消息处理
    # ------------------------------------------------------------------

    async def _process_message(self, msg_data: dict) -> None:
        """处理来自 WebSocket 子进程的消息。"""
        try:
            message_id = msg_data["message_id"]

            # 消息去重：用 OrderedDict 缓存已处理的消息 ID，最多保留 1000 条（FIFO 淘汰）
            if message_id in self._processed_ids:
                return
            self._processed_ids[message_id] = None
            while len(self._processed_ids) > 1000:
                self._processed_ids.popitem(last=False)  # 淘汰最早的

            sender_id = msg_data["sender_id"]
            chat_id = msg_data["chat_id"]
            chat_type = msg_data["chat_type"]   # "p2p"（私聊）或 "group"（群聊）
            msg_type = msg_data["msg_type"]     # "text" / "image" / "audio" / "file" 等

            # 收到消息后自动点赞，让用户知道 Bot 已收到
            await self._add_reaction(message_id, "THUMBSUP")

            media_files = []

            # 根据消息类型分别处理
            if msg_type == "text":
                # 飞书文本消息的 content 是 JSON 格式：{"text": "实际内容"}
                try:
                    content = json.loads(msg_data["content"]).get("text", "")
                except json.JSONDecodeError:
                    content = msg_data["content"] or ""
            elif msg_type == "image":
                # 图片消息：下载到本地，生成图片分析命令交给 Agent
                content, media_files = await self._handle_image_message(
                    msg_data["content"], message_id
                )
            else:
                # 其他类型：转为占位文本（如 [语音]、[文件]）
                content = _MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")

            if not content:
                return

            # 群聊回复到群，私聊回复到个人
            reply_to = chat_id if chat_type == "group" else sender_id
            # 调用基类的 _handle_message：鉴权 → 封装 InboundMessage → 回调到 ChannelManager
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_files or None,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "msg_type": msg_type,
                },
            )
        except Exception as e:
            logger.error(f"Error processing Feishu message: {e}")

    async def _handle_image_message(
        self, raw_content: str, message_id: str
    ) -> tuple[str, list[str]]:
        """处理图片消息，返回 (content, media_files)。"""
        media_files = []
        try:
            image_key = json.loads(raw_content).get("image_key")
            if image_key:
                logger.info(f"Downloading image: {image_key}")
                image_path = await self._download_image(image_key, message_id)
                if image_path:
                    media_files.append(image_path)
                    content = (
                        f"用户发送了图片（{image_path}），请执行："
                        f"python skills/image-analysis/scripts/vision.py "
                        f'analyze --image {image_path} --prompt "描述图片内容"'
                    )
                    logger.info(f"Image ready: {image_path}")
                else:
                    content = "[图片下载失败]"
            else:
                content = "[图片]"
        except Exception as e:
            logger.error(f"Failed to process image: {e}")
            content = "[图片处理失败]"
        return content, media_files

    # ------------------------------------------------------------------
    # 表情回应
    # ------------------------------------------------------------------

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """添加表情回应到消息（非阻塞）。"""
        if not self._client or not Emoji:
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        """同步添加表情回应（在线程池中运行）。"""
        try:
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message_reaction.create(request)
            if not response.success():
                logger.warning(f"Failed to add reaction: code={response.code}, msg={response.msg}")
            else:
                logger.debug(f"Added {emoji_type} reaction to {message_id}")
        except Exception as e:
            logger.warning(f"Error adding reaction: {e}")

    # ------------------------------------------------------------------
    # 图片下载
    # ------------------------------------------------------------------

    async def _download_image(self, image_key: str, message_id: str) -> str | None:
        """下载图片并保存到本地。"""
        try:
            from lark_oapi.api.im.v1 import GetMessageResourceRequest

            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(image_key)
                .type("image")
                .build()
            )
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, lambda: self._client.im.v1.message_resource.get(request)
            )

            if not response.success():
                logger.error(f"Failed to download image: {response.msg}")
                return await self._download_image_fallback(image_key, message_id)

            temp_dir = Path("data/temp/images")
            temp_dir.mkdir(parents=True, exist_ok=True)
            image_path = temp_dir / f"{message_id}.jpg"

            with open(image_path, "wb") as f:
                f.write(response.file.read())

            logger.info(f"Image downloaded: {image_path}")
            return str(image_path)
        except Exception as e:
            logger.error(f"Error downloading image: {e}")
            return await self._download_image_fallback(image_key, message_id)

    async def _download_image_fallback(self, image_key: str, message_id: str) -> str | None:
        """备用下载方法：使用 GetImageRequest。"""
        try:
            from lark_oapi.api.im.v1 import GetImageRequest

            logger.info(f"Trying fallback download: {image_key}")
            request = GetImageRequest.builder().image_key(image_key).build()

            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, lambda: self._client.im.v1.image.get(request)
            )

            if not response.success():
                logger.error(f"Fallback download failed: {response.msg}")
                return None

            temp_dir = Path("data/temp/images")
            temp_dir.mkdir(parents=True, exist_ok=True)
            image_path = temp_dir / f"{message_id}.jpg"

            with open(image_path, "wb") as f:
                f.write(response.file.read())

            logger.info(f"Image downloaded (fallback): {image_path}")
            return str(image_path)
        except Exception as e:
            logger.error(f"Fallback download failed: {e}")
            return None

    # ------------------------------------------------------------------
    # 卡片构建
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_md_table(table_text: str) -> dict | None:
        """解析 markdown 表格为飞书表格元素。"""
        lines = [line.strip() for line in table_text.strip().split("\n") if line.strip()]
        if len(lines) < 3:
            return None

        def _split(line: str) -> list[str]:
            return [c.strip() for c in line.strip("|").split("|")]

        headers = _split(lines[0])
        rows = [_split(line) for line in lines[2:]]
        columns = [
            {"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
            for i, h in enumerate(headers)
        ]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [
                {f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))}
                for r in rows
            ],
        }

    def _build_card_elements(self, content: str) -> list[dict]:
        """将内容拆分为 markdown 段落 + 飞书原生表格元素，用于卡片消息。

        例如 Agent 回复：
            "以下是结果：\n| 城市 | 温度 |\n|---|---|\n| 北京 | 25℃ |\n更多信息请查看..."
        会被拆成 3 个元素：
            1. markdown 元素："以下是结果："
            2. table 元素：飞书原生表格（不是等宽字体文本）
            3. markdown 元素："更多信息请查看..."
        """
        elements = []
        last_end = 0  # 记录上一个表格结束的位置

        # 用正则找出内容中所有的 markdown 表格
        for m in self._TABLE_RE.finditer(content):
            # 表格前面的文字 → markdown 元素
            before = content[last_end : m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            # 表格本身 → 尝试解析为飞书原生 table 元素，解析失败则退回 markdown
            elements.append(
                self._parse_md_table(m.group(1))
                or {"tag": "markdown", "content": m.group(1)}
            )
            last_end = m.end()

        # 最后一个表格之后的剩余文字 → markdown 元素
        remaining = content[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        # 如果内容中没有表格，整体作为一个 markdown 元素
        return elements or [{"tag": "markdown", "content": content}]

    # ------------------------------------------------------------------
    # 出站消息发送
    # ------------------------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        """发送消息到飞书。"""
        if not self._client:
            logger.warning("Feishu client not initialized")
            return

        try:
            # 判断接收者类型：oc_ 开头是群聊 ID，否则是个人 open_id
            receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"

            if msg.media:
                await self._send_with_media(msg, receive_id_type)
            else:
                # 使用飞书卡片消息格式发送（支持 Markdown + 原生表格渲染）
                await self._send_card(msg.chat_id, msg.content, receive_id_type)
        except Exception as e:
            logger.error(f"Error sending Feishu message: {e}")

    async def _send_card(self, chat_id: str, content: str, receive_id_type: str) -> None:
        """发送卡片消息（支持 markdown + 表格）。"""
        # 将回复内容拆分为 markdown 段落 + 飞书原生表格元素
        elements = self._build_card_elements(content)
        card = {"config": {"wide_screen_mode": True}, "elements": elements}
        card_json = json.dumps(card, ensure_ascii=False)

        # 使用 msg_type="interactive" 发送卡片消息
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(card_json)
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)

        if not response.success():
            logger.error(
                f"Failed to send Feishu message: code={response.code}, "
                f"msg={response.msg}, log_id={response.get_log_id()}"
            )
        else:
            logger.debug(f"Feishu message sent to {chat_id}")

    async def _send_with_media(self, msg: OutboundMessage, receive_id_type: str) -> None:
        """发送带媒体文件的消息。"""
        try:
            if msg.content and msg.content.strip():
                await self._send_card(msg.chat_id, msg.content, receive_id_type)

            for media_path in msg.media:
                await self._send_media_file(msg.chat_id, media_path, receive_id_type)
        except Exception as e:
            logger.error(f"Error sending media: {e}")

    async def _send_media_file(
        self, chat_id: str, file_path: str, receive_id_type: str
    ) -> None:
        """根据文件类型发送图片或文件。"""
        if self._is_image_file(file_path):
            await self._send_image(chat_id, file_path, receive_id_type)
        else:
            await self._send_file(chat_id, file_path, receive_id_type)

    @staticmethod
    def _is_image_file(file_path: str) -> bool:
        """判断是否为图片文件。"""
        return Path(file_path).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}

    async def _send_image(
        self, chat_id: str, image_path: str, receive_id_type: str
    ) -> None:
        """上传并发送图片（两步：先上传获取 image_key，再用 image_key 发送消息）。"""
        try:
            image_file = Path(image_path)
            if not image_file.exists():
                logger.error(f"Image not found: {image_path}")
                return

            # 第一步：上传图片到飞书服务器，获取 image_key
            upload_request = (
                CreateImageRequest.builder()
                .request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(open(image_file, "rb"))
                    .build()
                )
                .build()
            )
            upload_response = self._client.im.v1.image.create(upload_request)

            if not upload_response.success():
                logger.error(f"Failed to upload image: {upload_response.msg}")
                return

            image_key = upload_response.data.image_key
            logger.info(f"Image uploaded: {image_key}")

            # 第二步：用 image_key 发送图片消息
            content = json.dumps({"image_key": image_key})
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("image")
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)

            if not response.success():
                logger.error(f"Failed to send image: {response.msg}")
            else:
                logger.info(f"Image sent to {chat_id}")
        except Exception as e:
            logger.error(f"Error sending image: {e}")

    async def _send_file(
        self, chat_id: str, file_path: str, receive_id_type: str
    ) -> None:
        """上传并发送文件（两步：先上传获取 file_key，再用 file_key 发送消息）。"""
        try:
            file = Path(file_path)
            if not file.exists():
                logger.error(f"File not found: {file_path}")
                return

            from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

            upload_request = (
                CreateFileRequest.builder()
                .request_body(
                    CreateFileRequestBody.builder()
                    .file_type("stream")
                    .file_name(file.name)
                    .file(open(file, "rb"))
                    .build()
                )
                .build()
            )
            upload_response = self._client.im.v1.file.create(upload_request)

            if not upload_response.success():
                logger.error(f"Failed to upload file: {upload_response.msg}")
                return

            file_key = upload_response.data.file_key
            logger.info(f"File uploaded: {file_key}")

            content = json.dumps({"file_key": file_key})
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("file")
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)

            if not response.success():
                logger.error(f"Failed to send file: {response.msg}")
            else:
                logger.info(f"File sent to {chat_id}")
        except Exception as e:
            logger.error(f"Error sending file: {e}")

    # ------------------------------------------------------------------
    # 连接测试
    # ------------------------------------------------------------------

    async def test_connection(self) -> dict[str, Any]:
        """测试飞书连接（获取 tenant_access_token 验证凭据）。

        不启动 WebSocket，仅通过 HTTP 请求验证 app_id 和 app_secret 是否有效。
        前端设置页面点击"测试连接"时调用。
        """
        # 前置校验：格式检查
        if not self.config.app_id or not self.config.app_secret:
            return {"success": False, "message": "App ID or App Secret not configured"}

        if not FEISHU_AVAILABLE:
            return {"success": False, "message": "Feishu SDK not installed"}

        if not self.config.app_id.startswith("cli_"):
            return {"success": False, "message": "Invalid App ID format (should start with 'cli_')"}
        if len(self.config.app_id) < 12:
            return {"success": False, "message": "Invalid App ID format (too short)"}
        if len(self.config.app_secret) < 24:
            return {"success": False, "message": "Invalid App Secret format (too short)"}

        try:
            import httpx

            # 请求飞书开放平台获取 tenant_access_token，验证凭据是否正确
            async with httpx.AsyncClient(timeout=5.0) as http_client:
                response = await http_client.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={
                        "app_id": self.config.app_id,
                        "app_secret": self.config.app_secret,
                    },
                )
                result = response.json()

                if result.get("code") == 0:
                    return {
                        "success": True,
                        "message": "Feishu credentials verified",
                        "bot_info": {
                            "app_id": self.config.app_id[:12] + "...",
                            "status": "credentials_verified",
                        },
                    }

                error_msg = result.get("msg", "Unknown error")
                error_code = result.get("code", "")
                if "99991663" in str(error_code) or "app_id" in error_msg.lower():
                    return {"success": False, "message": "Invalid App ID or App Secret"}
                return {"success": False, "message": f"Authentication failed: {error_msg}"}

        except asyncio.TimeoutError:
            return {"success": False, "message": "Connection timeout"}
        except Exception as e:
            error_msg = str(e)
            if any(k in error_msg.lower() for k in ("app_id", "app_secret", "99991663")):
                return {"success": False, "message": "Invalid App ID or App Secret"}
            return {"success": False, "message": f"Connection test failed: {error_msg}"}

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def display_name(self) -> str:
        return "Feishu"
