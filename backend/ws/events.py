"""WebSocket 消息事件处理

实现消息事件的处理逻辑，包括：
- 消息接收和验证
- Agent 处理集成
- 流式响应推送
- 工具调用通知
- 错误处理
"""

import asyncio
from typing import Any

from fastapi import WebSocket
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.modules.agent.loop import AgentLoop
from backend.modules.session.manager import SessionManager
from backend.ws.connection import (
    ClientMessage,
    connection_manager,
    send_error,
    send_message_chunk,
    send_message_complete,
    send_tool_call,
    send_tool_result,
)


def _friendly_processing_error(raw: str) -> str:
    """将原始处理错误转换为用户友好提示

    原始错误信息可能包含技术细节（如 HTTP 状态码、异常堆栈），
    不适合直接展示给用户，这里根据关键词匹配转为中文友好提示。
    """
    lower = raw.lower()
    if any(k in lower for k in ("429", "余额", "quota", "rate limit")):
        return "AI 服务配额不足，请检查 API 账户余额。"        # API 调用超限
    if any(k in lower for k in ("401", "unauthorized", "api_key", "authentication")):
        return "API 认证失败，请检查密钥配置。"                 # API Key 无效或过期
    if any(k in lower for k in ("timeout", "connection", "network")):
        return "网络连接异常，请稍后重试。"                     # 网络问题
    return f"消息处理出错，请稍后重试。"                        # 兜底：未匹配到的其他错误


# ============================================================================
# Message Event Handlers
# ============================================================================


