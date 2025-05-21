import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional, List
from pathlib import Path
import datetime
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.layout import Layout
from rich.text import Text
from rich.spinner import Spinner
from rich.style import Style

from mcp.server.fastmcp import FastMCP, Context
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.requests import Request
from mcp.server.sse import SseServerTransport
from mcp.server import Server as LowLevelServer
import argparse

logger = logging.getLogger("MCPMultiTerminalServer_TS")

tc_process: Optional[asyncio.subprocess.Process] = None
tc_writer: Optional[asyncio.StreamWriter] = None
tc_reader: Optional[asyncio.StreamReader] = None
tc_stderr_task: Optional[asyncio.Task] = None
tc_stdout_handler_task: Optional[asyncio.Task] = None

request_futures: Dict[str, asyncio.Future] = {}
REQUEST_ID_COUNTER = 0
TS_SHUTDOWN_EVENT = asyncio.Event()

active_terminals: Dict[str, Dict[str, Any]] = {}

mcp_server_instance = FastMCP(
    "MultiTerminalAgentSSE",
    version="3.0.0",
    description="MCP服务器，用于通过终端客户端（TC）管理多个可见的终端。AI可以控制命令执行并查看输出。",
)

# 添加终端状态监控相关常量和变量
TERMINAL_STATUS_CHECK_INTERVAL = 10  # 状态检查间隔（秒）
TERMINAL_STATUS_TIMEOUT = 5  # 状态检查超时时间（秒）

# 添加健康检查相关常量和变量
HEALTH_CHECK_INTERVAL = 30  # 健康检查间隔（秒）
MAX_RECONNECT_ATTEMPTS = 3  # 最大重连次数
RECONNECT_DELAY = 5  # 重连延迟（秒）


class TerminalHealth:

    def __init__(self):
        self.last_check_time = None
        self.reconnect_attempts = 0
        self.is_healthy = True
        self.last_error = None


async def send_to_tc(action: str,
                     payload: Dict[str, Any],
                     request_timeout: float = 60.0) -> Dict[str, Any]:
    global tc_writer, REQUEST_ID_COUNTER, request_futures
    if TS_SHUTDOWN_EVENT.is_set():
        return {"success": False, "message": "终端服务正在关闭。"}
    if not tc_writer or tc_writer.is_closing():
        logger.error(f"TC写入器不可用，操作：{action}。TC进程可能已关闭。")
        return {"success": False, "message": "TC写入不可用。TC进程可能已关闭。"}

    REQUEST_ID_COUNTER += 1
    request_id = f"ts_req_{REQUEST_ID_COUNTER}"
    command_data = {
        "request_id": request_id,
        "type": action,
        "payload": payload
    }
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    request_futures[request_id] = future

    try:
        json_command = json.dumps(command_data) + "\n"
        tc_writer.write(json_command.encode('utf-8'))
        await tc_writer.drain()

        logger.info(
            f"TS: 已发送到TC (ID: {request_id}): Action='{action}', Payload='{json.dumps(payload)}'"
        )

        response_wait_timeout = request_timeout + 15.0
        response = await asyncio.wait_for(future,
                                          timeout=response_wait_timeout)
        return response
    except asyncio.TimeoutError:
        logger.error(
            f"等待TC响应超时，请求ID: {request_id} (操作 {action} 等待了 {response_wait_timeout}秒)"
        )
        if request_id in request_futures and not future.done():
            future.set_exception(
                asyncio.TimeoutError(f"TS等待TC超时，请求ID: {request_id}"))
        return {"success": False, "message": f"等待TC响应超时 (请求ID: {request_id})"}
    except Exception as e:
        logger.error(f"与TC通信错误 (ID: {request_id}, 操作: {action}): {e}",
                     exc_info=True)
        if request_id in request_futures and not future.done():
            future.set_exception(e)
        return {"success": False, "message": f"与TC通信错误: {str(e)}"}
    finally:
        if request_id in request_futures:
            request_futures.pop(request_id, None)


