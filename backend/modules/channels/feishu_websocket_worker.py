"""飞书 WebSocket 独立进程（子进程）

在完全独立的进程中运行飞书 WebSocket 连接，避免事件循环冲突。
使用 multiprocessing.Queue 进行进程间通信。

为什么需要独立进程？
飞书 SDK 的 lark.ws.Client.start() 内部会创建自己的事件循环，
与主进程的 asyncio 事件循环冲突，所以必须用 multiprocessing.Process 隔离。

数据流：飞书服务器 → WebSocket → on_message() → Queue.put() → 主进程读取
"""

import json
import os
import signal
import sys

from loguru import logger

# Worker 进程独立的日志配置（子进程不继承主进程的日志配置，需要单独设置）
logger.remove()
logger.add(
    "data/logs/feishu_worker_{time}.log",
    rotation="1 day",
    retention="7 days",
    level="INFO",
)
logger.add(sys.stderr, level="INFO")

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
except ImportError:
    logger.error("lark-oapi not installed")
    sys.exit(1)


class FeishuWebSocketWorker:
    """飞书 WebSocket Worker

    在独立进程中运行 WebSocket 连接，接收消息后通过 Queue 传递给主进程。
    """

    def __init__(self, app_id: str, app_secret: str, message_queue):
        self.app_id = app_id
        self.app_secret = app_secret
        self.message_queue = message_queue  # multiprocessing.Queue，与主进程共享
        self.ws_client = None               # lark.ws.Client 实例
        self._running = False
        logger.info(f"Worker initialized (PID: {os.getpid()})")

    # ------------------------------------------------------------------
    # 消息处理
    # ------------------------------------------------------------------

    def on_message(self, data: P2ImMessageReceiveV1) -> None:
        """消息处理器 - 飞书 SDK 收到消息事件时回调此方法。

        将飞书 SDK 的事件对象转为普通 dict（因为跨进程通信只能传可序列化的数据），
        然后放入 Queue 供主进程读取。
        """
        try:
            event = data.event
            message = event.message
            sender = event.sender

            # 将 SDK 事件对象转为可序列化的 dict
            msg_data = {
                "type": "message",           # 消息类型标记（区分 status/error 等控制消息）
                "message_id": message.message_id,
                "sender_id": sender.sender_id.open_id if sender.sender_id else "unknown",
                "chat_id": message.chat_id,
                "chat_type": message.chat_type,   # "p2p"（私聊）或 "group"（群聊）
                "msg_type": message.message_type,  # "text" / "image" / "audio" 等
                "content": message.content,        # 消息内容（JSON 字符串）
            }

            try:
                self.message_queue.put_nowait(msg_data)  # 非阻塞放入队列
                logger.info(f"Message queued: {msg_data['message_id']}")
            except Exception as e:
                logger.warning(f"Queue full, message dropped: {e}")  # 队列已满（maxsize=1000）

        except Exception as e:
            logger.error(f"Error processing message: {e}")
            try:
                # 将错误信息也放入队列，通知主进程
                self.message_queue.put_nowait({"type": "error", "error": str(e)})
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动 WebSocket 连接。

        此方法是阻塞的：ws_client.start() 内部会维持 WebSocket 长连接，
        直到连接断开或被 stop() 终止才会返回。
        """
        logger.info(f"Starting Feishu WebSocket worker (app: {self.app_id[:12]}...)")

        # 通过队列通知主进程：子进程已启动
        try:
            self.message_queue.put_nowait({"type": "status", "message": "Worker started"})
        except Exception:
            pass

        # 1. 创建事件处理器：注册 on_message 回调，接收 IM 消息事件
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")  # 两个空字符串：加密 key 和验证 token（WebSocket 模式不需要）
            .register_p2_im_message_receive_v1(self.on_message)  # 注册消息接收回调
            .build()
        )

        # 2. 创建 WebSocket 客户端
        self.ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        self._running = True
        logger.info("WebSocket connecting...")

        try:
            self.message_queue.put_nowait({"type": "status", "message": "WebSocket connecting..."})
        except Exception:
            pass

        # 3. 启动 WebSocket 连接（阻塞调用，收到消息时触发 on_message 回调）
        try:
            self.ws_client.start()
        except KeyboardInterrupt:
            logger.info("Received interrupt signal")
            self.stop()
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            try:
                self.message_queue.put_nowait({"type": "error", "error": f"WebSocket error: {e}"})
            except Exception:
                pass
            self.stop()
            raise

    def stop(self) -> None:
        """停止 WebSocket 连接。"""
        if self._running:
            logger.info("Stopping WebSocket worker...")
            self._running = False
            if self.ws_client:
                try:
                    self.ws_client.stop()
                except Exception as e:
                    logger.error(f"Error stopping WebSocket: {e}")
            logger.info("WebSocket worker stopped")


# ------------------------------------------------------------------
# 进程入口
# ------------------------------------------------------------------


def run_worker(app_id: str, app_secret: str, message_queue) -> None:
    """Worker 进程入口函数。

    由 FeishuChannel.start() 中的 ctx.Process(target=run_worker, ...) 调用。
    """
    logger.info(f"Worker process starting (PID: {os.getpid()}, app: {app_id[:12]}...)")

    worker = FeishuWebSocketWorker(app_id, app_secret, message_queue)

    # 注册信号处理器：主进程 terminate/kill 时能优雅停止 WebSocket 连接
    def _signal_handler(signum, frame):
        logger.info(f"Received signal {signum}")
        worker.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)  # 对应主进程的 terminate()
    signal.signal(signal.SIGINT, _signal_handler)   # 对应 Ctrl+C

    try:
        worker.start()  # 阻塞调用，直到 WebSocket 断开
    except Exception as e:
        logger.error(f"Worker error: {e}")
        import traceback

        logger.error(traceback.format_exc())
        sys.exit(1)


# 兼容命令行调用（用于测试）
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python feishu_websocket_worker.py <app_id> <app_secret>")
        sys.exit(1)

    from multiprocessing import get_context

    ctx = get_context("spawn")
    test_queue = ctx.Queue(maxsize=1000)
    logger.info("Running in standalone test mode")
    run_worker(sys.argv[1], sys.argv[2], test_queue)