async def handle_message_event(
    connection_id: str,
    message: ClientMessage,
    agent_loop: AgentLoop,
    db: AsyncSession,
) -> None:
    """处理客户端消息事件（前端发送聊天消息时调用）

    处理流程：
    1. 获取取消令牌（用于用户中断生成）
    2. 验证会话存在性 → session_manager.get_session(session_id)
    3. 保存用户消息到数据库 → session_manager.add_message(role="user")
    4. 加载历史消息（最近 50 条）→ session_manager.get_messages(limit=50)
    5. 创建 BufferedStreamingHandler（缓冲 10 字符 / 10ms）
    6. 调用 AgentLoop 处理，流式输出
       async for chunk in agent_loop.process_message(...):
           if cancel_token.is_cancelled: break
           assistant_content += chunk
           await streaming_handler.write(chunk)  ← 写入缓冲区，推送到前端
    7. flush 剩余缓冲内容
    8. 保存 AI 回复到数据库 → session_manager.add_message(role="assistant")
    9. 回填工具调用记录的 message_id
    10. 发送 message_complete 通知

    Args:
        connection_id: 连接 ID
        message: 客户端消息
        agent_loop: Agent 循环实例
        db: 数据库会话
    """
    session_id = message.session_id
    content = message.content

    logger.info(
        f"收到消息 - 连接:{connection_id}, 会话:{session_id}, 内容:{content[:50]}..."
    )

    try:
        # ── 步骤 1：获取取消令牌 ──
        # 每个 session 对应一个 CancellationToken，用户点「停止生成」时会标记它
        # Agent 循环每次迭代会检查 cancel_token.is_cancelled，为 True 就退出
        from backend.ws.connection import get_cancel_token, cleanup_cancel_token
        cancel_token = get_cancel_token(session_id)

        # ── 步骤 2：验证会话存在性 ──
        # 从数据库查询 session 记录，不存在则返回错误（可能是无效/过期的 session_id）
        session_manager = SessionManager(db)
        session = await session_manager.get_session(session_id)

        if session is None:
            logger.error(f"会话不存在: {session_id}")
            await send_error(
                session_id,
                f"Session '{session_id}' not found",
                "SESSION_NOT_FOUND",
            )
            return

        logger.info(f"会话验证通过: {session_id}")

        # ── 步骤 3：保存用户消息到数据库 ──
        # 先持久化用户消息，确保即使后续处理失败，用户输入也不会丢失
        user_message = await session_manager.add_message(
            session_id=session_id,
            role="user",
            content=content,
        )

        if user_message is None:
            logger.error(f"保存用户消息失败")
            await send_error(
                session_id,
                "Failed to save user message",
                "DATABASE_ERROR",
            )
            return

        logger.info(f"用户消息已保存: ID={user_message.id}")

        # ── 步骤 4：加载历史消息 ──
        # 取最近 50 条消息作为上下文，Web 前端不做摘要压缩（与渠道一样只是截断）
        messages = await session_manager.get_messages(
            session_id=session_id,
            limit=50,
        )

        logger.info(f"加载历史消息: {len(messages)} 条")

        # 构建上下文：排除刚添加的用户消息（messages[-1]），因为当前消息会单独传给 Agent
        context = []
        for msg in messages[:-1]:
            context.append({
                "role": msg.role,
                "content": msg.content,
            })

        logger.info(f"开始AI处理，上下文消息数: {len(context)}")

        # ── 步骤 5：创建缓冲流式处理器 ──
        # BufferedStreamingHandler 会攒够 buffer_size 个字符或超过 flush_interval_ms 毫秒后，
        # 才调用 send_message_chunk() 推送到前端，减少 WebSocket 发送次数
        assistant_content = ""

        from backend.ws.streaming import BufferedStreamingHandler

        streaming_handler = BufferedStreamingHandler(
            session_id=session_id,
            buffer_size=10,       # 攒够 10 个字符就发送（较小 → 更实时）
            flush_interval_ms=10, # 超过 10ms 没新数据也发送（较小 → 更实时）
        )

        # ── 步骤 6：调用 AgentLoop 处理，流式输出 ──
        # agent_loop.process_message() 是一个异步生成器，每产生一个 chunk（文本片段）就 yield
        # 内部流程：构建 prompt → 调用 LLM → 解析 tool_call → 执行工具 → 继续生成
        chunk_count = 0
        async for chunk in agent_loop.process_message(
            message=content,         # 当前用户输入
            session_id=session_id,   # 会话 ID（用于工具调用通知等）
            context=context,         # 历史消息上下文
            cancel_token=cancel_token,  # 取消令牌（用户可中断）
        ):
            # 每次循环先检查取消标记，用户点「停止」后尽快退出
            if cancel_token.is_cancelled:
                logger.info(f"处理被取消: {session_id}")
                await streaming_handler.write("\n\n[已停止生成]")
                await streaming_handler.flush()
                break

            # 累积完整回复（最后要存数据库），同时写入缓冲区（定时推送到前端）
            assistant_content += chunk
            await streaming_handler.write(chunk)  # 写入缓冲区，满了会自动 flush 到前端
            chunk_count += 1

            if chunk_count % 100 == 0:
                logger.debug(f"已发送 {chunk_count} 个chunk")

        logger.info(f"AI处理完成，共发送 {chunk_count} 个chunk，总长度: {len(assistant_content)}")

        # ── 步骤 7：flush 剩余缓冲内容 ──
        # 生成结束后，缓冲区可能还有不足 buffer_size 的残余字符，强制推送
        await streaming_handler.flush()

        stats = streaming_handler.get_stats()
        logger.debug(f"流式响应统计: {stats}")

        # ── 步骤 8：保存 AI 回复到数据库 ──
        if assistant_content:
            assistant_message = await session_manager.add_message(
                session_id=session_id,
                role="assistant",
                content=assistant_content,  # 完整的 AI 回复文本
            )

            logger.info(f"助手消息已保存到数据库: ID={assistant_message.id}")

            # ── 步骤 9：回填工具调用记录的 message_id ──
            # Agent 处理过程中产生的工具调用记录（tool_call / tool_result）此时还没有
            # 关联到哪条 assistant 消息，这里用刚保存的 message_id 回填关联关系
            try:
                from backend.modules.tools.conversation_history import get_conversation_history
                conversation_history = get_conversation_history()
                await conversation_history.backfill_message_id(
                    session_id=session_id,
                    message_id=assistant_message.id,
                )
            except Exception as e:
                logger.warning(f"Failed to backfill message_id: {e}")

            # ── 步骤 10：发送 message_complete 通知 ──
            # 告诉前端本轮回复已全部发送完毕，前端收到后停止"正在输入"动画
            await send_message_complete(session_id, "")
        else:
            logger.warning(f"AI响应为空")
            await send_message_complete(session_id, "")

        logger.info(f"消息处理完成 (会话 {session_id})")

        # 清理取消令牌：本轮处理结束，移除令牌，下次请求会创建新的
        cleanup_cancel_token(session_id)

    except Exception as e:
        logger.exception(f"处理消息事件时出错: {e}")
        # 将原始错误转为用户友好提示（如 429 → "API 配额不足"）
        friendly = _friendly_processing_error(str(e))
        await send_error(
            session_id,
            friendly,
            "PROCESSING_ERROR",
        )
        # 异常路径也要清理取消令牌，避免内存泄漏
        from backend.ws.connection import cleanup_cancel_token
        cleanup_cancel_token(session_id)


