"""Subagent Manager - 子 Agent 管理"""

import asyncio
import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from loguru import logger


class TaskStatus(Enum):
    """任务状态枚举"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SubagentTask:
    """子 Agent 任务"""

    def __init__(
        self,
        task_id: str,
        label: str,
        message: str,
        session_id: str | None = None,
    ):
        self.task_id = task_id
        self.label = label
        self.message = message
        self.session_id = session_id
        self.status = TaskStatus.PENDING
        self.progress = 0
        self.result: str | None = None
        self.error: str | None = None
        self.created_at = datetime.now()
        self.started_at: datetime | None = None
        self.completed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "task_id": self.task_id,
            "label": self.label,
            "message": self.message,
            "session_id": self.session_id,
            "status": self.status.value,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class SubagentManager:
    """
    子 Agent 管理器
    
    管理后台任务的创建、执行、取消和状态查询
    """

    def __init__(self, provider, workspace, model: str, temperature: float = 0.7, max_tokens: int = 4096):
        """
        初始化 SubagentManager
        
        Args:
            provider: LLM Provider 实例
            workspace: 工作空间路径
            model: 模型名称
            temperature: 温度参数
            max_tokens: 最大 token 数
        """
        self.provider = provider
        self.workspace = workspace
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.tasks: dict[str, SubagentTask] = {}
        self.running_tasks: dict[str, asyncio.Task] = {}
        
        logger.debug("SubagentManager initialized")

    def create_task(
        self,
        label: str,
        message: str,
        session_id: str | None = None,
    ) -> str:
        """
        创建新的后台任务
        
        Args:
            label: 任务标签
            message: 任务消息
            session_id: 关联的会话 ID (可选)
            
        Returns:
            str: 任务 ID
        """
        task_id = str(uuid.uuid4())
        
        task = SubagentTask(
            task_id=task_id,
            label=label,
            message=message,
            session_id=session_id,
        )
        
        self.tasks[task_id] = task
        logger.info(f"Created task {task_id}: {label}")
        
        return task_id

    async def execute_task(self, task_id: str) -> None:
        """
        执行后台任务

        Args:
            task_id: 任务 ID

        Raises:
            ValueError: 任务不存在
        """
        task = self.tasks.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        if task.status != TaskStatus.PENDING:
            logger.warning(f"Task {task_id} is not pending, current status: {task.status}")
            return

        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now()
        task.progress = 0

        logger.info(f"Starting task {task_id}: {task.label}")

        # 发送任务创建通知
        try:
            from backend.ws.task_notifications import notify_task_created
            await notify_task_created(task_id, task.label)
        except Exception as e:
            logger.debug(f"Task notification failed (non-fatal): {e}")

        # 创建异步任务
        async_task = asyncio.create_task(self._run_task(task))
        self.running_tasks[task_id] = async_task

    async def _run_task(self, task: SubagentTask) -> None:
        """
        运行任务的内部方法
        
        Args:
            task: 任务对象
        """
        try:
            # 构建子 Agent 专用的系统提示词
            system_prompt = self._build_subagent_prompt(task.message)
            
            # 构建消息列表
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task.message},
            ]
            
            # 构建工具注册表（子 Agent 可用的工具）
            from backend.modules.tools.registry import ToolRegistry
            from backend.modules.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
            from backend.modules.tools.shell import ExecTool
            
            tools = ToolRegistry()
            tools.register(ReadFileTool(self.workspace))
            tools.register(WriteFileTool(self.workspace))
            tools.register(EditFileTool(self.workspace))
            tools.register(ListDirTool(self.workspace))
            tools.register(ExecTool(
                workspace=self.workspace,
                timeout=300,
                allow_dangerous=False,
                restrict_to_workspace=True,
            ))
            
            # 尝试注册 Web 工具
            try:
                from backend.modules.tools.web import WebSearchTool, WebFetchTool
                tools.register(WebSearchTool())
                tools.register(WebFetchTool())
            except ImportError:
                logger.warning("Web tools not available for subagent")
            
            # 收集响应
            response_chunks = []
            iteration = 0
            max_iterations = 15  # 子 Agent 限制迭代次数
            
            # 执行 Agent Loop
            while iteration < max_iterations:
                iteration += 1
                
                # 获取工具定义
                tool_definitions = tools.get_definitions()
                
                # 调用 LLM（使用 chat_stream 并收集完整响应）
                content_buffer = ""
                tool_calls_buffer = []
                
                async for chunk in self.provider.chat_stream(
                    messages=messages,
                    tools=tool_definitions,
                    model=self.model,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                ):
                    # 收集内容
                    if chunk.is_content and chunk.content:
                        content_buffer += chunk.content
                    
                    # 收集工具调用
                    if chunk.is_tool_call and chunk.tool_call:
                        tool_calls_buffer.append(chunk.tool_call)
                
                # 处理响应
                if content_buffer:
                    response_chunks.append(content_buffer)
                
                # 处理工具调用
                if tool_calls_buffer:
                    import json
                    
                    # 添加助手消息
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
                    messages.append({
                        "role": "assistant",
                        "content": content_buffer or "",
                        "tool_calls": tool_call_dicts,
                    })
                    
                    # 执行工具
                    for tool_call in tool_calls_buffer:
                        result = await tools.execute(
                            tool_name=tool_call.name,
                            arguments=tool_call.arguments
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": result,
                        })

                        task.progress = min(90, task.progress + 5)
                        # 推送进度更新
                        try:
                            from backend.ws.task_notifications import notify_task_progress
                            await notify_task_progress(task.task_id, task.progress)
                        except Exception:
                            pass
                else:
                    # 没有工具调用，完成
                    break

            # 任务完成
            task.result = "".join(response_chunks)
            task.status = TaskStatus.COMPLETED
            task.progress = 100
            task.completed_at = datetime.now()

            logger.info(f"Task {task.task_id} completed successfully")

            # 推送完成通知
            try:
                from backend.ws.task_notifications import notify_task_complete
                await notify_task_complete(task.task_id, task.result)
            except Exception as e:
                logger.debug(f"Task complete notification failed: {e}")

            # 将结果推送到聊天界面并持久化
            await self._deliver_result_to_session(task)

        except asyncio.CancelledError:
            # 任务被取消
            task.status = TaskStatus.CANCELLED
            task.completed_at = datetime.now()

            logger.info(f"Task {task.task_id} was cancelled")

            # 推送失败通知（取消也视为一种失败）
            try:
                from backend.ws.task_notifications import notify_task_failed
                await notify_task_failed(task.task_id, "任务已取消")
            except Exception:
                pass

            await self._deliver_result_to_session(task)

        except Exception as e:
            # 任务失败
            task.status = TaskStatus.FAILED
            task.error = str(e)
            task.completed_at = datetime.now()

            logger.error(f"Task {task.task_id} failed: {e}")

            # 推送失败通知
            try:
                from backend.ws.task_notifications import notify_task_failed
                await notify_task_failed(task.task_id, str(e))
            except Exception:
                pass

            await self._deliver_result_to_session(task)
            
        finally:
            # 清理运行中的任务
            if task.task_id in self.running_tasks:
                del self.running_tasks[task.task_id]

    async def _deliver_result_to_session(self, task: SubagentTask) -> None:
        """将子代理结果推送到聊天界面并持久化到数据库"""
        if not task.session_id:
            return

        try:
            # 构造推送内容
            if task.status == TaskStatus.COMPLETED:
                content = f"**子代理任务「{task.label}」已完成**\n\n{task.result or '（无返回内容）'}"
            elif task.status == TaskStatus.CANCELLED:
                content = f"**子代理任务「{task.label}」已取消**"
            else:
                content = f"**子代理任务「{task.label}」失败**: {task.error or '未知错误'}"

            # 1. 通过 WebSocket 实时推送到前端聊天界面
            # TODO: 目前只支持 WebSocket 推送，飞书/Telegram 等渠道用户收不到实时结果
            #       需要增加 channel/chat_id 字段，通过 ChannelManager 投递（参考 CronExecutor._deliver_to_channel）
            from backend.ws.connection import send_message_chunk, send_message_complete
            await send_message_chunk(task.session_id, content)
            await send_message_complete(task.session_id, message_id=f"subagent-{task.task_id}")

            # 2. 持久化到数据库，刷新页面后也能看到
            from backend.database import get_db_session_factory
            from backend.models.message import Message
            db_factory = get_db_session_factory()
            async with db_factory() as db:
                db.add(Message(
                    session_id=task.session_id,
                    role="assistant",
                    content=content,
                ))
                await db.commit()

            logger.info(f"Subagent result delivered to session {task.session_id}")

        except Exception as e:
            logger.error(f"Failed to deliver subagent result to session: {e}")

    def _build_subagent_prompt(self, task: str) -> str:
        """
        构建子 Agent 专用的系统提示词
        
        Args:
            task: 任务描述
            
        Returns:
            str: 系统提示词
        """
        workspace_path = str(self.workspace.expanduser().resolve())
        
        return f"""# 子代理 (Subagent)

