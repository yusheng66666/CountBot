"""智能 Cron 调度器

核心设计：精确唤醒而非轮询。
传统 cron 每秒/每分钟检查是否有任务到期，浪费 CPU。
本调度器计算最近任务的 next_run 时间，用 asyncio.sleep(精确秒数) 等待，
到点后唤醒执行，执行完再设置下一个定时器。

调度流程：
  _arm_timer() → asyncio.sleep(delay) → _on_timer() → _execute_job_safe() → _arm_timer()
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Callable, Awaitable

from backend.modules.cron.service import CronService, SHANGHAI_TZ
from backend.utils.logger import logger


def _now_shanghai() -> datetime:
    """获取当前北京时间（naive，无 tzinfo）

    为什么用 naive datetime？SQLite 不支持时区感知的 datetime，
    所以统一用 replace(tzinfo=None) 去掉时区信息，但实际语义是北京时间。
    """
    return datetime.now(SHANGHAI_TZ).replace(tzinfo=None)

DEFAULT_MAX_CONCURRENT = 3   # 最大并发执行数（防止同时执行太多任务拖慢系统）
DEFAULT_JOB_TIMEOUT = 300    # 单个任务最大执行时间 5 分钟（防止任务卡住）
MAX_COMMIT_RETRIES = 3       # SQLite 写入重试次数（应对 "database is locked" 错误）


class CronScheduler:
    """智能调度器 - 精确按需唤醒，支持并发控制

    生命周期：start() → 循环执行任务 → stop()
    """

    def __init__(
        self,
        db_session_factory,
        on_execute: Optional[Callable[..., Awaitable[str]]] = None,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        job_timeout: int = DEFAULT_JOB_TIMEOUT,
    ):
        self.db_session_factory = db_session_factory  # 数据库会话工厂（每个任务独立 session）
        self.on_execute = on_execute        # 任务执行回调，实际指向 CronExecutor.execute()
        self.job_timeout = job_timeout      # 单任务超时时间
        self._running = False               # 调度器运行状态标记
        self._timer_task: Optional[asyncio.Task] = None  # 当前的定时器协程任务
        self._lock = asyncio.Lock()         # 防止 start/stop 并发调用
        self._semaphore = asyncio.Semaphore(max_concurrent)  # 并发信号量，限制同时执行的任务数
        self._active_jobs: set[str] = set()       # 正在执行的 job_id 集合（用于防重复执行）
        self._active_tasks: set[asyncio.Task] = set()  # 正在执行的 asyncio.Task（用于 stop 时等待）
    
    async def start(self):
        """启动调度器 - 应用启动时调用一次"""
        async with self._lock:
            if self._running:
                logger.warning("Scheduler already running")
                return
            self._running = True

        # 启动时先重新计算所有任务的 next_run（可能因上次异常退出导致时间不准）
        await self._recompute_next_runs()
        # 设置第一个定时器，开始调度循环
        self._arm_timer()
        logger.info(f"Cron scheduler started (max_concurrent={self._semaphore._value}, timeout={self.job_timeout}s)")
    
    async def stop(self):
        """停止调度器 - 应用关闭时调用，优雅等待执行中的任务"""
        async with self._lock:
            if not self._running:
                return
            self._running = False

        # 1. 取消定时器（不再调度新任务）
        if self._timer_task:
            self._timer_task.cancel()
            try:
                await self._timer_task
            except asyncio.CancelledError:
                pass
            self._timer_task = None

        # 2. 等待正在执行的任务完成（最多等 30 秒，超时则强制取消）
        if self._active_tasks:
            logger.info(f"Waiting for {len(self._active_tasks)} active jobs to finish...")
            done, pending = await asyncio.wait(
                self._active_tasks,
                timeout=30,
            )
            if pending:
                logger.warning(f"Force cancelling {len(pending)} jobs after timeout")
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        logger.info("Cron scheduler stopped")
    
    async def _recompute_next_runs(self):
        """重新计算所有启用任务的 next_run

        场景：启动时调用一次，确保所有任务的 next_run 是准确的。
        比如应用宕机了 2 小时，很多任务的 next_run 已经过期，需要重新算。
        """
        try:
            async with self.db_session_factory() as db:
                service = CronService(db)
                jobs = await service.list_jobs(enabled_only=True)

                for job in jobs:
                    try:
                        # 用 croniter 根据 cron 表达式算出下一个触发时间
                        next_run = service.calculate_next_run(job.schedule)
                        job.next_run = next_run
                    except Exception as e:
                        logger.error(f"Failed to compute next run for {job.id}: {e}")

                await self._safe_commit(db)
                logger.debug(f"Recomputed {len(jobs)} jobs")
        except Exception as e:
            logger.error(f"Failed to recompute: {e}")
    
    async def _get_next_wake_time(self) -> Optional[datetime]:
        """获取所有启用任务中最早的 next_run 时间

        调度器根据这个时间决定 sleep 多久。
        比如有 3 个任务：10:00、10:30、11:00，返回 10:00。
        """
        try:
            async with self.db_session_factory() as db:
                service = CronService(db)
                jobs = await service.list_jobs(enabled_only=True)

                if not jobs:
                    return None

                # 取所有任务的 next_run，选最小的（最早到期的）
                next_times = [j.next_run for j in jobs if j.next_run]
                if not next_times:
                    return None

                return min(next_times)
        except Exception as e:
            logger.error(f"Failed to get next wake time: {e}")
            return None
    
    def _arm_timer(self):
        """设置下一个定时器 —— 调度循环的核心

        工作流程：
        1. 查询最近的任务时间 → _get_next_wake_time()
        2. 计算需要等待的秒数 → asyncio.sleep(delay)
        3. 到点后执行到期任务 → _on_timer()
        4. 执行完后再调用 _arm_timer() 设置下一个定时器（形成循环）
        """
        # 取消上一个还没执行的定时器（可能因为有新任务加入需要重新计算）
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()

        async def schedule_next():
            try:
                # 1. 查询最近的任务时间
                next_wake = await self._get_next_wake_time()

                if not next_wake or not self._running:
                    # 没有任何启用的任务，60 秒后再检查一次（可能期间有新任务被创建）
                    logger.debug("No jobs to schedule")
                    await asyncio.sleep(60)
                    if self._running:
                        self._arm_timer()
                    return

                # 2. 计算精确等待时间
                now = _now_shanghai()
                delay = (next_wake - now).total_seconds()

                if delay < 0:
                    # 已经过期了（可能因为上个任务执行太久），立即执行
                    logger.warning(f"Job overdue by {abs(delay):.1f}s")
                    delay = 0

                # 3. 精确等待（这里 await，让出事件循环给其他协程）
                logger.debug(f"Next job in {delay:.1f}s at {next_wake}")
                await asyncio.sleep(delay)

                # 4. 到点，执行到期任务
                if self._running:
                    await self._on_timer()
            except asyncio.CancelledError:
                logger.debug("Timer cancelled")
            except Exception as e:
                # 出错后 10 秒重试，避免调度循环中断
                logger.error(f"Timer error: {e}")
                await asyncio.sleep(10)
                if self._running:
                    self._arm_timer()

        # 创建异步任务，在后台运行定时器
        self._timer_task = asyncio.create_task(schedule_next())
    
    async def _on_timer(self):
        """定时器触发 - 查询到期任务并并发执行"""
        try:
            async with self.db_session_factory() as db:
                service = CronService(db)
                # 查询 next_run <= now 的任务
                due_jobs = await service.get_due_jobs()

                if not due_jobs:
                    logger.debug("No due jobs")
                    return

                # 防重复：过滤掉已经在执行中的任务（上一轮还没执行完的）
                pending = [j for j in due_jobs if j.id not in self._active_jobs]
                if not pending:
                    logger.debug("All due jobs already running, skipping")
                    return

                logger.info(f"Executing {len(pending)} jobs (active: {len(self._active_jobs)})")

                # 为每个到期任务创建独立的异步任务，并发执行
                # _execute_job_safe 内部有信号量控制，最多同时执行 max_concurrent 个
                tasks = []
                for job in pending:
                    task = asyncio.create_task(
                        self._execute_job_safe(job),
                        name=f"cron-job-{job.id[:8]}"  # 命名方便调试
                    )
                    self._active_tasks.add(task)
                    # 任务完成后自动从集合中移除
                    task.add_done_callback(self._active_tasks.discard)
                    tasks.append(task)

                # 等待所有任务完成（return_exceptions=True 防止一个失败导致其他被取消）
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            logger.error(f"Timer handler error: {e}")

        finally:
            # 不管成功还是失败，都要设置下一个定时器，保持调度循环不中断
            if self._running:
                self._arm_timer()
    
    async def _execute_job_safe(self, job):
        """安全执行包装 —— 三层保护：信号量 + 超时 + 独立 session

        保护层：
        1. self._semaphore — 并发控制，最多 N 个任务同时执行
        2. asyncio.wait_for(timeout) — 单任务超时保护
        3. self.db_session_factory() — 每个任务独立的数据库会话，事务隔离
        """
        async with self._semaphore:  # 获取信号量（超过 max_concurrent 会在这里等待）
            self._active_jobs.add(job.id)  # 标记为执行中（用于防重复）
            try:
                async with self.db_session_factory() as db:
                    service = CronService(db)
                    # 重新从数据库加载最新状态（可能在等待信号量期间被禁用了）
                    fresh_job = await service.get_job(job.id)
                    if fresh_job and fresh_job.enabled:
                        # wait_for 提供超时保护，超过 job_timeout 秒抛 TimeoutError
                        await asyncio.wait_for(
                            self._execute_job(fresh_job, service),
                            timeout=self.job_timeout,
                        )
            except asyncio.TimeoutError:
                # 任务执行超时，记录错误状态到数据库
                logger.error(f"Job {job.id} timed out after {self.job_timeout}s")
                try:
                    async with self.db_session_factory() as db:
                        service = CronService(db)
                        timed_out_job = await service.get_job(job.id)
                        if timed_out_job:
                            timed_out_job.last_run = _now_shanghai()
                            timed_out_job.last_status = "error"
                            timed_out_job.last_error = f"Timed out after {self.job_timeout}s"
                            timed_out_job.error_count = (timed_out_job.error_count or 0) + 1
                            timed_out_job.run_count = (timed_out_job.run_count or 0) + 1
                            # 即使超时也要计算下次运行时间，不能让任务就此停止
                            if timed_out_job.enabled:
                                try:
                                    timed_out_job.next_run = service.calculate_next_run(timed_out_job.schedule)
                                except Exception:
                                    pass
                            await self._safe_commit(db)
                except Exception as e:
                    logger.error(f"Failed to update timeout status for {job.id}: {e}")
            except Exception as e:
                logger.error(f"Unexpected error executing job {job.id}: {e}")
            finally:
                self._active_jobs.discard(job.id)  # 无论成功/失败/超时，都从执行中集合移除
    
    async def _execute_job(self, job, service: CronService):
        """执行单个任务 —— 调用 on_execute 回调并更新数据库状态"""
        started_at = _now_shanghai()
        logger.info(f"Executing: {job.name} ({job.id})")

        try:
            if self.on_execute:
                # on_execute 实际指向 CronExecutor.execute()
                # 参数：job_id, message, channel, chat_id, deliver_response
                response = await self.on_execute(
                    job.id,
                    job.message,       # 任务内容（"__heartbeat__" 或用户定义的消息）
                    job.channel,       # 投递渠道（如 "feishu"）
                    job.chat_id,       # 投递目标 chat_id
                    job.deliver_response  # 是否将执行结果发送到渠道
                )

                # 执行成功，更新任务状态
                job.last_run = started_at
                job.last_status = "ok"
                job.last_error = None
                job.last_response = response[:1000] if response else None  # 截断保存
                job.run_count = (job.run_count or 0) + 1
                logger.info(f"Job completed: {job.name}")
            else:
                logger.warning(f"No executor: {job.name}")
                job.last_status = "skipped"

            # 计算下次运行时间（以本次开始时间为基准，避免漂移）
            if job.enabled:
                try:
                    job.next_run = service.calculate_next_run(
                        job.schedule,
                        base_time=started_at  # 基于开始时间而非结束时间
                    )
                except Exception as e:
                    # cron 表达式无效，禁用任务防止反复失败
                    logger.error(f"Failed to calculate next run: {e}")
                    job.enabled = False
                    job.last_error = f"Invalid schedule: {e}"

            await self._safe_commit(service.db)

        except Exception as e:
            # 执行失败，记录错误但不禁用任务（下次还会尝试）
            logger.error(f"Job failed: {job.name} - {e}")

            job.last_run = started_at
            job.last_status = "error"
            job.last_error = str(e)[:1000]
            job.error_count = (job.error_count or 0) + 1

            # 即使失败也要计算下次运行时间
            if job.enabled:
                try:
                    job.next_run = service.calculate_next_run(
                        job.schedule,
                        base_time=started_at
                    )
                except Exception:
                    job.enabled = False
                    job.last_error = f"Failed to calculate next run: {e}"

            await self._safe_commit(service.db)
    
    async def _safe_commit(self, db):
        """带重试的安全 commit —— 应对 SQLite 并发写入锁

        SQLite 在写入时会锁整个数据库文件，多个任务同时写入会报 "database is locked"。
        这里用递增等待的方式重试：0.5s → 1.0s → 1.5s，最多 3 次。
        """
        for attempt in range(MAX_COMMIT_RETRIES):
            try:
                await db.commit()
                return
            except Exception as e:
                if "database is locked" in str(e).lower() and attempt < MAX_COMMIT_RETRIES - 1:
                    wait = 0.5 * (attempt + 1)  # 递增等待：0.5s, 1.0s, 1.5s
                    logger.warning(f"DB locked, retrying in {wait}s (attempt {attempt + 1})")
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"Commit failed after {attempt + 1} attempts: {e}")
                    raise
    
    def is_running(self) -> bool:
        return self._running
    
    def is_job_active(self, job_id: str) -> bool:
        """检查某个任务是否正在执行"""
        return job_id in self._active_jobs
    
    @property
    def active_job_count(self) -> int:
        """当前正在执行的任务数"""
        return len(self._active_jobs)
    
    async def trigger_reschedule(self):
        """手动触发重新调度 —— 创建/更新/删除任务后调用

        CronService 在 add_job / update_job / delete_job 后会调用此方法，
        让调度器重新计算所有 next_run 并重设定时器。
        """
        if self._running:
            logger.debug("Triggering reschedule")
            await self._recompute_next_runs()
            self._arm_timer()