async def handle_tool_execution(
    session_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    agent_loop: AgentLoop,
) -> None:
    """处理工具执行事件

    Args:
        session_id: 会话 ID
        tool_name: 工具名称
        arguments: 工具参数
        agent_loop: Agent 循环实例
    """
    from backend.ws.tool_notifications import execute_tool_with_notifications

    try:
        logger.info(f"执行工具 {tool_name} (会话 {session_id})")

        # execute_tool_with_notifications 会在执行前后自动发送 tool_call / tool_result 通知到前端
        # 内部调用 agent_loop.execute_tool 执行实际的工具逻辑
        result = await execute_tool_with_notifications(
            session_id=session_id,
            tool_name=tool_name,
            arguments=arguments,
            executor=agent_loop.execute_tool,  # 实际执行工具的函数
        )

        logger.info(f"工具执行完成: {tool_name}")

    except Exception as e:
        logger.exception(f"工具执行失败: {e}")
        # 不需要额外发送错误通知，execute_tool_with_notifications 内部已处理


async def handle_ping_event(connection_id: str) -> None:
    """处理心跳事件 - 前端定时发送 ping，后端回复 pong，用于保活和检测连接状态

    Args:
        connection_id: 连接 ID
    """
    from backend.ws.connection import ServerMessage

    # 收到 ping 回复 pong，前端据此判断连接是否存活
    await connection_manager.send_message(
        connection_id,
        ServerMessage(type="pong"),
    )


async def handle_subscribe_event(
    connection_id: str,
    session_id: str,
) -> None:
    """处理订阅事件 - 前端切换会话时，将当前 WebSocket 连接绑定到新会话

    场景：用户在前端侧边栏点击另一个会话，前端发送 subscribe 事件，
    后端将这个 connection_id 绑定到新的 session_id，后续该会话的消息就能推送到这个连接。

    Args:
        connection_id: 连接 ID
        session_id: 会话 ID
    """
    await connection_manager.bind_session(connection_id, session_id)
    logger.info(f"连接 {connection_id} 订阅会话 {session_id}")


async def handle_unsubscribe_event(
    connection_id: str,
    session_id: str,
) -> None:
    """处理取消订阅事件 - 前端离开会话时，解除绑定

    Args:
        connection_id: 连接 ID
        session_id: 会话 ID
    """
    # TODO: 当前 ConnectionManager 没有 unbind_session 方法，
    # 所以取消订阅暂时只记录日志。实际影响不大：bind_session 会覆盖旧绑定
    logger.info(f"连接 {connection_id} 取消订阅会话 {session_id}")


# ============================================================================
# Event Router
# ============================================================================