你是主代理创建的子代理，专门负责完成特定任务。

## 你的任务
{task}

## 工作规则
1. **专注任务**: 只完成分配的任务，不做其他事情
2. **简洁高效**: 最终响应会报告给主代理，保持简洁但信息完整
3. **不要闲聊**: 不要发起对话或承担额外任务
4. **彻底完成**: 确保任务完整完成，提供清晰的结果总结

## 可用能力
- 读写工作空间文件
- 执行 Shell 命令
- 网络搜索和抓取网页
- 使用所有标准工具

## 限制
- 不能直接向用户发送消息（无 message 工具）
- 不能创建其他子代理（无 spawn 工具）
- 无法访问主代理的对话历史

## 工作空间
{workspace_path}

## 完成标准
任务完成后，提供清晰的总结：
- 完成了什么
- 发现了什么（如果是调查任务）
- 遇到的问题（如果有）
- 建议的后续步骤（如果需要）"""

    async def cancel_task(self, task_id: str) -> bool:
        """
        取消任务
        
        Args:
            task_id: 任务 ID
            
        Returns:
            bool: 是否成功取消
        """
        task = self.tasks.get(task_id)
        if not task:
            logger.warning(f"Cannot cancel task {task_id}: not found")
            return False
        
        if task.status != TaskStatus.RUNNING:
            logger.warning(f"Cannot cancel task {task_id}: not running")
            return False
        
        # 取消异步任务
        async_task = self.running_tasks.get(task_id)
        if async_task:
            async_task.cancel()
            logger.info(f"Cancelled task {task_id}")
            return True
        
        return False

    def get_task(self, task_id: str) -> SubagentTask | None:
        """
        获取任务信息
        
        Args:
            task_id: 任务 ID
            
        Returns:
            SubagentTask: 任务对象，如果不存在则返回 None
        """
        return self.tasks.get(task_id)

    def list_tasks(
        self,
        status: TaskStatus | None = None,
        session_id: str | None = None,
    ) -> list[SubagentTask]:
        """
        列出任务
        
        Args:
            status: 过滤状态 (可选)
            session_id: 过滤会话 ID (可选)
            
        Returns:
            list: 任务列表
        """
        tasks = list(self.tasks.values())
        
        # 按状态过滤
        if status:
            tasks = [t for t in tasks if t.status == status]
        
        # 按会话过滤
        if session_id:
            tasks = [t for t in tasks if t.session_id == session_id]
        
        # 按创建时间倒序排序
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        
        return tasks

    def get_running_tasks(self) -> list[SubagentTask]:
        """
        获取所有运行中的任务
        
        Returns:
            list: 运行中的任务列表
        """
        return self.list_tasks(status=TaskStatus.RUNNING)

    def delete_task(self, task_id: str) -> bool:
        """
        删除任务
        
        Args:
            task_id: 任务 ID
            
        Returns:
            bool: 是否成功删除
        """
        task = self.tasks.get(task_id)
        if not task:
            logger.warning(f"Cannot delete task {task_id}: not found")
            return False
        
        # 如果任务正在运行，先取消
        if task.status == TaskStatus.RUNNING:
            asyncio.create_task(self.cancel_task(task_id))
        
        # 删除任务
        del self.tasks[task_id]
        logger.info(f"Deleted task {task_id}")
        
        return True

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self.running_tasks)

    def get_stats(self) -> dict[str, int]:
        """
        获取任务统计信息
        
        Returns:
            dict: 统计信息
        """
        return {
            "total": len(self.tasks),
            "pending": len([t for t in self.tasks.values() if t.status == TaskStatus.PENDING]),
            "running": len([t for t in self.tasks.values() if t.status == TaskStatus.RUNNING]),
            "completed": len([t for t in self.tasks.values() if t.status == TaskStatus.COMPLETED]),
            "failed": len([t for t in self.tasks.values() if t.status == TaskStatus.FAILED]),
            "cancelled": len([t for t in self.tasks.values() if t.status == TaskStatus.CANCELLED]),
        }

    async def cleanup_old_tasks(self, max_age_hours: int = 24) -> int:
        """
        清理旧任务
        
        Args:
            max_age_hours: 最大保留时间（小时）
            
        Returns:
            int: 清理的任务数量
        """
        from datetime import timedelta
        
        cutoff_time = datetime.now() - timedelta(hours=max_age_hours)
        cleaned = 0
        
        for task_id, task in list(self.tasks.items()):
            # 只清理已完成、失败或取消的任务
            if task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
                if task.completed_at and task.completed_at < cutoff_time:
                    del self.tasks[task_id]
                    cleaned += 1
        
        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} old tasks")
        
        return cleaned
