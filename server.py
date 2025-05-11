import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional, List

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


@asynccontextmanager
async def tc_subprocess_lifespan_manager(
        mcp_server: FastMCP) -> AsyncIterator[None]:
    global tc_process, tc_writer, tc_reader, tc_stderr_task, tc_stdout_handler_task, TS_SHUTDOWN_EVENT, active_terminals
    TS_SHUTDOWN_EVENT.clear()
    active_terminals.clear()
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
        ping_response = await send_to_tc("ping_tc", {}, request_timeout=15.0)
        if not ping_response.get("success") or ping_response.get(
                "message") != "pong":
            raise RuntimeError(f"TC未响应ping或报告未就绪: {ping_response}")
        logger.info(f"TC已就绪。状态: {ping_response.get('tc_status')}")
        logger.info("===================================================")
        logger.info("=== 终端服务器和客户端均已准备就绪并可操作 ===")
        logger.info("===================================================")

    except Exception as e_startup:
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
    TS_SHUTDOWN_EVENT.set()

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
    向指定的终端发送一个命令（会自动在命令后附加回车执行），等待一段时间后捕获并返回该终端的完整屏幕快照。
    这使你能够看到命令执行后的结果。

    Args:
        terminal_id: 目标终端的ID。
        command: 要发送的命令字符串。
        wait_seconds_after_command: (可选) 发送命令后等待多少秒再捕获快照。默认为1.5秒。对于耗时较长的命令，可以适当增加此值以确保命令有足够时间产生输出。
        strip_ansi_for_snapshot: (可选) 是否从返回的快照中移除ANSI颜色等转义序列。默认为True，以获取纯文本内容。

    Returns:
        一个包含以下键的字典:
        - success (bool): 命令是否成功发送以及是否（可能）已捕获快照。
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
    获取指定终端当前可见内容的快照。你需要提供`terminal_id`。
    此资源调用目前默认移除ANSI颜色代码。如果需要控制ANSI代码的移除，请使用`send_command_to_terminal`工具并在其参数中指定。
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
    获取指定终端的上下文信息。你需要提供`terminal_id`。
    返回的信息包括当前工作目录（CWD）、终端用途（purpose）、状态以及可见窗口信息。
    """
    logger.info(
        f"TS: MCP资源 '{RESOURCE_SCHEME}://terminal/{terminal_id}/context' 被调用。")
    if terminal_id not in active_terminals:
        return {"success": False, "message": f"终端ID '{terminal_id}' 未找到。"}

    term_info = active_terminals[terminal_id]
    tmux_session_name = term_info["tmux_session_name"]
    tc_context_timeout = 10.0
    payload = {
        "terminal_id": terminal_id,
        "tmux_session_name": tmux_session_name
    }
    response = await send_to_tc("get_context",
                                payload,
                                request_timeout=tc_context_timeout)

    if response.get("success"):
        response["purpose"] = term_info.get("purpose")
        response["terminal_id"] = terminal_id
        response["status"] = term_info.get("status", "unknown")
        response["visible_window_info"] = term_info.get(
            "visible_window_info", response.get("visible_window_info"))
    return response


@mcp_server_instance.resource(f"{RESOURCE_SCHEME}://terminals/list")
async def list_active_terminals() -> Dict[str, Any]:
    """
    列出当前由此服务器管理的所有活动终端。
    返回每个终端的ID、用途、状态和可见窗口信息。
    当你需要知道有哪些终端可用或者想操作一个已存在的终端但忘记了其ID时，可以使用此功能。
    """
    logger.info(f"TS: MCP资源 '{RESOURCE_SCHEME}://terminals/list' 被调用。")
    return {"success": True, "active_terminals": dict(active_terminals)}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format=
        "%(asctime)s - TS_LOG - %(levelname)s - [%(name)s] - %(filename)s:%(lineno)d - %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

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

    config = uvicorn.Config(app=root_starlette_app,
                            host=args.host,
                            port=args.port,
                            loop="asyncio",
                            lifespan="on",
                            log_level="info")
    server = uvicorn.Server(config=config)

    should_exit_event = asyncio.Event()

    def signal_handler(sig: int, frame: Optional[Any]):
        logger.info(f"操作系统信号 {sig} 被TS接收。启动优雅关闭。")
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
            logger.info("Uvicorn服务器任务被取消。")
        except Exception as e:
            logger.error(f"Uvicorn server.serve() 任务因异常结束: {e}", exc_info=True)
            should_exit_event.set()

        if should_exit_event.is_set():
            logger.info("关闭信号已处理，Uvicorn服务器已停止或正在停止。")

        if not server_task.done():
            logger.info("Uvicorn服务器任务未完成，尝试再次取消。")
            server_task.cancel()
            try:
                await asyncio.wait_for(server_task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.error("等待Uvicorn服务器任务完成取消超时。")
            except asyncio.CancelledError:
                logger.info("Uvicorn服务器任务在最终检查期间成功取消。")
            except Exception as e_final_await:
                logger.error(f"最终等待Uvicorn服务器任务时出错: {e_final_await}")

    try:
        logger.info("启动主服务器运行程序 (TS)...")
        loop.run_until_complete(main_server_runner())
    except KeyboardInterrupt:
        logger.info("TS服务器在主块中捕获到KeyboardInterrupt。")
    except Exception as e:
        logger.critical(f"TS服务器在主执行块中崩溃: {e}", exc_info=True)
    finally:
        logger.info("TS服务器主执行块 'finally' 到达。确保完全清理。")
        if not TS_SHUTDOWN_EVENT.is_set(): TS_SHUTDOWN_EVENT.set()

        for fut_id in list(request_futures.keys()):
            fut = request_futures.pop(fut_id, None)
            if fut and not fut.done(): fut.cancel("TS在main中进行最终清理。")

        if loop and not loop.is_closed():
            try:
                if loop.is_running():
                    loop.run_until_complete(asyncio.sleep(0.2))
            except RuntimeError as e_loop_sleep:
                logger.debug(f"最终清理期间的循环休眠: {e_loop_sleep}")

            try:
                current_task = asyncio.current_task(
                    loop=loop) if loop and not loop.is_closed() else None
                tasks = [
                    t for t in asyncio.all_tasks(loop=loop)
                    if t is not current_task and not t.done()
                ]
                if tasks:
                    logger.debug(f"在最终TS清理期间取消 {len(tasks)} 个未完成的任务。")
                    for task in tasks:
                        task.cancel()
                    if loop.is_running():
                        loop.run_until_complete(
                            asyncio.gather(*tasks, return_exceptions=True))
                    else:
                        logger.warning("循环已停止，无法完全等待剩余任务的取消。")

                if loop.is_running():
                    logger.debug("在TS中关闭异步生成器。")
                    loop.run_until_complete(loop.shutdown_asyncgens())
                else:
                    logger.warning("循环已停止，无法关闭异步生成器。")

            except RuntimeError as e_loop_state:
                logger.warning(
                    f"最终循环任务/生成器清理期间的RuntimeError: {e_loop_state} (循环可能已关闭或停止)"
                )
            except Exception as e_final_cleanup_tasks:
                logger.error(
                    f"最终asyncio任务/生成器清理期间的异常: {e_final_cleanup_tasks}",
                    exc_info=True)
            finally:
                if not loop.is_closed():
                    if loop.is_running():
                        loop.stop()
                    loop.close()
                    logger.info("Asyncio事件循环在TS最终清理中关闭。")

        logger.info("TS服务器已完全关闭。")