async def route_event(
    connection_id: str,
    event_type: str,
    event_data: dict[str, Any],
    agent_loop: AgentLoop,
    db: AsyncSession,
) -> None:
    """事件路由器 - 根据 type 字段分发到对应的处理函数

    前端发送的 JSON 消息格式: {"type": "message"|"ping"|"subscribe"|..., "sessionId": "...", ...}
    这里根据 type 字段路由到不同的 handler。

    Args:
        connection_id: 连接 ID
        event_type: 事件类型（从 JSON 的 type 字段提取）
        event_data: 完整的事件数据 dict
        agent_loop: Agent 循环实例
        db: 数据库会话
    """
    try:
        if event_type == "message":
            # 用户发送聊天消息 → 走完整的 Agent 处理流程
            message = ClientMessage(**event_data)
            await handle_message_event(connection_id, message, agent_loop, db)

        elif event_type == "tool_execute":
            # 前端主动请求执行工具（较少见，通常工具调用由 Agent 自动发起）
            session_id = event_data.get("sessionId")
            tool_name = event_data.get("tool")
            arguments = event_data.get("arguments", {})

            if not session_id or not tool_name:
                await send_error(
                    session_id or "",
                    "Missing required fields: sessionId, tool",
                    "INVALID_EVENT",
                )
                return

            await handle_tool_execution(session_id, tool_name, arguments, agent_loop)

        elif event_type == "ping":
            # 心跳保活：前端定时 ping，后端 pong
            await handle_ping_event(connection_id)

        elif event_type == "subscribe":
            # 切换会话：前端切到某个会话，绑定当前连接
            session_id = event_data.get("sessionId")
            if not session_id:
                logger.warning("订阅事件缺少 sessionId")
                return

            await handle_subscribe_event(connection_id, session_id)

        elif event_type == "unsubscribe":
            # 离开会话：前端离开某个会话，解除绑定
            session_id = event_data.get("sessionId")
            if not session_id:
                logger.warning("取消订阅事件缺少 sessionId")
                return

            await handle_unsubscribe_event(connection_id, session_id)

        else:
            logger.warning(f"未知事件类型: {event_type}")
            await send_error(
                event_data.get("sessionId", ""),
                f"Unknown event type: {event_type}",
                "UNKNOWN_EVENT",
            )

    except Exception as e:
        logger.exception(f"路由事件时出错: {e}")
        await send_error(
            event_data.get("sessionId", ""),
            f"Event routing failed: {str(e)}",
            "ROUTING_ERROR",
        )


# ============================================================================
# WebSocket Event Loop
# ============================================================================


async def websocket_event_loop(
    websocket: WebSocket,
    connection_id: str,
    agent_loop: AgentLoop,
) -> None:
    """WebSocket 事件循环 - 由 connection.py 的 handle_websocket() 调用

    这是 WebSocket 连接的主循环：不断接收前端消息 → 解析 JSON → route_event 分发处理。
    循环一直运行，直到客户端断开或出现不可恢复的错误。

    Args:
        websocket: WebSocket 连接
        connection_id: 连接 ID
        agent_loop: Agent 循环实例
    """
    from fastapi import WebSocketDisconnect
    import json
    from pydantic import ValidationError

    try:
        while True:
            # 每次循环先检查连接状态（客户端可能已关闭浏览器标签页）
            if websocket.client_state.name != "CONNECTED":
                logger.info(f"WebSocket 连接已关闭 (状态: {websocket.client_state.name}): {connection_id}")
                break

            try:
                # 阻塞等待前端发来的消息（这里会 await，让出事件循环给其他协程）
                data = await websocket.receive_text()
            except RuntimeError as e:
                if "not connected" in str(e).lower():
                    logger.info(f"WebSocket 连接已断开: {connection_id}")
                    break
                raise

            # 解析 JSON 并路由到对应的事件处理器
            try:
                message_dict = json.loads(data)
                event_type = message_dict.get("type")  # "message" / "ping" / "subscribe" / "stop" 等
                event_data = message_dict

                if not event_type:
                    await send_error(
                        "",
                        "Missing event type",
                        "INVALID_EVENT",
                    )
                    continue

                # 每个事件都需要独立的数据库会话（事务隔离）
                # async for ... break 是 FastAPI 依赖注入获取 db session 的惯用写法
                async for db in get_db():
                    try:
                        await route_event(
                            connection_id,
                            event_type,
                            event_data,
                            agent_loop,
                            db,
                        )
                    finally:
                        await db.close()  # 确保数据库连接归还连接池
                    break  # 只需要一个 db session，取到后立即 break

            except (json.JSONDecodeError, ValidationError) as e:
                # JSON 解析失败或消息格式不符合 Pydantic 模型，通知前端
                logger.warning(f"无效的消息格式: {e}")
                await send_error(
                    "",
                    "Invalid message format",
                    "INVALID_MESSAGE",
                )

    except WebSocketDisconnect:
        # 正常断开：客户端主动关闭连接（关闭标签页、刷新页面等）
        logger.info(f"客户端断开连接: {connection_id}")
    except RuntimeError as e:
        # FastAPI/Starlette 在连接异常时可能抛出 RuntimeError
        if "not connected" in str(e).lower() or "accept" in str(e).lower():
            logger.info(f"WebSocket 连接已关闭: {connection_id}")
        else:
            logger.exception(f"WebSocket 运行时错误: {e}")
    except Exception as e:
        logger.exception(f"WebSocket 事件循环错误: {e}")