async def tc_stdout_message_handler(reader: asyncio.StreamReader):
    global request_futures
    while not TS_SHUTDOWN_EVENT.is_set():
        try:
            line_bytes = await asyncio.wait_for(reader.readline(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except (asyncio.IncompleteReadError, ConnectionResetError,
                BrokenPipeError) as e:
            logger.warning(f"TC stdout流结束或连接重置/中断: {type(e).__name__}。")
            break
        except Exception as e_read:
            logger.error(f"读取TC stdout时发生意外错误: {e_read}", exc_info=True)
            await asyncio.sleep(0.1)
            continue

        if not line_bytes:
            if reader.at_eof():
                logger.warning("TC stdout到达EOF。停止处理器。")
                break
            logger.debug("从TC stdout接收到空行，继续。")
            continue

        line = line_bytes.decode('utf-8', errors='replace').strip()
        if not line: continue

        try:
            response_data = json.loads(line)
            request_id = response_data.get("request_id")
            if request_id and request_id in request_futures:
                future = request_futures.pop(request_id, None)
                if future and not future.done():
                    future.set_result(response_data.get("payload", {}))
                elif future and future.done():
                    logger.warning(f"TC对已解决/取消的future的响应: {request_id}")
            elif response_data.get("type") == "tc_event":
                logger.info(f"收到TC事件: {response_data.get('payload')}")
                event_payload = response_data.get("payload", {})
                if event_payload.get(
                        "event_type") == "terminal_closed_unexpectedly":
                    closed_terminal_id = event_payload.get("terminal_id")
                    if closed_terminal_id and closed_terminal_id in active_terminals:
                        logger.warning(
                            f"TC报告terminal_id意外关闭: {closed_terminal_id}。从活动列表中移除。"
                        )
                        if closed_terminal_id in active_terminals:
                            del active_terminals[closed_terminal_id]
            else:
                logger.info(f"来自TC的未经请求/未知消息: {response_data}")
        except json.JSONDecodeError:
            logger.error(f"来自TC stdout的JSON解码错误。接收到的行: '{line}'")
        except Exception as e_proc:
            logger.error(f"处理TC stdout消息时出错: {e_proc} (原始行: '{line}')",
                         exc_info=True)
    logger.info("TC stdout消息处理器正在停止。")


async def tc_stderr_logger(stderr_reader: asyncio.StreamReader):
    while not TS_SHUTDOWN_EVENT.is_set():
        try:
            line_bytes = await asyncio.wait_for(stderr_reader.readline(),
                                                timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except (asyncio.IncompleteReadError, BrokenPipeError) as e:
            logger.info(f"TC stderr流结束或中断: {type(e).__name__}。")
            break
        except Exception as e_read_stderr:
            logger.error(f"读取TC stderr时出错: {e_read_stderr}", exc_info=True)
            break

        if not line_bytes:
            if stderr_reader.at_eof():
                logger.info("TC stderr流到达EOF。")
                break
            logger.debug("从TC stderr接收到空行，继续。")
            continue

        decoded_line = line_bytes.decode(errors='replace').strip()
        if decoded_line:
            logger.info(f"[TC_ERR] {decoded_line}")
    logger.info("TC stderr记录器正在停止。")


async def check_server_health():
    """检查服务器健康状态"""
    try:
        # 检查终端客户端状态
        if not tc_process or tc_process.returncode is not None:
            return False, "终端客户端进程已退出"

        # 检查通信状态
        ping_response = await send_to_tc("ping_tc", {}, request_timeout=5.0)
        if not ping_response.get("success"):
            return False, "终端客户端无响应"

        return True, "服务器运行正常"
    except Exception as e:
        return False, f"健康检查失败: {str(e)}"


async def attempt_reconnect():
    """尝试重新连接终端客户端"""
    global tc_process, tc_writer, tc_reader, tc_stderr_task, tc_stdout_handler_task

    if terminal_health.reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
        logger.error("达到最大重连次数，停止重连")
        return False

    terminal_health.reconnect_attempts += 1
    logger.info(f"尝试重连终端客户端 (第 {terminal_health.reconnect_attempts} 次)")

    try:
        # 清理旧的连接
        if tc_writer and not tc_writer.is_closing():
            tc_writer.close()
        if tc_stderr_task and not tc_stderr_task.done():
            tc_stderr_task.cancel()
        if tc_stdout_handler_task and not tc_stdout_handler_task.done():
            tc_stdout_handler_task.cancel()

        # 重新启动终端客户端
        current_dir = os.path.dirname(os.path.abspath(__file__))
        tc_script_path = os.path.join(current_dir, "client.py")

        tc_process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            tc_script_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid if sys.platform != "win32" else None)

        if tc_process.stdin is None or tc_process.stdout is None or tc_process.stderr is None:
            raise RuntimeError("未能获取TC进程的stdio管道")

        tc_writer = tc_process.stdin
        tc_reader = tc_process.stdout
        tc_stderr_reader_for_task = tc_process.stderr

        tc_stderr_task = asyncio.create_task(
            tc_stderr_logger(tc_stderr_reader_for_task))
        tc_stdout_handler_task = asyncio.create_task(
            tc_stdout_message_handler(tc_reader))

        # 验证连接
        ping_response = await send_to_tc("ping_tc", {}, request_timeout=5.0)
        if not ping_response.get("success"):
            raise RuntimeError("重连后ping失败")

        logger.info("终端客户端重连成功")
        terminal_health.reconnect_attempts = 0
        terminal_health.is_healthy = True
        terminal_health.last_error = None
        return True

    except Exception as e:
        logger.error(f"重连失败: {e}")
        terminal_health.last_error = str(e)
        return False


async def health_check_task():
    """定期执行健康检查"""
    while not TS_SHUTDOWN_EVENT.is_set():
        try:
            is_healthy, message = await check_server_health()
            terminal_health.is_healthy = is_healthy
            terminal_health.last_check_time = datetime.datetime.now()

            if not is_healthy:
                logger.warning(f"健康检查失败: {message}")
                if not terminal_health.is_healthy:
                    await attempt_reconnect()
            else:
                terminal_health.reconnect_attempts = 0
                terminal_health.last_error = None

        except Exception as e:
            logger.error(f"健康检查任务出错: {e}")

        await asyncio.sleep(HEALTH_CHECK_INTERVAL)


# 初始化健康检查对象
terminal_health = TerminalHealth()


@asynccontextmanager
async def tc_subprocess_lifespan_manager(
        mcp_server: FastMCP) -> AsyncIterator[None]:
    global tc_process, tc_writer, tc_reader, tc_stderr_task, tc_stdout_handler_task, TS_SHUTDOWN_EVENT, active_terminals
    TS_SHUTDOWN_EVENT.clear()
    active_terminals.clear()

    # 启动终端状态监控任务和健康检查任务
    status_monitor_task = None
    health_check_task_instance = None

    logger.info("TC子进程生命周期管理器: 启动中...")
    current_dir = os.path.dirname(os.path.abspath(__file__))
    tc_script_name = "client.py"
    tc_script_path_env = os.getenv("TC_SCRIPT_PATH")

    if tc_script_path_env and os.path.exists(tc_script_path_env):
        tc_script_path = tc_script_path_env
        logger.info(f"使用TC_SCRIPT_PATH环境变量中的TC脚本: {tc_script_path}")
    else:
        tc_script_path = os.path.join(current_dir, tc_script_name)
        if not os.path.exists(tc_script_path):
            alt_tc_script_path = os.path.join(os.getcwd(), tc_script_name)
            if os.path.exists(alt_tc_script_path):
                tc_script_path = alt_tc_script_path
            else:
                raise FileNotFoundError(
                    f"TC脚本 '{tc_script_name}' 在以下路径未找到: {tc_script_path} 或 {alt_tc_script_path}"
                )

    logger.info(f"尝试启动TC脚本: {tc_script_path}")
    status.update_status("正在启动终端客户端...", "running", "main")
    status.refresh_display()

    try:
        tc_process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            tc_script_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid if sys.platform != "win32" else None)
        logger.info(f"TC子进程已启动。PID: {tc_process.pid}")

        if tc_process.stdin is None or tc_process.stdout is None or tc_process.stderr is None:
            if tc_process.returncode is None:
                try:
                    tc_process.terminate()
                    await tc_process.wait()
                except ProcessLookupError:
                    logger.warning(f"在管道检查清理期间未找到TC进程 {tc_process.pid}。")
                except Exception as e_term_pipe_fail:
                    logger.error(f"在管道检查失败期间终止TC进程时出错: {e_term_pipe_fail}")
            raise RuntimeError("未能获取TC进程的stdio管道。stdin、stdout或stderr为None。")

        tc_writer = tc_process.stdin
        tc_reader = tc_process.stdout
        tc_stderr_reader_for_task = tc_process.stderr

        tc_stderr_task = asyncio.create_task(
            tc_stderr_logger(tc_stderr_reader_for_task))
        tc_stdout_handler_task = asyncio.create_task(
            tc_stdout_message_handler(tc_reader))

        logger.info("正在Ping TC以检查是否就绪...")
        status.update_status("正在检查终端客户端状态...", "running", "main")
        status.refresh_display()

        ping_response = await send_to_tc("ping_tc", {}, request_timeout=15.0)
        if not ping_response.get("success") or ping_response.get(
                "message") != "pong":
            status.update_status("终端客户端未就绪", "error", "main")
            status.refresh_display()
            raise RuntimeError(f"TC未响应ping或报告未就绪: {ping_response}")

        status.update_status("终端客户端已就绪", "success", "main")
        status.update_status(
            f"服务器状态: {ping_response.get('tc_status', 'ready')}", "success",
            "sub")
        status.refresh_display()

        logger.info(f"TC已就绪。状态: {ping_response.get('tc_status')}")
        logger.info("===================================================")
        logger.info("=== 终端服务器和客户端均已准备就绪并可操作 ===")
        logger.info("===================================================")

        # 加载保存的终端会话
        await load_terminal_sessions()

        # 启动状态监控任务和健康检查任务
        status_monitor_task = asyncio.create_task(monitor_terminal_statuses())
        health_check_task_instance = asyncio.create_task(health_check_task())

    except Exception as e_startup:
        status.update_status(f"启动失败: {str(e_startup)}", "error", "main")
        status.refresh_display()
        logger.error(f"TC子进程生命周期管理器: 启动或ping TC失败: {e_startup}", exc_info=True)
        if tc_process and tc_process.returncode is None:
            try:
                tc_process.terminate()
                await tc_process.wait()
            except ProcessLookupError:
                logger.warning(f"在启动失败清理期间未找到TC进程 {tc_process.pid}。")
            except Exception as e_term:
                logger.error(f"启动失败时终止TC出错: {e_term}")
        raise

    yield

    logger.info("TC子进程生命周期管理器: 关闭TC中...")
    status.is_shutting_down = True
    status.clear_status()
    status.update_status("正在关闭终端客户端...", "warning", "main")
    status.refresh_display()

    # 取消健康检查任务
    if health_check_task_instance and not health_check_task_instance.done():
        health_check_task_instance.cancel()
        try:
            await health_check_task_instance
        except asyncio.CancelledError:
            logger.info("健康检查任务已取消")
        except Exception as e:
            logger.error(f"等待健康检查任务取消时出错: {e}")

    if tc_writer and not tc_writer.is_closing():
        logger.info("正在向TC发送关闭命令...")
        try:
            await asyncio.wait_for(send_to_tc("shutdown_tc", {},
                                              request_timeout=5.0),
                                   timeout=8.0)
        except asyncio.TimeoutError:
            logger.warning("发送shutdown_tc命令或等待TC确认超时。")
        except Exception as e_send_shutdown:
            logger.error(f"发送关闭命令到TC或处理其响应失败: {e_send_shutdown}")

        if tc_writer and not tc_writer.is_closing():
            try:
                if hasattr(tc_writer, 'close') and callable(tc_writer.close):
                    tc_writer.close()
                    if hasattr(tc_writer, 'wait_closed') and callable(
                            tc_writer.wait_closed):
                        await tc_writer.wait_closed()
                logger.info("TC写入器 (stdin管道) 已关闭。")
            except Exception as e_close_writer:
                logger.error(f"关闭期间关闭tc_writer时出错: {e_close_writer}")

    io_tasks = [
        t for t in [tc_stdout_handler_task, tc_stderr_task]
        if t and not t.done()
    ]
    if io_tasks:
        logger.debug(f"等待 {len(io_tasks)} 个TC I/O任务完成...")
        _, pending = await asyncio.wait(io_tasks, timeout=5.0)
        for task in pending:
            logger.warning(f"TC I/O任务 {task.get_name()} 未及时完成，正在取消。")
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    if tc_process and tc_process.returncode is None:
        logger.info(f"等待TC子进程 (PID: {tc_process.pid}) 退出...")
        try:
            await asyncio.wait_for(tc_process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(f"TC子进程 (PID: {tc_process.pid}) 未正常退出。强制终止中。")
            if sys.platform != "win32" and hasattr(
                    os, 'getpgid') and tc_process.pid is not None:
                try:
                    os.killpg(os.getpgid(tc_process.pid), signal.SIGTERM)
                    await asyncio.wait_for(tc_process.wait(), timeout=3.0)
                except ProcessLookupError:
                    logger.warning(f"未找到用于SIGTERM的TC进程组 {tc_process.pid}。")
                except asyncio.TimeoutError:
                    logger.warning(
                        f"TC进程组 {tc_process.pid} 在SIGTERM后未退出，发送SIGKILL。")
                    try:
                        os.killpg(os.getpgid(tc_process.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        logger.warning(f"未找到用于SIGKILL的TC进程组 {tc_process.pid}。")
                    except Exception as e_killpg:
                        logger.error(f"发送SIGKILL到进程组时出错: {e_killpg}")
                except Exception as e_termpg:
                    logger.error(f"发送SIGTERM到进程组时出错: {e_termpg}")
            else:
                try:
                    tc_process.terminate()
                except ProcessLookupError:
                    logger.warning(f"未找到用于终止的TC进程 {tc_process.pid}。")
                try:
                    await asyncio.wait_for(tc_process.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    logger.warning(f"TC进程 {tc_process.pid} 在终止后未退出，正在杀死。")
                    try:
                        tc_process.kill()
                    except ProcessLookupError:
                        logger.warning(f"未找到用于杀死的TC进程 {tc_process.pid}。")
                    try:
                        await asyncio.wait_for(tc_process.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        logger.error(
                            f"TC子进程 (PID: {tc_process.pid}) 即使在杀死后也未退出。")
        logger.info(f"TC子进程已退出，代码: {tc_process.returncode}")

    for fut_id in list(request_futures.keys()):
        fut = request_futures.pop(fut_id, None)
        if fut and not fut.done():
            fut.cancel("TS正在关闭，取消挂起的TC请求。")
            logger.debug(f"在TS关闭期间取消了请求ID的挂起future: {fut_id}。")

    active_terminals.clear()
    logger.info("TC子进程生命周期管理器: 关闭完成。")
    status.update_status("终端客户端已关闭", "success", "main")
    status.refresh_display()

    # 取消状态监控任务
    if status_monitor_task and not status_monitor_task.done():
        status_monitor_task.cancel()
        try:
            await status_monitor_task
        except asyncio.CancelledError:
            logger.info("终端状态监控任务已取消")
        except Exception as e:
            logger.error(f"等待终端状态监控任务取消时出错: {e}")


@asynccontextmanager
async def starlette_lifespan_wrapper(app: Starlette):
    async with tc_subprocess_lifespan_manager(mcp_server_instance):
        yield


@mcp_server_instance.tool()
async def create_terminal(
        ctx: Context,
        purpose: Optional[str] = None,
        shell_command: Optional[str] = None) -> Dict[str, Any]:
    """
    创建一个新的、隔离的终端会话，由tmux管理并在新的终端模拟器窗口中显示。
    这个终端是独立的，后续操作需要使用返回的`terminal_id`。

    Args:
        purpose: (可选) 终端用途的描述。
        shell_command: (可选) 终端创建时运行的shell命令 (例如 "bash -il", "zsh")。默认为标准的交互式shell。

    Returns:
        一个包含以下键的字典:
        - success (bool): 操作是否成功。
        - terminal_id (str, 如果成功): 新创建终端的唯一ID。
        - message (str): 操作结果的消息。
        - visible_window_info (dict, 如果成功): 关于可见终端窗口的信息。
    """
    terminal_id = f"term_{uuid.uuid4().hex[:12]}"
    tmux_session_name = terminal_id.replace("-", "_")

    logger.info(
        f"TS: MCP工具 'create_terminal' 被调用。用途: '{purpose}', Shell: '{shell_command}'. 分配ID: {terminal_id}, tmux: {tmux_session_name}"
    )

    tc_creation_timeout = 30.0
    payload = {
        "terminal_id": terminal_id,
        "tmux_session_name": tmux_session_name,
        "purpose": purpose,
        "shell_command": shell_command,
    }
    response = await send_to_tc("create_terminal",
                                payload,
                                request_timeout=tc_creation_timeout)

    if response.get("success"):
        active_terminals[terminal_id] = {
            "tmux_session_name": tmux_session_name,
            "purpose": purpose,
            "status": "created",
            "visible_window_info": response.get("visible_window_info")
        }
        logger.info(f"终端 {terminal_id} (用途: {purpose}) 已由TC成功创建。")
        await save_terminal_sessions()
        return {
            "success": True,
            "terminal_id": terminal_id,
            "message": response.get("message", "终端已创建。"),
            "visible_window_info": response.get("visible_window_info")
        }
    else:
        logger.error(f"创建终端失败。TC响应: {response}")
        return {
            "success": False,
            "terminal_id": None,
            "message": response.get("message", "创建终端失败。")
        }


@mcp_server_instance.tool()
async def close_terminal(ctx: Context, terminal_id: str) -> Dict[str, Any]:
    """
    关闭指定的终端会话。你需要提供要关闭的终端的`terminal_id`。
    关闭后，该终端将无法再进行操作。终端模拟器窗口可能会自动关闭，也可能需要手动关闭。

    Args:
        terminal_id: 要关闭的终端的ID。

    Returns:
        一个包含 'success' (bool) 和 'message' (str) 的字典。
    """
    logger.info(f"TS: MCP工具 'close_terminal' 被调用，terminal_id: {terminal_id}")
    if terminal_id not in active_terminals:
        return {
            "success": False,
            "message": f"终端ID '{terminal_id}' 未找到或非活动状态。"
        }

    tmux_session_name = active_terminals[terminal_id]["tmux_session_name"]
    tc_close_timeout = 15.0
    payload = {
        "terminal_id": terminal_id,
        "tmux_session_name": tmux_session_name,
    }
    response = await send_to_tc("close_terminal",
                                payload,
                                request_timeout=tc_close_timeout)

    if response.get("success"):
        logger.info(f"终端 {terminal_id} (tmux: {tmux_session_name}) 已由TC成功关闭。")
        if terminal_id in active_terminals: del active_terminals[terminal_id]
        await save_terminal_sessions()
        return {"success": True, "message": response.get("message", "终端已关闭。")}
    else:
        if "not found" in response.get(
                "message", "").lower() and terminal_id in active_terminals:
            logger.warning(
                f"TC在关闭期间报告终端 {terminal_id} (tmux: {tmux_session_name}) 未找到。从活动列表中移除。"
            )
            del active_terminals[terminal_id]
        logger.error(f"关闭终端 {terminal_id} 失败。TC响应: {response}")
        return {
            "success": False,
            "message": response.get("message", f"关闭终端 {terminal_id} 失败。")
        }


@mcp_server_instance.tool()
async def send_command_to_terminal(
        ctx: Context,
        terminal_id: str,
        command: str,
        wait_seconds_after_command: float = 1.5,
        strip_ansi_for_snapshot: bool = True) -> Dict[str, Any]:
    """
    向指定的终端发送一个命令，等待指定时间后捕获并返回该终端的完整屏幕快照。
    这个工具适合执行命令并立即获取结果。对于长时间运行的命令（如sqlmap），建议：
    1. 先发送命令
    2. 然后使用 get_terminal_snapshot 工具定期检查终端输出

    Args:
        terminal_id: 目标终端的ID。
        command: 要发送的命令字符串。
        wait_seconds_after_command: (可选) 发送命令后等待多少秒再捕获快照。默认为1.5秒。
            对于耗时较长的命令，可以适当增加此值以确保命令有足够时间产生输出。
        strip_ansi_for_snapshot: (可选) 是否从返回的快照中移除ANSI颜色等转义序列。默认为True。

    Returns:
        一个包含以下键的字典:
        - success (bool): 命令是否成功发送以及是否已捕获快照。
        - message (str): 操作结果的消息。
        - snapshot_content (Optional[str]): 捕获到的终端屏幕内容。如果捕获失败则为None。
        - ansi_stripped (bool): 指示返回的快照内容是否已移除ANSI代码。
    """
    logger.info(
        f"TS: MCP工具 'send_command_to_terminal' 被调用。Terminal ID: '{terminal_id}', Command: '{command}', Wait: {wait_seconds_after_command}s, Strip ANSI: {strip_ansi_for_snapshot}"
    )
    if terminal_id not in active_terminals:
        return {
            "success": False,
            "message": f"终端ID '{terminal_id}' 未找到。",
            "snapshot_content": None,
            "ansi_stripped": strip_ansi_for_snapshot
        }

    tmux_session_name = active_terminals[terminal_id]["tmux_session_name"]
    tc_processing_timeout = wait_seconds_after_command + 10.0

    payload = {
        "terminal_id": terminal_id,
        "tmux_session_name": tmux_session_name,
        "command": command,
        "wait_seconds_after_command": wait_seconds_after_command,
        "strip_ansi_for_snapshot": strip_ansi_for_snapshot,
    }
    response = await send_to_tc("send_command_and_snapshot",
                                payload,
                                request_timeout=tc_processing_timeout)

    return {
        "success":
        response.get("success", False),
        "message":
        response.get("message", "处理命令或获取快照失败。"),
        "snapshot_content":
        response.get("snapshot_content"),
        "ansi_stripped":
        response.get(
            "ansi_stripped_by_tc",
            strip_ansi_for_snapshot if response.get("success") else False)
    }


@mcp_server_instance.tool()
async def send_keystrokes_to_terminal(
    ctx: Context,
    terminal_id: str,
    keys: List[str],
) -> Dict[str, Any]:
    """
    向指定的终端发送一系列原始按键。这对于与交互式提示符交互或发送控制字符（如Ctrl+C）非常有用。
    支持的特殊按键名包括：'CTRL_C', 'CTRL_L', 'CTRL_D', 'ENTER', 'TAB', 'UP_ARROW', 'DOWN_ARROW', 'LEFT_ARROW', 'RIGHT_ARROW', 'BACKSPACE', 'DELETE', 'ESCAPE', 'SPACE'。
    普通字符可以直接作为字符串发送。

    Args:
        terminal_id: 目标终端的ID。
        keys: 一个字符串列表，每个字符串代表一个要发送的按键 (例如: ["ls", " ", "-l", "ENTER"] 或 ["CTRL_C"])。

    Returns:
        一个包含 'success' (bool) 和 'message' (str) 的字典。
    """
    logger.info(
        f"TS: MCP工具 'send_keystrokes_to_terminal' 被调用。Terminal ID: '{terminal_id}', Keys: {keys}"
    )
    if terminal_id not in active_terminals:
        return {"success": False, "message": f"终端ID '{terminal_id}' 未找到。"}

    tmux_session_name = active_terminals[terminal_id]["tmux_session_name"]
    tc_keystroke_timeout = 10.0
    payload = {
        "terminal_id": terminal_id,
        "tmux_session_name": tmux_session_name,
        "keys": keys,
    }
    return await send_to_tc("send_keystrokes",
                            payload,
                            request_timeout=tc_keystroke_timeout)


RESOURCE_SCHEME = "api"


@mcp_server_instance.resource(
    f"{RESOURCE_SCHEME}://terminal/{{terminal_id}}/snapshot")
async def get_terminal_snapshot(terminal_id: str) -> str:
    """
    获取指定终端当前可见内容的快照。这是一个实时监控工具，你可以随时调用它来查看终端的最新输出，
    无论命令是否执行完毕。特别适合：
    1. 监控长时间运行的命令（如sqlmap、nmap等）
    2. 检查命令执行进度
    3. 获取终端当前状态

    注意：此工具只返回当前可见的终端内容，不会等待命令执行完成。
    如果需要持续监控命令输出，建议定期调用此工具。

    Args:
        terminal_id: 要获取快照的终端ID。

    Returns:
        终端当前可见的文本内容。如果终端不存在或发生错误，将返回错误信息。
    """
    strip_ansi_for_resource_call = True
    logger.info(
        f"TS: MCP资源 '{RESOURCE_SCHEME}://terminal/{terminal_id}/snapshot' 被调用。固定strip_ansi: {strip_ansi_for_resource_call}"
    )

    if terminal_id not in active_terminals:
        return f"错误: 终端ID '{terminal_id}' 未找到。"

    tmux_session_name = active_terminals[terminal_id]["tmux_session_name"]
    tc_snapshot_timeout = 15.0
    payload = {
        "terminal_id": terminal_id,
        "tmux_session_name": tmux_session_name,
        "strip_ansi": strip_ansi_for_resource_call,
        "scope": "visible",
    }
    response = await send_to_tc("get_snapshot",
                                payload,
                                request_timeout=tc_snapshot_timeout)
    if response.get("success"):
        content = response.get("snapshot_content")
        return content if isinstance(
            content, str) else f"错误: 未找到或无效的快照内容 {terminal_id}。"
    else:
        return f"获取快照错误 {terminal_id}: {response.get('message', '未知的TC错误')}"


@mcp_server_instance.resource(
    f"{RESOURCE_SCHEME}://terminal/{{terminal_id}}/context")
async def get_terminal_context(terminal_id: str) -> Dict[str, Any]:
    """
    获取指定终端的上下文信息，包括当前工作目录、终端状态等。
    这个工具可以帮助你了解终端的当前状态，比如：
    1. 终端是否在等待输入
    2. 当前工作目录
    3. 终端是否处于活动状态

    Args:
        terminal_id: 要获取上下文信息的终端ID。

    Returns:
        一个包含以下信息的字典:
        - success (bool): 操作是否成功
        - terminal_id (str): 终端ID
        - purpose (str): 终端用途
        - status (str): 终端状态
        - current_working_directory (str): 当前工作目录
        - is_awaiting_input (bool): 是否在等待输入
        - last_status_check (str): 最后状态检查时间
        - visible_window_info (dict): 可见窗口信息
    """
    logger.info(
        f"TS: MCP资源 '{RESOURCE_SCHEME}://terminal/{terminal_id}/context' 被调用。")
    if terminal_id not in active_terminals:
        return {"success": False, "message": f"终端ID '{terminal_id}' 未找到。"}

    term_info = active_terminals[terminal_id]

    # 获取最新状态
    status_info = await check_terminal_status(terminal_id, term_info)

    # 更新终端信息
    if status_info["status"] != "closed":
        active_terminals[terminal_id].update({
            "status":
            status_info["status"],
            "last_status_check":
            status_info["timestamp"],
            "current_working_directory":
            status_info.get("current_working_directory"),
            "is_awaiting_input":
            status_info.get("is_awaiting_input", False),
            "last_content":
            status_info.get("last_content")
        })
    else:
        if terminal_id in active_terminals:
            del active_terminals[terminal_id]
        await save_terminal_sessions()
        return {"success": False, "message": f"终端 {terminal_id} 已关闭。"}

    return {
        "success": True,
        "terminal_id": terminal_id,
        "purpose": term_info.get("purpose"),
        "status": status_info["status"],
        "current_working_directory":
        status_info.get("current_working_directory"),
        "is_awaiting_input": status_info.get("is_awaiting_input", False),
        "last_status_check": status_info["timestamp"],
        "visible_window_info": term_info.get("visible_window_info")
    }


@mcp_server_instance.resource(f"{RESOURCE_SCHEME}://terminals/list")
async def list_active_terminals() -> Dict[str, Any]:
    """
    列出当前由此服务器管理的所有活动终端。
    这个工具可以帮助你：
    1. 查看所有可用的终端
    2. 获取终端的基本信息
    3. 在忘记终端ID时找到正确的终端

    Returns:
        一个包含以下信息的字典:
        - success (bool): 操作是否成功
        - active_terminals (dict): 活动终端列表，每个终端包含其ID、用途、状态等信息
    """
    logger.info(f"TS: MCP资源 '{RESOURCE_SCHEME}://terminals/list' 被调用。")
    return {"success": True, "active_terminals": dict(active_terminals)}


# 添加会话持久化相关函数
async def save_terminal_sessions():
    """保存当前活动的终端会话到文件"""
    sessions_file = Path("terminal_sessions.json")
    sessions_data = {
        "terminals": active_terminals,
        "timestamp": str(datetime.datetime.now())
    }
    try:
        with open(sessions_file, 'w', encoding='utf-8') as f:
            json.dump(sessions_data, f, ensure_ascii=False, indent=2)
        logger.info(f"已保存 {len(active_terminals)} 个终端会话到文件")
    except Exception as e:
        logger.error(f"保存终端会话失败: {e}")


async def load_terminal_sessions():
    """从文件加载终端会话"""
    sessions_file = Path("terminal_sessions.json")
    if not sessions_file.exists():
        return

    try:
        with open(sessions_file, 'r', encoding='utf-8') as f:
            sessions_data = json.load(f)

        # 验证会话是否仍然有效
        for terminal_id, term_info in sessions_data.get("terminals",
                                                        {}).items():
            success, _, _ = await run_tmux_command_for_session(
                term_info["tmux_session_name"],
                ["has-session", "-t", term_info["tmux_session_name"]],
                timeout=2.0)
            if success:
                active_terminals[terminal_id] = term_info
                logger.info(f"已恢复终端会话: {terminal_id}")
            else:
                logger.warning(f"终端会话 {terminal_id} 已失效，跳过恢复")

        logger.info(f"已从文件加载 {len(active_terminals)} 个终端会话")
    except Exception as e:
        logger.error(f"加载终端会话失败: {e}")


async def check_terminal_status(terminal_id: str,
                                term_info: Dict[str, Any]) -> Dict[str, Any]:
    """检查单个终端的状态"""
    tmux_session_name = term_info["tmux_session_name"]
    current_status = term_info.get("status", "unknown")

    try:
        # 检查 tmux 会话是否存在
        success, _, _ = await run_tmux_command_for_session(
            tmux_session_name, ["has-session", "-t", tmux_session_name],
            timeout=TERMINAL_STATUS_TIMEOUT)

        if not success:
            return {
                "terminal_id": terminal_id,
                "status": "closed",
                "reason": "tmux_session_not_found",
                "timestamp": str(datetime.datetime.now())
            }

        # 获取当前工作目录
        cwd = await get_cwd_for_session(tmux_session_name)

        # 获取终端内容
        content = await get_tmux_pane_content_for_session(tmux_session_name,
                                                          strip_ansi=True)

        # 检查终端是否在等待输入
        is_awaiting_input = False
        if content:
            last_line = content.strip().split('\n')[-1]
            # 检查是否以常见的提示符结尾
            is_awaiting_input = any(
                last_line.endswith(prompt) for prompt in ['$', '#', '>', '%'])

        return {
            "terminal_id": terminal_id,
            "status": "active",
            "current_working_directory": cwd,
            "is_awaiting_input": is_awaiting_input,
            "last_content": content,
            "timestamp": str(datetime.datetime.now())
        }

    except Exception as e:
        logger.error(f"检查终端 {terminal_id} 状态时出错: {e}")
        return {
            "terminal_id": terminal_id,
            "status": "error",
            "reason": str(e),
            "timestamp": str(datetime.datetime.now())
        }


async def monitor_terminal_statuses():
    """定期监控所有终端的状态"""
    while not TS_SHUTDOWN_EVENT.is_set():
        try:
            for terminal_id, term_info in list(active_terminals.items()):
                status_info = await check_terminal_status(
                    terminal_id, term_info)

                # 更新终端状态
                if status_info["status"] == "closed":
                    logger.warning(f"终端 {terminal_id} 已关闭，从活动列表中移除")
                    if terminal_id in active_terminals:
                        del active_terminals[terminal_id]
                    await save_terminal_sessions()
                else:
                    # 更新终端信息
                    active_terminals[terminal_id].update({
                        "status":
                        status_info["status"],
                        "last_status_check":
                        status_info["timestamp"],
                        "current_working_directory":
                        status_info.get("current_working_directory"),
                        "is_awaiting_input":
                        status_info.get("is_awaiting_input", False),
                        "last_content":
                        status_info.get("last_content")
                    })

            # 保存更新后的会话状态
            await save_terminal_sessions()

        except Exception as e:
            logger.error(f"监控终端状态时出错: {e}")

        await asyncio.sleep(TERMINAL_STATUS_CHECK_INTERVAL)


if __name__ == "__main__":
    # 配置日志
    log_file = "app.log"

    # 创建日志目录（如果不存在）
    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # 配置根日志记录器
    logging.basicConfig(
        level=logging.INFO,
        format=
        "%(asctime)s - %(levelname)s - [%(name)s] - %(filename)s:%(lineno)d - %(message)s",
        handlers=[logging.FileHandler(log_file, encoding='utf-8')])

    # 禁用所有日志的终端输出
    for logger_name in logging.root.manager.loggerDict:
        logger = logging.getLogger(logger_name)
        logger.handlers = [
            h for h in logger.handlers if isinstance(h, logging.FileHandler)
        ]
        logger.propagate = False

    # 设置 uvicorn 的日志配置
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.handlers = [logging.FileHandler(log_file, encoding='utf-8')]
    uvicorn_logger.propagate = False

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = [logging.FileHandler(log_file, encoding='utf-8')]
    access_logger.propagate = False

    error_logger = logging.getLogger("uvicorn.error")
    error_logger.handlers = [logging.FileHandler(log_file, encoding='utf-8')]
    error_logger.propagate = False

    # 禁用 uvicorn 的默认日志输出
    uvicorn_logger.setLevel(logging.WARNING)
    access_logger.setLevel(logging.WARNING)
    error_logger.setLevel(logging.WARNING)

    class StatusIndicator:

        def __init__(self):
            self.console = Console()
            self.layout = Layout()
            self.status_lines = []
            self.max_lines = 10
            self.last_update_time = None
            self.is_shutting_down = False
            self.live = None

        def create_header(self):
            """创建服务器信息头部"""
            header = Table.grid(padding=(0, 1))
            header.add_column("header", style="bold cyan")
            header.add_row("🚀 MCP多终端服务器")
            header.add_row(f"📡 服务器地址: http://{args.host}:{args.port}")
            header.add_row(f"📥 SSE GET端点: {DEFAULT_SSE_PATH}")
            header.add_row(f"📤 SSE POST消息端点: {DEFAULT_POST_MESSAGE_PATH}")
            return Panel(header, title="服务器信息", border_style="cyan", width=80)

        def create_status_section(self):
            """创建状态监控部分"""
            status_table = Table.grid(padding=(0, 1))
            status_table.add_column("status", style="bold", width=3)
            status_table.add_column("message", width=75)

            for line in self.status_lines:
                status_table.add_row(line["status"], line["message"])

            if self.last_update_time:
                status_table.add_row(
                    "", f"最后更新: {self.last_update_time.strftime('%H:%M:%S')}")

            return Panel(status_table,
                         title="状态监控",
                         border_style="green",
                         width=80)

        def update_status(self, message, status="running", section="main"):
            """更新状态信息"""
            current_time = datetime.datetime.now()

            if status == "running":
                status_icon = Spinner("dots", text=message)
            elif status == "success":
                status_icon = "✅"
            elif status == "error":
                status_icon = "❌"
            elif status == "warning":
                status_icon = "⚠️"
            else:
                status_icon = "•"

            status_line = {"status": status_icon, "message": message}

            if section == "main":
                if self.status_lines:
                    self.status_lines[0] = status_line
                else:
                    self.status_lines = [status_line]
            else:
                if len(self.status_lines) >= self.max_lines:
                    self.status_lines.pop()
                self.status_lines.insert(1, status_line)

            self.last_update_time = current_time
            self.refresh_display()

        def refresh_display(self):
            """刷新显示"""
            if not self.live:
                self.live = Live(self.create_layout(),
                                 console=self.console,
                                 refresh_per_second=4,
                                 vertical_overflow="visible",
                                 auto_refresh=True)
                self.live.start()
            else:
                self.live.update(self.create_layout())

        def create_layout(self):
            """创建整体布局"""
            self.layout.split(
                Layout(name="header", size=6),
                Layout(name="status", size=len(self.status_lines) + 3))

            self.layout["header"].update(self.create_header())
            self.layout["status"].update(self.create_status_section())

            return self.layout

        def show_server_info(self, host, port, sse_path, post_path):
            """显示服务器信息"""
            self.console.clear()
            self.console.print(self.create_header())

        def clear_status(self):
            """清除所有状态"""
            self.status_lines = []
            self.last_update_time = None
            self.is_shutting_down = False
            if self.live:
                self.live.stop()
                self.live = None

    status = StatusIndicator()

    parser = argparse.ArgumentParser(description='运行MCP多终端服务器')
    parser.add_argument('--host',
                        default='0.0.0.0',
                        help='绑定的主机 (默认: 0.0.0.0)')
    parser.add_argument('--port',
                        type=int,
                        default=int(os.getenv("MCP_PORT", "10002")),
                        help='监听的端口 (默认: 10002 或 MCP_PORT环境变量)')
    parser.add_argument('--tc-script-path',
                        default=None,
                        help='终端客户端.py的绝对路径 (可选, 覆盖默认搜索)')
    parser.add_argument(
        '--mcp-tc-config-path',
        default=None,
        help='tc_config.json的绝对路径 (可选, 会被设置为MCP_TC_CONFIG_PATH环境变量)')

    args = parser.parse_args()

    if args.tc_script_path:
        os.environ["TC_SCRIPT_PATH"] = args.tc_script_path
        logger.info(f"TC_SCRIPT_PATH环境变量已设置为: {args.tc_script_path}")

    if args.mcp_tc_config_path:
        os.environ["MCP_TC_CONFIG_PATH"] = args.mcp_tc_config_path
        logger.info(f"MCP_TC_CONFIG_PATH环境变量已设置为: {args.mcp_tc_config_path}")

    DEFAULT_SSE_PATH = "/sse"
    DEFAULT_POST_MESSAGE_PATH = "/messages/"

    logger.info(f"启动MCP多终端服务器于 http://{args.host}:{args.port}")
    logger.info(f"SSE GET端点: {DEFAULT_SSE_PATH}")
    logger.info(f"SSE POST消息端点: {DEFAULT_POST_MESSAGE_PATH}")

    # 添加美化输出
    print("\n" + "=" * 50)
    print("🚀 MCP多终端服务器启动")
    print("=" * 50)
    print(f"📡 服务器地址: http://{args.host}:{args.port}")
    print(f"📥 SSE GET端点: {DEFAULT_SSE_PATH}")
    print(f"📤 SSE POST消息端点: {DEFAULT_POST_MESSAGE_PATH}")
    print("=" * 50 + "\n")

    sse_transport = SseServerTransport(DEFAULT_POST_MESSAGE_PATH)

    async def handle_sse_connection(request: Request) -> None:
        low_level_mcp_server: LowLevelServer = mcp_server_instance._mcp_server
        async with sse_transport.connect_sse(
                request.scope,
                request.receive,
                request._send,
        ) as (read_stream, write_stream):
            init_options = low_level_mcp_server.create_initialization_options()
            await low_level_mcp_server.run(read_stream, write_stream,
                                           init_options)

    root_starlette_app = Starlette(routes=[
        Route(DEFAULT_SSE_PATH,
              endpoint=handle_sse_connection,
              methods=["GET"]),
        Mount(DEFAULT_POST_MESSAGE_PATH, app=sse_transport.handle_post_message)
    ],
                                   lifespan=starlette_lifespan_wrapper)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    config = uvicorn.Config(
        app=root_starlette_app,
        host=args.host,
        port=args.port,
        loop="asyncio",
        lifespan="on",
        log_level="warning",  # 设置 uvicorn 的日志级别为 warning
        access_log=False,  # 禁用访问日志
        log_config=None  # 禁用默认日志配置
    )
    server = uvicorn.Server(config=config)

    should_exit_event = asyncio.Event()

    def signal_handler(sig: int, frame: Optional[Any]):
        print("\n正在优雅关闭服务器...")
        should_exit_event.set()
        TS_SHUTDOWN_EVENT.set()
        if hasattr(server, 'should_exit'):
            server.should_exit = True

    for sig_name in ('SIGINT', 'SIGTERM'):
        sig_val = getattr(signal, sig_name, None)
        if sig_val:
            try:
                signal.signal(sig_val, signal_handler)
            except ValueError:
                try:
                    loop.add_signal_handler(
                        sig_val, lambda s=sig_val: signal_handler(s, None))
                except RuntimeError as e:
                    logger.warning(
                        f"无法为 {sig_name} 设置asyncio信号处理器: {e}。Uvicorn可能会处理它。")
            except Exception as e_sig:
                logger.error(f"为 {sig_name} 设置信号处理器时出错: {e_sig}")

    async def main_server_runner():
        server_task = asyncio.create_task(server.serve())
        try:
            await server_task
        except asyncio.CancelledError:
            status.update_status("服务器正在关闭...", "warning", "main")
            status.refresh_display()
        except Exception as e:
            logger.error(f"Uvicorn server.serve() 任务因异常结束: {e}", exc_info=True)
            should_exit_event.set()

        if should_exit_event.is_set():
            status.update_status("服务器已停止", "success", "main")
            status.refresh_display()

        if not server_task.done():
            server_task.cancel()
            try:
                await asyncio.wait_for(server_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception as e_final_await:
                logger.error(f"最终等待Uvicorn服务器任务时出错: {e_final_await}")

    try:
        status.show_server_info(args.host, args.port, DEFAULT_SSE_PATH,
                                DEFAULT_POST_MESSAGE_PATH)
        status.update_status("服务器正在启动...", "running", "main")
        status.refresh_display()
        loop.run_until_complete(main_server_runner())
    except KeyboardInterrupt:
        status.update_status("正在关闭服务器...", "warning", "main")
        status.refresh_display()
    except Exception as e:
        logger.critical(f"TS服务器在主执行块中崩溃: {e}", exc_info=True)
        status.update_status(f"服务器崩溃: {str(e)}", "error", "main")
        status.refresh_display()
    finally:
        if not TS_SHUTDOWN_EVENT.is_set():
            TS_SHUTDOWN_EVENT.set()

        for fut_id in list(request_futures.keys()):
            fut = request_futures.pop(fut_id, None)
            if fut and not fut.done():
                fut.cancel("TS在main中进行最终清理。")

        if loop and not loop.is_closed():
            try:
                if loop.is_running():
                    loop.run_until_complete(asyncio.sleep(0.2))
            except RuntimeError:
                pass

            try:
                current_task = asyncio.current_task(
                    loop=loop) if loop and not loop.is_closed() else None
                tasks = [
                    t for t in asyncio.all_tasks(loop=loop)
                    if t is not current_task and not t.done()
                ]
                if tasks:
                    for task in tasks:
                        task.cancel()
                    if loop.is_running():
                        loop.run_until_complete(
                            asyncio.gather(*tasks, return_exceptions=True))

                if loop.is_running():
                    loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            finally:
                if not loop.is_closed():
                    if loop.is_running():
                        loop.stop()
                    loop.close()

        status.clear_status()
        status.update_status("服务器已完全关闭", "success", "main")
        status.refresh_display()
