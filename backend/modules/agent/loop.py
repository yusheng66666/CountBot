"""Agent Loop - 核心 Agent 循环处理逻辑

本文件实现了 ReAct（Reasoning + Acting）模式的 Agent 主循环。

核心流程：
    用户消息 → 构建上下文 → 调用 LLM → LLM 返回文本或工具调用
        → 如果是文本：yield 给调用者（流式输出），循环结束
        → 如果是工具调用：执行工具 → 将结果加入消息列表 → 再次调用 LLM → ...

Java 类比：
    - AgentLoop 类似于一个无状态的 Service（不保存会话历史，每次调用独立处理）
    - process_message() 类似于 @Async 方法，返回的是 AsyncIterator 而非 CompletableFuture
    - 工具调用的重试机制类似于 Spring Retry 的 @Retryable
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any, AsyncIterator

from loguru import logger
from backend.modules.tools.conversation_history import get_conversation_history


class AgentLoop:
    """Agent 主循环类 - 处理消息、调用 LLM、执行工具、生成响应

    这是一个无状态处理器：不保存历史消息，不维护会话状态。
    每次调用 process_message() 都是独立的处理流程，
    历史消息由外部通过 context 参数传入。

    Java 类比：类似于 Spring 的 @Service 类 + @Scope("prototype")，
    每个 WebSocket 连接创建一个独立实例。
    """

    def __init__(
        self,
        provider,
        workspace: Path,
        tools,
        context_builder=None,
        session_manager=None,
        subagent_manager=None,
        model: str | None = None,
        max_iterations: int = 25,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ):
        # ---- 核心依赖 ----
        self.provider = provider              # LLM 提供商（如 LiteLLMProvider），负责调用大模型 API
        self.workspace = workspace            # 工作区路径，工具读写文件时的根目录
        self.tools = tools                    # 工具注册表（ToolRegistry），管理所有可用工具
        self.context_builder = context_builder  # 上下文构建器，负责组装系统提示词 + 历史消息
        self.session_manager = session_manager  # 会话管理器（CLI 模式使用，WebSocket 模式由外部管理）
        self.subagent_manager = subagent_manager  # 子代理管理器，支持 spawn 工具创建后台任务

        # ---- 模型参数 ----
        self.model = model                    # 模型名称（如 "gpt-4o"、"deepseek-chat"）
        self.temperature = temperature        # 生成随机性，0=确定性最高，1=最随机，0.7 是平衡值
        self.max_tokens = max_tokens          # 单次 LLM 回复的最大 token 数

        # ---- 安全限制 ----
        self.max_iterations = max_iterations  # ReAct 循环最大迭代次数（防止 AI 无限调用工具）
        self.max_retries = max_retries        # 单个工具执行失败时的最大重试次数
        self.retry_delay = retry_delay        # 两次重试之间的等待秒数

        logger.debug(
            f"AgentLoop initialized: workspace={workspace}, "
            f"max_iterations={max_iterations}, max_retries={max_retries}, "
            f"temperature={temperature}, max_tokens={max_tokens}"
        )

    # =========================================================================
    # 核心方法：process_message — ReAct 循环
    # =========================================================================

    async def process_message(
        self,
        message: str,
        session_id: str,
        context: list[dict[str, Any]] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        cancel_token=None,
    ) -> AsyncIterator[str]:
        """处理用户消息并生成流式响应

        这是一个异步生成器（async generator），使用 yield 逐块返回 LLM 的文本输出。
        调用者通过 async for 接收每个文本片段，实现"打字机"效果。

        Java 类比：类似于返回 Flux<String>（WebFlux 响应式流），
        调用者通过 subscribe 逐个接收元素。

        整体流程：
            1. 准备阶段：设置工具上下文、构建消息列表
            2. ReAct 循环：反复调用 LLM，直到 LLM 不再请求工具调用
            3. 收尾阶段：保存会话、记录审计日志
        """
        logger.info(f"Processing message for session {session_id}: {message[:50]}...")

        # =====================================================================
        # 阶段一：准备工作
        # =====================================================================

        # 1.1 设置工具注册表的会话上下文
        # 工具执行时需要知道"为哪个会话服务"，用于审计日志和结果路由
        if self.tools:
            self.tools.set_session_id(session_id)
            self.tools.set_channel(channel)

            # spawn 工具需要额外的上下文（用于创建子代理时继承会话信息）
            spawn_tool = self.tools.get_tool("spawn")
            if spawn_tool and hasattr(spawn_tool, 'set_context'):
                spawn_tool.set_context(session_id)

        # 1.2 构建消息列表（system prompt + 历史消息 + 当前用户消息）
        # 这个列表就是发给 LLM 的完整上下文，LLM 根据这些信息生成回复
        # Java 类比：类似于构建一个 List<ChatMessage>，包含 system、user、assistant 角色的消息
        if self.context_builder and context is not None:
            messages = self.context_builder.build_messages(
                history=context,
                current_message=message,
                media=media,
                channel=channel,
                chat_id=chat_id,
            )
        else:
            # 无 context_builder 时的降级处理（CLI 直接调用等简单场景）
            if context is None:
                context = []

            messages = list(context)
            messages.append({
                "role": "user",
                "content": message,
            })

        # 1.3 初始化循环控制变量
        iteration = 0           # 当前迭代轮次
        total_tool_calls = 0    # 累计工具调用次数
        final_content = ""      # 最终的完整回复文本（用于保存到数据库）

        # =====================================================================
        # 阶段二：ReAct 循环（核心）
        #
        # 循环逻辑：
        #   while 未达到最大迭代次数:
        #       调用 LLM（流式）
        #       if LLM 返回了工具调用:
        #           执行工具 → 结果加入 messages → 继续循环（让 LLM 看到工具结果）
        #       else:
        #           LLM 只返回了文本 → 循环结束
        #
        # 终止条件（三选一）：
        #   1. LLM 不再调用工具（正常结束）
        #   2. 达到 max_iterations 次迭代（安全保护）
        #   3. cancel_token 被标记为已取消（用户中断）
        # =====================================================================

        try:
            while iteration < self.max_iterations:
                iteration += 1

                # 检查点 1：用户是否取消了生成
                if cancel_token and cancel_token.is_cancelled:
                    logger.info(f"Agent loop cancelled at iteration {iteration}: {session_id}")
                    return

                logger.debug(f"Agent iteration {iteration}/{self.max_iterations}, total tool calls: {total_tool_calls}")

                # 获取所有已注册工具的定义（JSON Schema 格式），告诉 LLM "你可以调用这些工具"
                tool_definitions = self.tools.get_definitions() if self.tools else []

                # 本轮迭代的缓冲区
                content_buffer = ""       # 累积 LLM 输出的文本内容
                tool_calls_buffer = []    # 累积 LLM 请求的工具调用
                finish_reason = None      # LLM 结束原因（"stop" 或 "tool_calls"）
                reasoning_buffer = ""     # 推理内容（DeepSeek 等支持 reasoning 的模型）

                # ---- 流式调用 LLM ----
                # provider.chat_stream() 返回异步迭代器，每个 chunk 是 LLM 输出的一小片段
                # Java 类比：类似于 WebClient.retrieve().bodyToFlux(ChatChunk.class)
                async for chunk in self.provider.chat_stream(
                    messages=messages,
                    tools=tool_definitions,
                    model=self.model,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                ):
                    # 文本内容：追加到 buffer，同时 yield 给调用者（实现流式输出）
                    if chunk.is_content and chunk.content:
                        content_buffer += chunk.content
                        yield chunk.content  # ★ 这里是流式输出的关键：每收到一小段文字就立即推给前端

                    # 工具调用：收集到列表，等流式结束后统一执行
                    # （LLM 可能同时请求调用多个工具，需要全部收齐再执行）
                    if chunk.is_tool_call and chunk.tool_call:
                        tool_calls_buffer.append(chunk.tool_call)

                    # 推理内容：部分模型（如 DeepSeek）会输出"思考过程"，单独收集
                    if chunk.is_reasoning and chunk.reasoning_content:
                        reasoning_buffer += chunk.reasoning_content

                    # 完成信号：LLM 告知本轮输出结束
                    if chunk.is_done and chunk.finish_reason:
                        finish_reason = chunk.finish_reason

                    # 错误：直接 yield 错误信息给用户，然后终止
                    if chunk.is_error:
                        yield chunk.error
                        return

                # ---- LLM 流式输出结束，处理结果 ----

                if content_buffer:
                    final_content = content_buffer
                    logger.info(f"AI完整响应 (长度: {len(content_buffer)}字符):\n{content_buffer}")

                # ---- 如果 LLM 请求了工具调用，进入工具执行流程 ----
                if tool_calls_buffer:
                    # 将 LLM 的工具调用请求转换为 OpenAI 格式的 tool_calls 字典
                    # 这是 LLM API 的标准格式：assistant 消息中包含 tool_calls 数组
                    tool_call_dicts = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in tool_calls_buffer
                    ]

                    # 将 assistant 消息（包含文本 + 工具调用请求）加入消息列表
                    # 这样下一轮 LLM 调用时能看到"我之前请求了哪些工具"
                    if self.context_builder:
                        messages = self.context_builder.add_assistant_message(
                            messages,
                            content_buffer or None,
                            tool_call_dicts,
                            reasoning_content=reasoning_buffer or None,
                        )
                    else:
                        msg = {
                            "role": "assistant",
                            "content": content_buffer or "",
                            "tool_calls": tool_call_dicts,
                        }
                        if reasoning_buffer:
                            msg["reasoning_content"] = reasoning_buffer
                        messages.append(msg)

                    # ---- 逐个执行工具 ----
                    for tool_call in tool_calls_buffer:
                        # 安全检查：是否超过最大工具调用次数
                        if total_tool_calls >= self.max_iterations:
                            logger.warning(
                                f"Reached max tool calls limit ({self.max_iterations}), "
                                f"skipping remaining tool calls in this iteration"
                            )
                            break

                        # 检查点 2：用户是否取消了生成（每个工具执行前都检查一次）
                        if cancel_token and cancel_token.is_cancelled:
                            logger.info(f"Agent loop cancelled before tool execution: {session_id}")
                            return

                        total_tool_calls += 1
                        tool_name = tool_call.name
                        tool_args = tool_call.arguments
                        tool_id = tool_call.id

                        logger.info(
                            f"Executing tool {total_tool_calls}/{self.max_iterations}: "
                            f"{tool_name} with args: {json.dumps(tool_args, ensure_ascii=False)}"
                        )

                        # 通过 WebSocket 通知前端："AI 正在调用 xxx 工具"
                        try:
                            from backend.ws.tool_notifications import notify_tool_execution
                            await notify_tool_execution(
                                session_id=session_id,
                                tool_name=tool_name,
                                arguments=tool_args,
                            )
                        except Exception as e:
                            logger.warning(f"Failed to send tool notification: {e}")

                        start_time = time.time()

                        # ---- 工具执行 + 重试机制 ----
                        # 假设失败是临时性的（如网络超时），重试时不改变参数
                        # Java 类比：类似于 @Retryable(maxAttempts=3, backoff=@Backoff(delay=1000))
                        result = None
                        last_error = None

                        for attempt in range(self.max_retries):
                            try:
                                result = await self.execute_tool(tool_name, tool_args)
                                logger.debug(f"Tool {tool_name} executed successfully")
                                break

                            except Exception as e:
                                last_error = e
                                logger.warning(
                                    f"Tool {tool_name} failed (attempt {attempt + 1}/{self.max_retries}): {e}"
                                )

                                if attempt < self.max_retries - 1:
                                    await asyncio.sleep(self.retry_delay)

                        duration_ms = int((time.time() - start_time) * 1000)

                        # ---- 处理工具执行结果 ----
                        if result is not None:
                            # 工具执行成功：记录到对话历史 + 通知前端 + 加入消息列表
                            try:
                                conversation_history = get_conversation_history()
                                conversation_history.add_conversation(
                                    session_id=session_id,
                                    tool_name=tool_name,
                                    arguments=tool_args,
                                    user_message=message,
                                    result=result,
                                    duration_ms=duration_ms
                                )
                            except Exception as e:
                                logger.warning(f"Failed to record tool conversation: {e}")

                            # 通知前端："工具执行完成，结果是 xxx"
                            try:
                                from backend.ws.tool_notifications import notify_tool_execution
                                await notify_tool_execution(
                                    session_id=session_id,
                                    tool_name=tool_name,
                                    arguments=tool_args,
                                    result=result,
                                )
                            except Exception as e:
                                logger.warning(f"Failed to send tool result notification: {e}")

                            # ★ 关键：将工具结果以 role="tool" 加入消息列表
                            # 下一轮循环 LLM 就能看到工具返回了什么，据此生成最终回复
                            # Java 类比：类似于往 List<ChatMessage> 中 add 一条 ToolMessage
                            if self.context_builder:
                                messages = self.context_builder.add_tool_result(
                                    messages,
                                    tool_id,
                                    tool_name,
                                    result,
                                )
                            else:
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_id,
                                    "name": tool_name,
                                    "content": result,
                                })
                        else:
                            # 所有重试都失败了：将错误信息作为工具结果告诉 LLM
                            # LLM 看到错误后通常会告知用户"工具执行失败"或尝试其他方案
                            error_msg = f"Tool execution failed after {self.max_retries} attempts: {str(last_error)}"
                            logger.error(f"Tool {tool_name} failed permanently: {error_msg}")

                            try:
                                conversation_history = get_conversation_history()
                                conversation_history.add_conversation(
                                    session_id=session_id,
                                    tool_name=tool_name,
                                    arguments=tool_args,
                                    user_message=message,
                                    error=error_msg,
                                    duration_ms=duration_ms
                                )
                            except Exception as e:
                                logger.warning(f"Failed to record tool conversation: {e}")

                            try:
                                from backend.ws.tool_notifications import notify_tool_execution
                                await notify_tool_execution(
                                    session_id=session_id,
                                    tool_name=tool_name,
                                    arguments=tool_args,
                                    error=error_msg,
                                )
                            except Exception as e:
                                logger.warning(f"Failed to send tool error notification: {e}")

                            if self.context_builder:
                                messages = self.context_builder.add_tool_result(
                                    messages,
                                    tool_id,
                                    tool_name,
                                    error_msg,
                                )
                            else:
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_id,
                                    "name": tool_name,
                                    "content": error_msg,
                                })
                else:
                    # ---- LLM 没有请求工具调用 → 正常结束循环 ----
                    # 说明 LLM 认为已经有足够信息回答用户，不需要再调用工具
                    logger.info("No tool calls, ending agent loop")
                    break

            # =================================================================
            # 阶段三：收尾工作
            # =================================================================

            # 3.1 如果达到了迭代/工具调用上限，给用户一个提示
            if iteration >= self.max_iterations or total_tool_calls >= self.max_iterations:
                if total_tool_calls >= self.max_iterations:
                    logger.warning(f"Max tool calls ({self.max_iterations}) reached")
                    warning_msg = f"\n\n[达到最大工具调用次数 {self.max_iterations}]"
                else:
                    logger.warning(f"Max iterations ({self.max_iterations}) reached")
                    warning_msg = f"\n\n[达到最大迭代次数 {self.max_iterations}]"
                yield warning_msg
                final_content += warning_msg

            # 3.2 保存到会话（CLI 模式使用，WebSocket 模式由 events.py 负责保存）
            if self.session_manager and final_content:
                try:
                    session = self.session_manager.get_or_create(session_id)
                    session.add_message("user", message)
                    session.add_message("assistant", final_content)
                    self.session_manager.save(session)
                except Exception as e:
                    logger.warning(f"Failed to save session: {e}")

            # 3.3 记录 AI 完整响应到文件审计日志（便于事后排查问题）
            if self.tools and final_content:
                try:
                    from backend.modules.tools.file_audit_logger import file_audit_logger
                    file_audit_logger.record_ai_response(
                        session_id=session_id,
                        user_message=message,
                        ai_response=final_content,
                        duration_ms=None
                    )
                except Exception as e:
                    logger.warning(f"Failed to record AI response to audit log: {e}")

        except Exception as e:
            logger.exception(f"Error in agent loop: {e}")
            raise

    # =========================================================================
    # 辅助方法
    # =========================================================================

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """执行工具调用

        这是对 ToolRegistry.execute() 的简单包装。
        工具注册表内部会做参数验证、权限检查、审计记录等。

        Java 类比：类似于调用 toolRegistry.execute(name, args)，
        ToolRegistry 内部有 AOP 式的横切逻辑。

        Args:
            tool_name: 工具名称（如 "read_file"、"web_fetch"）
            arguments: 工具参数字典（如 {"path": "/tmp/test.txt"}）

        Returns:
            str: 工具执行结果（文本格式，会作为 role="tool" 消息发给 LLM）

        Raises:
            ValueError: 工具不存在
            Exception: 工具执行失败
        """
        if not self.tools:
            raise ValueError("ToolRegistry not initialized")

        logger.debug(f"Executing tool: {tool_name}")

        try:
            # auto_record=False：不自动记录对话历史，由 process_message 中手动记录
            # （因为 process_message 需要额外记录 user_message 和 duration_ms）
            result = await self.tools.execute(tool_name, arguments, auto_record=False)
            return result

        except Exception as e:
            logger.error(f"Tool execution failed: {tool_name} - {e}")
            raise

    async def process_direct(
        self,
        content: str,
        session_id: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
    ) -> str:
        """直接处理消息，返回完整字符串（非流式）

        将 process_message() 的异步生成器收集为完整字符串。
        适用于不需要流式输出的场景：
          - 定时任务（cron）执行
          - CLI 命令行调用
          - 子代理处理

        Java 类比：
          - process_message() 返回 Flux<String>（流式）
          - process_direct() 返回 Mono<String>（收集后一次性返回）
          - 内部相当于 flux.collectList().map(list -> String.join("", list))

        Args:
            content: 消息内容
            session_id: 会话标识符
            channel: 来源渠道（用于上下文）
            chat_id: 来源聊天 ID（用于上下文）

        Returns:
            Agent 的完整响应文本
        """
        response_parts = []

        async for chunk in self.process_message(
            message=content,
            session_id=session_id,
            channel=channel,
            chat_id=chat_id,
        ):
            response_parts.append(chunk)

        return "".join(response_parts)
