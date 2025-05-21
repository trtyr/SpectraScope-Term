import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import uuid
import shutil
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format=
    "%(asctime)s - TC_LOG - %(levelname)s - [%(name)s] - %(filename)s:%(lineno)d - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("MCPMultiTerminalClient_TC")

DEFAULT_TERMINAL_COMMAND_TEMPLATES: Dict[str, List[str]] = {
    "xfce4-terminal":
    ["xfce4-terminal", "--disable-server", "--title", "{title}", "--command"],
    "gnome-terminal":
    ["gnome-terminal", "--disable-factory", "--title={title}", "--"],
    "konsole": ["konsole", "--new-tab", "-p", "tabtitle={title}", "-e"],
    "qterminal": ["qterminal", "-e"],
    "xterm": ["xterm", "-T", "{title}", "-e"],
    "kitty": ["kitty", "--title", "{title}", "-e"],
    "alacritty": ["alacritty", "--title", "{title}", "-e"],
}
DEFAULT_SHELL_COMMAND = "bash -il"

CONFIG_FILE_NAME = "tc_config.json"
DEFAULT_CONFIG_PATH: Path

env_config_path_str = os.getenv("MCP_TC_CONFIG_PATH")
if env_config_path_str:
    logger.info(
        f"TC_DIAG: Found MCP_TC_CONFIG_PATH environment variable: '{env_config_path_str}'"
    )

    if os.path.isabs(env_config_path_str):
        DEFAULT_CONFIG_PATH = Path(env_config_path_str)
        logger.info(
            f"TC_DIAG: Using absolute config path from environment: '{DEFAULT_CONFIG_PATH}'"
        )
    else:
        logger.warning(
            f"TC_DIAG: MCP_TC_CONFIG_PATH ('{env_config_path_str}') is not an absolute path. Falling back to script directory."
        )

        script_dir = Path(__file__).resolve().parent
        DEFAULT_CONFIG_PATH = script_dir / CONFIG_FILE_NAME
        logger.info(
            f"TC_DIAG: Using config path relative to script directory: '{DEFAULT_CONFIG_PATH}'"
        )
else:

    script_dir = Path(__file__).resolve().parent
    DEFAULT_CONFIG_PATH = script_dir / CONFIG_FILE_NAME
    logger.info(
        f"TC_DIAG: MCP_TC_CONFIG_PATH not set. Defaulting to config file in script directory: '{DEFAULT_CONFIG_PATH}'"
    )

logger.info(
    f"TC_DIAG: Final DEFAULT_CONFIG_PATH determined as: '{DEFAULT_CONFIG_PATH}', type: {type(DEFAULT_CONFIG_PATH)}"
)

SHUTDOWN_EVENT = asyncio.Event()
ANSI_ESCAPE_PATTERN = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

managed_terminals: Dict[str, Dict[str, Any]] = {}
tc_writer_global: Optional[asyncio.StreamWriter] = None

user_config: Dict[str, Any] = {}


def get_process_environment() -> Dict[str, str]:
    """
    获取当前环境的一个副本，并确保设置了推荐的UTF-8区域设置。
    """
    env = os.environ.copy()
    utf8_locale_to_set = "C.UTF-8"

    env["LANG"] = utf8_locale_to_set
    env["LC_ALL"] = utf8_locale_to_set
    env["LC_CTYPE"] = utf8_locale_to_set
    logger.info(
        f"Setting LANG/LC_ALL/LC_CTYPE to '{utf8_locale_to_set}' for subprocess."
    )
    return env


def load_user_config(
        config_path: Path = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    """加载用户配置文件"""
    global DEFAULT_SHELL_COMMAND
    loaded_config: Dict[str, Any] = {
        "terminal_command": None,
        "preferred_terminal": None,
        "terminal_templates": {},
        "default_shell_command": DEFAULT_SHELL_COMMAND,
    }

    logger.info(f"Attempting to load user config from: '{config_path}'")

    if config_path.exists() and config_path.is_file():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            direct_cmd_template = data.get("terminal_command")
            if isinstance(direct_cmd_template, list) and all(
                    isinstance(item, str) for item in direct_cmd_template):
                loaded_config["terminal_command"] = direct_cmd_template
                logger.info(
                    f"从配置文件加载直接终端命令terminal_command: {direct_cmd_template}")

            if not loaded_config["terminal_command"]:
                loaded_config["preferred_terminal"] = data.get(
                    "preferred_terminal")
                if isinstance(loaded_config["preferred_terminal"], str):
                    logger.info(
                        f"从配置文件加载首选终端: {loaded_config['preferred_terminal']}")
                else:
                    loaded_config["preferred_terminal"] = None

                user_templates = data.get("terminal_templates", {})
                if isinstance(user_templates, dict):
                    loaded_config["terminal_templates"] = user_templates
                    logger.info(
                        f"从配置文件加载了 {len(user_templates)} 个用户终端terminal_command。"
                    )

            custom_shell = data.get("default_shell_command")
            if isinstance(custom_shell, str) and custom_shell.strip():
                loaded_config["default_shell_command"] = custom_shell
                DEFAULT_SHELL_COMMAND = custom_shell
                logger.info(f"从配置文件加载默认shell命令: {custom_shell}")

            logger.info(f"用户配置文件 '{config_path}' 加载成功。")
        except json.JSONDecodeError:
            logger.error(f"解析用户配置文件 '{config_path}' 失败。将使用默认设置。")
        except Exception as e:
            logger.error(f"加载用户配置文件 '{config_path}' 时发生错误: {e}。将使用默认设置。")
    else:
        logger.info(f"用户配置文件 '{config_path}' 未找到。将使用默认设置。")
    return loaded_config


def get_effective_terminal_templates() -> Dict[str, List[str]]:
    """合并内置terminal_command和用户terminal_command，用户terminal_command优先 (用于回退机制)"""
    effective_templates = {
        k: list(v)
        for k, v in DEFAULT_TERMINAL_COMMAND_TEMPLATES.items()
    }
    user_defined_templates = user_config.get("terminal_templates")
    if isinstance(user_defined_templates, dict):
        for name, template_list in user_defined_templates.items():
            if isinstance(template_list, list) and all(
                    isinstance(item, str) for item in template_list):
                effective_templates[name] = template_list
                logger.debug(f"使用用户terminal_command '{name}': {template_list}")
            else:
                logger.warning(f"配置文件中 '{name}' 的终端terminal_command格式无效，已忽略。")
    return effective_templates


def strip_ansi_codes(text: str) -> str:
    return ANSI_ESCAPE_PATTERN.sub('', text)


async def run_tmux_command_for_session(
        tmux_session_name: str,
        command_args: List[str],
        timeout: float = 10.0) -> Tuple[bool, str, str]:
    if SHUTDOWN_EVENT.is_set() and "kill-session" not in command_args:
        logger.warning(f"TC正在关闭，不运行tmux命令: {' '.join(command_args)}")
        return False, "", "TC正在关闭"

    session_management_cmds = [
        "new-session", "has-session", "list-sessions", "kill-session",
        "start-server"
    ]
    should_check_session_existence = not any(
        cmd_part in command_args for cmd_part in session_management_cmds)
    if "capture-pane" in command_args or "send-keys" in command_args or "display-message" in command_args:
        should_check_session_existence = True

    process_env = get_process_environment()

    if should_check_session_existence:
        try:
            proc_check = await asyncio.create_subprocess_exec(
                "tmux",
                "has-session",
                "-t",
                tmux_session_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=process_env)
            await asyncio.wait_for(proc_check.wait(), timeout=2.0)
            if proc_check.returncode != 0:
                logger.warning(
                    f"tmux会话 '{tmux_session_name}' 未找到，命令: {' '.join(command_args)}"
                )
                return False, "", f"会话 {tmux_session_name} 未找到"
        except asyncio.TimeoutError:
            logger.warning(f"检查tmux会话 '{tmux_session_name}' 超时。")
            return False, "", f"检查会话 {tmux_session_name} 超时"
        except FileNotFoundError:
            logger.error("未找到tmux命令。请确保已安装tmux并在PATH中。")
            return False, "", "未找到tmux"
        except Exception as e:
            logger.error(f"检查会话 '{tmux_session_name}' 时出错: {e}")
            return False, "", str(e)

    try:
        final_command_args = ["tmux"] + command_args

        logger.info(
            f"Executing tmux command for session '{tmux_session_name}': {' '.join(final_command_args)}"
        )
        process = await asyncio.create_subprocess_exec(
            *final_command_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=process_env)
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout)
        stdout = stdout_bytes.decode('utf-8', errors='replace').strip()
        stderr = stderr_bytes.decode('utf-8', errors='replace').strip()

        if process.returncode == 0:
            return True, stdout, stderr
        else:
            logger.error(
                f"tmux命令 '{' '.join(final_command_args)}' 失败，代码 {process.returncode}。Stderr: '{stderr}'。Stdout: '{stdout}'"
            )
            return False, stdout, stderr
    except asyncio.TimeoutError:
        logger.error(
            f"tmux命令 '{' '.join(final_command_args)}' 在 {timeout}秒后超时。")
        return False, "", f"执行超时: {' '.join(final_command_args)}"
    except FileNotFoundError:
        logger.error("未找到tmux命令。请确保已安装tmux并在PATH中。")
        return False, "", "未找到tmux"
    except Exception as e:
        logger.error(f"运行tmux命令 '{' '.join(final_command_args)}' 时发生异常: {e}",
                     exc_info=True)
        return False, "", str(e)


async def get_tmux_pane_content_for_session(tmux_session_name: str,
                                            strip_ansi: bool = False
                                            ) -> Optional[str]:
    # 获取历史记录大小
    success, history_size, _ = await run_tmux_command_for_session(
        tmux_session_name,
        ["display-message", "-p", "-t", f"{tmux_session_name}:0.0", "#{history_size}"],
        timeout=5.0
    )
    
    if not success:
        logger.warning(f"获取会话 {tmux_session_name} 的历史记录大小失败")
        return None
        
    try:
        history_size = int(history_size.strip())
    except ValueError:
        logger.warning(f"无效的历史记录大小: {history_size}")
        return None
        
    # 使用 -S 和 -E 参数捕获完整历史记录
    success, stdout, stderr = await run_tmux_command_for_session(
        tmux_session_name,
        ["capture-pane", "-ept", f"{tmux_session_name}:0.0", "-S", f"-{history_size}", "-E", "0"],
        timeout=5.0
    )
    
    if success:
        return strip_ansi_codes(stdout) if strip_ansi else stdout
    logger.warning(f"捕获会话 {tmux_session_name} 的面板内容失败: {stderr}")
    return None


async def get_cwd_for_session(tmux_session_name: str) -> Optional[str]:
    logger.info(f"获取tmux会话的CWD: {tmux_session_name}")
    success, stdout, stderr = await run_tmux_command_for_session(
        tmux_session_name, [
            "display-message", "-p", "-t", f"{tmux_session_name}:0.0",
            "#{pane_current_path}"
        ],
        timeout=3.0)
    if success and stdout:
        logger.info(f"会话 {tmux_session_name} 的CWD: '{stdout}'")
        return stdout.strip()
    logger.warning(
        f"获取会话 {tmux_session_name} 的CWD失败。Stdout: '{stdout}', Stderr: '{stderr}'"
    )
    return None


async def send_event_to_ts(event_type: str, event_data: Dict[str, Any]):
    global tc_writer_global
    if tc_writer_global and not tc_writer_global.is_closing():
        event_message = {
            "request_id": f"tc_event_{uuid.uuid4().hex[:8]}",
            "type": "tc_event",
            "payload": {
                "event_type": event_type,
                **event_data
            }
        }
        try:
            tc_writer_global.write(
                (json.dumps(event_message) + "\n").encode('utf-8'))
            await tc_writer_global.drain()
            logger.info(f"TC已发送事件到TS: {event_type}, 数据: {event_data}")
        except Exception as e:
            logger.error(f"发送事件到TS时出错: {e}", exc_info=True)
    else:
        logger.warning(f"无法发送事件 '{event_type}' 到TS: 写入器不可用。")


async def monitor_terminal_processes():
    global managed_terminals
    while not SHUTDOWN_EVENT.is_set():
        await asyncio.sleep(15)
        if SHUTDOWN_EVENT.is_set(): break

        for terminal_id, term_info in list(managed_terminals.items()):
            pid = term_info.get("process_pid")
            tmux_session = term_info.get("tmux_session_name")
            if pid:
                try:
                    os.kill(pid, 0)
                    success_check, _, _ = await run_tmux_command_for_session(
                        tmux_session, ["has-session", "-t", tmux_session],
                        timeout=2.0)
                    if not success_check:
                        logger.warning(
                            f"终端模拟器 (PID {pid}) {terminal_id} 正在运行，但其tmux会话 '{tmux_session}' 已消失。报告关闭。"
                        )
                        await send_event_to_ts(
                            "terminal_closed_unexpectedly", {
                                "terminal_id": terminal_id,
                                "reason": f"tmux会话 {tmux_session} 未找到。"
                            })
                        if terminal_id in managed_terminals:
                            del managed_terminals[terminal_id]
                except ProcessLookupError:
                    logger.warning(
                        f"终端模拟器 (PID {pid}) {terminal_id} (tmux: {tmux_session}) 似乎已从外部关闭。"
                    )
                    await send_event_to_ts("terminal_closed_unexpectedly", {
                        "terminal_id": terminal_id,
                        "reason": f"进程PID {pid} 未找到。"
                    })
                    if terminal_id in managed_terminals:
                        del managed_terminals[terminal_id]
                except Exception as e:
                    logger.error(f"检查终端PID {pid} ({terminal_id}) 状态时出错: {e}")
    logger.info("终端进程监视器已停止。")


async def handle_ts_command(data: Dict[str, Any],
                            writer: asyncio.StreamWriter):
    global managed_terminals, tc_writer_global, user_config
    if tc_writer_global is None: tc_writer_global = writer

    action_type = data.get("type")
    payload = data.get("payload", {})
    request_id = data.get("request_id")
    response_payload: Dict[str, Any] = {"success": False, "message": "未知操作"}

    logger.info(
        f"TC收到操作: {action_type}, payload: {json.dumps(payload)}, request_id: {request_id}"
    )

    if action_type == "create_terminal":
        terminal_id = payload.get("terminal_id")
        tmux_session_name = payload.get("tmux_session_name")
        purpose = payload.get("purpose")
        shell_cmd_to_run = payload.get("shell_command") or user_config.get(
            "default_shell_command") or DEFAULT_SHELL_COMMAND

        if not terminal_id or not tmux_session_name:
            response_payload = {
                "success": False,
                "message": "创建终端缺少terminal_id或tmux_session_name。"
            }
        elif terminal_id in managed_terminals:
            response_payload = {
                "success": False,
                "message": f"终端ID {terminal_id} 已存在。"
            }
        else:
            term_exec_template: Optional[List[str]] = None
            chosen_term_name: Optional[str] = "custom_direct"

            direct_cmd_tpl = user_config.get("terminal_command")
            if direct_cmd_tpl and isinstance(direct_cmd_tpl, list) and all(
                    isinstance(item, str) for item in direct_cmd_tpl):
                if shutil.which(direct_cmd_tpl[0]):
                    term_exec_template = direct_cmd_tpl
                    logger.info(f"使用用户配置的 terminal_command: {direct_cmd_tpl}")
                else:
                    logger.warning(
                        f"用户配置的 terminal_command 中的命令 '{direct_cmd_tpl[0]}' 未找到。"
                    )

            if not term_exec_template:
                chosen_term_name = None
                preferred_term_name = user_config.get("preferred_terminal")
                effective_templates = get_effective_terminal_templates()

                if preferred_term_name and preferred_term_name in effective_templates:
                    if shutil.which(
                            effective_templates[preferred_term_name][0]):
                        term_exec_template = effective_templates[
                            preferred_term_name]
                        chosen_term_name = preferred_term_name
                        logger.info(
                            f"使用首选终端模拟器 (来自 'preferred_terminal' 或用户terminal_command): {preferred_term_name}"
                        )

                if not term_exec_template:
                    sorted_template_names = list(effective_templates.keys())
                    for name in sorted_template_names:
                        cmd_template = effective_templates[name]
                        if shutil.which(cmd_template[0]):
                            term_exec_template = cmd_template
                            chosen_term_name = name
                            logger.info(
                                f"为新终端 {terminal_id} 使用终端模拟器 (从有效terminal_command列表): {name}"
                            )
                            break

            if not term_exec_template or not chosen_term_name:
                checked_sources = ["terminal_command"
                                   ] if direct_cmd_tpl else []
                checked_sources.extend(
                    get_effective_terminal_templates().keys())
                response_payload = {
                    "success":
                    False,
                    "message":
                    "未找到合适的终端模拟器。已检查来源: " +
                    ", ".join(list(set(checked_sources)))
                }
            else:
                await run_tmux_command_for_session(tmux_session_name,
                                                   ["start-server"],
                                                   timeout=5.0)
                tmux_full_cmd = f"tmux new-session -A -s {tmux_session_name} {shell_cmd_to_run}"
                term_title = f"MCP-{terminal_id[:8]}" + (f" ({purpose})"
                                                         if purpose else "")

                final_term_cmd_list_base = [
                    part.replace("{title}", term_title)
                    for part in term_exec_template
                ]
                final_term_cmd_list = list(final_term_cmd_list_base)

                last_arg_is_exec_flag = final_term_cmd_list[-1] in [
                    "--", "-e", "--command"
                ]

                if last_arg_is_exec_flag:
                    final_term_cmd_list.append(tmux_full_cmd)
                else:
                    logger.warning(
                        f"终端terminal_command '{chosen_term_name}': {term_exec_template} "
                        f"不以标准执行标志结尾。将尝试拆分tmux命令作为参数。")
                    final_term_cmd_list.extend(tmux_full_cmd.split())

                logger.info(
                    f"TC: 正在为终端ID '{terminal_id}' 启动新终端，命令: {' '.join(final_term_cmd_list)}"
                )

                term_proc: Optional[subprocess.Popen] = None
                process_env = get_process_environment()
                try:
                    term_proc = subprocess.Popen(
                        final_term_cmd_list,
                        preexec_fn=os.setsid
                        if sys.platform != "win32" else None,
                        env=process_env)
                    await asyncio.sleep(2.5)
                    success_check, _, stderr_check = await run_tmux_command_for_session(
                        tmux_session_name,
                        ["has-session", "-t", tmux_session_name],
                        timeout=5.0)
                    if success_check:
                        managed_terminals[terminal_id] = {
                            "tmux_session_name": tmux_session_name,
                            "purpose": purpose,
                            "process_pid":
                            term_proc.pid if term_proc else None,
                            "status": "active",
                            "visible_window_info": {
                                "emulator": chosen_term_name,
                                "pid": term_proc.pid if term_proc else None,
                                "title": term_title
                            }
                        }
                        response_payload = {
                            "success":
                            True,
                            "terminal_id":
                            terminal_id,
                            "message":
                            f"终端 {terminal_id} (tmux: {tmux_session_name}) 已用 {chosen_term_name} 创建。",
                            "visible_window_info":
                            managed_terminals[terminal_id]
                            ["visible_window_info"]
                        }
                        logger.info(
                            f"终端 {terminal_id} (tmux: {tmux_session_name}) 已创建。模拟器PID: {term_proc.pid if term_proc else 'N/A'}"
                        )
                    else:
                        response_payload = {
                            "success":
                            False,
                            "message":
                            f"验证tmux会话 '{tmux_session_name}' 创建失败: {stderr_check}"
                        }
                        if term_proc and term_proc.poll() is None:
                            logger.warning(
                                f"因tmux会话启动失败，杀死终端模拟器 (PID {term_proc.pid})。")
                            term_proc.kill()
                except Exception as e_create:
                    logger.error(f"创建终端 {terminal_id} 时出错: {e_create}",
                                 exc_info=True)
                    response_payload = {
                        "success": False,
                        "message": f"创建终端时发生异常: {str(e_create)}"
                    }
                    if term_proc and term_proc.poll() is None: term_proc.kill()

    elif action_type == "close_terminal":
        terminal_id = payload.get("terminal_id")
        tmux_session_name = payload.get("tmux_session_name")
        logger.info(
            f"TC: close_terminal 操作，terminal_id: {terminal_id}, tmux_session: {tmux_session_name}"
        )

        if not terminal_id or terminal_id not in managed_terminals:
            if tmux_session_name:
                success_check_tmux, _, _ = await run_tmux_command_for_session(
                    tmux_session_name,
                    ["has-session", "-t", tmux_session_name],
                    timeout=2.0)
                if success_check_tmux:
                    logger.warning(
                        f"终端ID {terminal_id} 不在管理列表中，但tmux会话 {tmux_session_name} 存在。尝试杀死。"
                    )
                else:
                    response_payload = {
                        "success":
                        False,
                        "message":
                        f"终端ID {terminal_id} 未找到且tmux会话 {tmux_session_name} 不存在。"
                    }
                    response_wrapper = {
                        "request_id": request_id,
                        "type": f"{action_type}_response",
                        "payload": response_payload
                    }
                    writer.write(
                        (json.dumps(response_wrapper) + "\n").encode('utf-8'))
                    await writer.drain()
                    return
            else:
                response_payload = {
                    "success": False,
                    "message": f"终端ID {terminal_id} 未找到且未提供tmux会话名称。"
                }
                response_wrapper = {
                    "request_id": request_id,
                    "type": f"{action_type}_response",
                    "payload": response_payload
                }
                writer.write(
                    (json.dumps(response_wrapper) + "\n").encode('utf-8'))
                await writer.drain()
                return
        elif managed_terminals[terminal_id][
                "tmux_session_name"] != tmux_session_name:
            response_payload = {
                "success":
                False,
                "message":
                f"终端ID {terminal_id} 的tmux会话名称不匹配。预期 {managed_terminals[terminal_id]['tmux_session_name']}, 得到 {tmux_session_name}。"
            }
        else:
            pass

        logger.info(
            f"尝试杀死tmux会话: {tmux_session_name}，对应terminal_id: {terminal_id}")
        success_kill, _, stderr_kill = await run_tmux_command_for_session(
            tmux_session_name, ["kill-session", "-t", tmux_session_name],
            timeout=5.0)
        term_info = managed_terminals.pop(terminal_id, None)
        term_proc_pid = term_info.get("process_pid") if term_info else None

        if success_kill:
            response_payload = {
                "success": True,
                "message": f"终端 {terminal_id} 的tmux会话 {tmux_session_name} 已杀死。"
            }
            logger.info(f"终端 {terminal_id} 的tmux会话 {tmux_session_name} 已杀死。")
            if term_proc_pid:
                logger.info(
                    f"与被杀死的tmux会话关联的终端模拟器 (PID: {term_proc_pid}) {terminal_id} 可能需要手动关闭，或者如果tmux是其唯一进程则会退出。"
                )
        else:
            await asyncio.sleep(0.1)
            still_exists, _, _ = await run_tmux_command_for_session(
                tmux_session_name, ["has-session", "-t", tmux_session_name],
                timeout=2.0)
            if not still_exists:
                response_payload = {
                    "success":
                    True,
                    "message":
                    f"终端 {terminal_id} 的tmux会话 {tmux_session_name} 已消失或已成功杀死。"
                }
                logger.info(
                    f"在为 {terminal_id} 尝试杀死后，确认tmux会话 {tmux_session_name} 已消失。"
                )
            else:
                response_payload = {
                    "success": False,
                    "message":
                    f"杀死tmux会话 {tmux_session_name} 失败: {stderr_kill}"
                }
                if term_info: managed_terminals[terminal_id] = term_info

    elif action_type == "send_command_and_snapshot":
        terminal_id = payload.get("terminal_id")
        tmux_session_name = payload.get("tmux_session_name")
        command_to_send = payload.get("command")
        wait_seconds = payload.get("wait_seconds_after_command", 1.0)
        strip_ansi_snapshot = payload.get("strip_ansi_for_snapshot", True)

        logger.info(
            f"TC: send_command_and_snapshot 操作，terminal_id: {terminal_id}, tmux_session: {tmux_session_name}, command: '{command_to_send}'"
        )

        if not terminal_id or terminal_id not in managed_terminals:
            response_payload = {
                "success": False,
                "message": f"终端ID {terminal_id} 未找到。"
            }
        elif managed_terminals[terminal_id][
                "tmux_session_name"] != tmux_session_name:
            response_payload = {
                "success": False,
                "message": f"终端ID {terminal_id} 的tmux会话名称不匹配。"
            }
        elif not command_to_send:
            response_payload = {"success": False, "message": "未提供命令。"}
        else:
            success_send, _, stderr_send = await run_tmux_command_for_session(
                tmux_session_name, [
                    "send-keys", "-t", f"{tmux_session_name}:0.0",
                    command_to_send, "Enter"
                ],
                timeout=5.0)
            if success_send:
                logger.info(
                    f"命令 '{command_to_send}' 已发送到 {tmux_session_name}。等待 {wait_seconds}秒以获取输出..."
                )
                await asyncio.sleep(wait_seconds)
                snapshot_content = await get_tmux_pane_content_for_session(
                    tmux_session_name, strip_ansi=strip_ansi_snapshot)
                if snapshot_content is not None:
                    response_payload = {
                        "success": True,
                        "message": "命令已发送且快照已捕获。",
                        "snapshot_content": snapshot_content,
                        "ansi_stripped_by_tc": strip_ansi_snapshot
                    }
                else:
                    response_payload = {
                        "success": True,
                        "message": "命令已发送，但捕获快照失败。",
                        "snapshot_content": None,
                        "ansi_stripped_by_tc": strip_ansi_snapshot
                    }
            else:
                response_payload = {
                    "success": False,
                    "message":
                    f"发送命令到tmux会话 {tmux_session_name} 失败: {stderr_send}"
                }

    elif action_type == "send_keystrokes":
        terminal_id = payload.get("terminal_id")
        tmux_session_name = payload.get("tmux_session_name")
        keys_to_send: List[str] = payload.get("keys", [])

        logger.info(
            f"TC: send_keystrokes 操作，terminal_id: {terminal_id}, tmux_session: {tmux_session_name}, keys: {keys_to_send}"
        )

        if not terminal_id or terminal_id not in managed_terminals:
            response_payload = {
                "success": False,
                "message": f"终端ID {terminal_id} 未找到。"
            }
        elif managed_terminals[terminal_id][
                "tmux_session_name"] != tmux_session_name:
            response_payload = {
                "success": False,
                "message": f"终端ID {terminal_id} 的tmux会话名称不匹配。"
            }
        else:
            tmux_keys_args = []
            for key_name in keys_to_send:
                key_upper = key_name.upper()
                if key_upper == "CTRL_C": tmux_keys_args.append("C-c")
                elif key_upper == "CTRL_L": tmux_keys_args.append("C-l")
                elif key_upper == "CTRL_D": tmux_keys_args.append("C-d")
                elif key_upper == "ENTER": tmux_keys_args.append("Enter")
                elif key_upper == "TAB": tmux_keys_args.append("Tab")
                elif key_upper == "UP_ARROW": tmux_keys_args.append("Up")
                elif key_upper == "DOWN_ARROW": tmux_keys_args.append("Down")
                elif key_upper == "LEFT_ARROW": tmux_keys_args.append("Left")
                elif key_upper == "RIGHT_ARROW": tmux_keys_args.append("Right")
                elif key_upper == "BACKSPACE": tmux_keys_args.append("BSpace")
                elif key_upper == "DELETE": tmux_keys_args.append("DC")
                elif key_upper == "ESCAPE": tmux_keys_args.append("Escape")
                elif key_upper == "SPACE": tmux_keys_args.append("Space")
                else: tmux_keys_args.append(key_name)

            logger.info(f"TC: Mapped keys to tmux arguments: {tmux_keys_args}")

            if not tmux_keys_args:
                response_payload = {
                    "success": False,
                    "message": "未提供或映射要发送的按键。"
                }
            else:
                full_send_keys_cmd = [
                    "send-keys", "-t", f"{tmux_session_name}:0.0"
                ] + tmux_keys_args
                success_send, _, stderr_send = await run_tmux_command_for_session(
                    tmux_session_name, full_send_keys_cmd, timeout=5.0)
                if success_send:
                    response_payload = {"success": True, "message": "按键已发送。"}
                    await asyncio.sleep(0.15 * len(tmux_keys_args))
                else:
                    response_payload = {
                        "success": False,
                        "message": f"发送按键失败: {stderr_send}"
                    }

    elif action_type == "get_snapshot":
        terminal_id = payload.get("terminal_id")
        tmux_session_name = payload.get("tmux_session_name")
        strip_ansi_flag = payload.get("strip_ansi", False)
        logger.info(
            f"TC: get_snapshot 操作，terminal_id: {terminal_id}, tmux_session: {tmux_session_name}, strip_ansi: {strip_ansi_flag}"
        )

        if not terminal_id or terminal_id not in managed_terminals:
            response_payload = {
                "success": False,
                "message": f"终端ID {terminal_id} 未找到。"
            }
        elif managed_terminals[terminal_id][
                "tmux_session_name"] != tmux_session_name:
            response_payload = {
                "success": False,
                "message": f"终端ID {terminal_id} 的tmux会话名称不匹配。"
            }
        else:
            content = await get_tmux_pane_content_for_session(
                tmux_session_name, strip_ansi=strip_ansi_flag)
            if content is not None:
                response_payload = {
                    "success": True,
                    "snapshot_content": content,
                    "ansi_stripped_by_tc": strip_ansi_flag,
                    "terminal_id": terminal_id
                }
            else:
                response_payload = {
                    "success": False,
                    "message": f"获取终端 {terminal_id} 的快照失败。"
                }

    elif action_type == "get_context":
        terminal_id = payload.get("terminal_id")
        tmux_session_name = payload.get("tmux_session_name")
        logger.info(
            f"TC: get_context 操作，terminal_id: {terminal_id}, tmux_session: {tmux_session_name}"
        )

        if not terminal_id or terminal_id not in managed_terminals:
            response_payload = {
                "success": False,
                "message": f"终端ID {terminal_id} 未找到。"
            }
        elif managed_terminals[terminal_id][
                "tmux_session_name"] != tmux_session_name:
            response_payload = {
                "success": False,
                "message": f"终端ID {terminal_id} 的tmux会话名称不匹配。"
            }
        else:
            cwd = await get_cwd_for_session(tmux_session_name)
            term_info = managed_terminals[terminal_id]
            response_payload = {
                "success": True,
                "terminal_id": terminal_id,
                "current_working_directory": cwd,
                "current_prompt": None,
                "is_awaiting_input": False,
                "purpose": term_info.get("purpose"),
                "status": term_info.get("status"),
                "visible_window_info": term_info.get("visible_window_info"),
                "message": "上下文已检索。" + (" CWD未确定。" if cwd is None else ""),
            }

    elif action_type == "shutdown_tc":
        logger.info("收到来自TS的关闭命令。设置SHUTDOWN_EVENT。")
        SHUTDOWN_EVENT.set()
        response_payload = {"success": True, "message": "TC关闭已启动。"}
    elif action_type == "ping_tc":
        response_payload = {
            "success": True,
            "message": "pong",
            "tc_status": "ready_multi"
        }
    else:
        logger.warning(f"收到未知操作类型: {action_type}")

    response_wrapper = {
        "request_id": request_id,
        "type": f"{action_type}_response",
        "payload": response_payload
    }
    try:
        if writer and not writer.is_closing():
            writer.write((json.dumps(response_wrapper) + "\n").encode('utf-8'))
            await writer.drain()
            logger.info(
                f"TC已发送响应，ID {request_id}: {response_payload.get('message', 'OK')}"
            )
    except Exception as e:
        logger.error(f"发送响应到TS时出错，ID {request_id}, 操作 {action_type}: {e}",
                     exc_info=True)


async def shutdown_tc_gracefully(
        loop_ref: Optional[asyncio.AbstractEventLoop] = None):
    global managed_terminals
    if hasattr(shutdown_tc_gracefully,
               'has_run') and shutdown_tc_gracefully.has_run:
        logger.info("优雅关闭已运行或正在进行中。")
        return
    setattr(shutdown_tc_gracefully, 'has_run', True)

    active_loop = loop_ref or asyncio.get_event_loop()
    logger.info("TC启动优雅关闭序列。")
    if not SHUTDOWN_EVENT.is_set(): SHUTDOWN_EVENT.set()

    kill_tasks = []
    for terminal_id, term_info in list(managed_terminals.items()):
        tmux_session_name = term_info["tmux_session_name"]
        logger.info(f"计划在关闭期间杀死tmux会话 {tmux_session_name} (终端 {terminal_id})。")
        kill_tasks.append(
            run_tmux_command_for_session(
                tmux_session_name, ["kill-session", "-t", tmux_session_name],
                timeout=5.0))

    if kill_tasks:
        results = await asyncio.gather(*kill_tasks, return_exceptions=True)
        for res_idx, result_item in enumerate(results):
            if isinstance(result_item, Exception) or \
               (isinstance(result_item, tuple) and len(result_item) > 0 and not result_item[0]):
                logger.warning(f"关闭期间杀死一个tmux会话失败: {result_item}")
            else:
                logger.info(f"关闭期间成功杀死一个tmux会话。")

    managed_terminals.clear()
    logger.info("TC的优雅关闭序列完成 (tmux会话已处理)。")


async def main_tc_client(loop: asyncio.AbstractEventLoop):
    global tc_writer_global, user_config

    user_config = load_user_config()
    logger.info(
        f"TC: 多终端客户端已启动。使用的配置文件路径: '{DEFAULT_CONFIG_PATH}'. 用户配置内容: {user_config}"
    )

    try:
        tc_stdin_reader = asyncio.StreamReader(loop=loop)
        stdin_reader_protocol = asyncio.StreamReaderProtocol(tc_stdin_reader,
                                                             loop=loop)
        await loop.connect_read_pipe(lambda: stdin_reader_protocol,
                                     sys.stdin.buffer)
        tc_responses_reader = tc_stdin_reader

        stdout_writer_transport, stdout_writer_protocol = await loop.connect_write_pipe(
            lambda: asyncio.streams.FlowControlMixin(loop=loop),
            sys.stdout.buffer)
        tc_writer_global = asyncio.StreamWriter(stdout_writer_transport,
                                                stdout_writer_protocol, None,
                                                loop)
    except Exception as e:
        logger.critical(f"TC: 连接stdio管道失败: {e}", exc_info=True)
        SHUTDOWN_EVENT.set()
        if tc_writer_global and not tc_writer_global.is_closing():
            try:
                tc_writer_global.close()
                await tc_writer_global.wait_closed()
            except Exception as e_close:
                logger.error(f"在管道设置失败时关闭tc_writer_global出错: {e_close}")
        return

    logger.info("TC: 已通过stdio连接到TS。等待命令...")
    monitor_task = loop.create_task(monitor_terminal_processes())

    try:
        while not SHUTDOWN_EVENT.is_set():
            try:
                line_bytes = await asyncio.wait_for(
                    tc_responses_reader.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if not line_bytes:
                logger.warning("TS进程stdin已关闭。TC启动关闭。")
                SHUTDOWN_EVENT.set()
                break

            line = line_bytes.decode('utf-8').strip()
            if not line: continue

            command_data = None
            try:
                command_data = json.loads(line)
                await handle_ts_command(command_data, tc_writer_global)
            except json.JSONDecodeError:
                logger.error(f"来自TS的JSON解码错误: '{line}'")
            except Exception as e_handle:
                logger.error(f"处理来自TS的命令时出错: {e_handle}", exc_info=True)
                if command_data and command_data.get(
                        "request_id"
                ) and tc_writer_global and not tc_writer_global.is_closing():
                    err_resp_payload = {
                        "success": False,
                        "message": f"TC处理命令时发生内部错误: {str(e_handle)}"
                    }
                    err_resp_wrapper = {
                        "request_id": command_data.get("request_id"),
                        "type": f"{command_data.get('type','error')}_response",
                        "payload": err_resp_payload
                    }
                    try:
                        tc_writer_global.write((json.dumps(err_resp_wrapper) +
                                                "\n").encode('utf-8'))
                        await tc_writer_global.drain()
                    except Exception as e_send_err:
                        logger.error(f"发送错误响应到TS失败: {e_send_err}")
    except asyncio.CancelledError:
        logger.info("TC主循环被取消。")
    except Exception as e_main:
        logger.critical(f"TC主循环未处理异常: {e_main}", exc_info=True)
    finally:
        logger.info("TC主循环完成。启动最终关闭序列...")
        if not SHUTDOWN_EVENT.is_set(): SHUTDOWN_EVENT.set()

        if monitor_task and not monitor_task.done():
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                logger.info("终端监视器任务已取消。")
            except Exception as e_mon_cancel:
                logger.error(f"等待监视器任务取消时出错: {e_mon_cancel}")

        await shutdown_tc_gracefully(loop_ref=loop)

        if tc_writer_global and not tc_writer_global.is_closing():
            try:
                tc_writer_global.close()
                await tc_writer_global.wait_closed()
                logger.info("TC到TS的写入器已关闭。")
            except Exception as e_close_writer:
                logger.error(f"关闭TC到TS的写入器时出错: {e_close_writer}")

        logger.info("TC main_tc_client finally块完成。")


_module_level_debug_flag = True

if __name__ == "__main__":
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)

    logger.info(
        f"TC: Initializing in __main__. DEFAULT_CONFIG_PATH is '{DEFAULT_CONFIG_PATH}' (type: {type(DEFAULT_CONFIG_PATH)})"
    )

    try:
        if isinstance(DEFAULT_CONFIG_PATH, Path):
            logger.info(
                f"TC_DIAG: Config file parent directory to be checked by load_user_config: '{DEFAULT_CONFIG_PATH.parent}'"
            )
        else:
            logger.error(
                f"TC_DIAG: CRITICAL in __main__ - DEFAULT_CONFIG_PATH is not a Path object: {DEFAULT_CONFIG_PATH} (type: {type(DEFAULT_CONFIG_PATH)})"
            )
    except Exception as e_main_path_check:
        logger.error(
            f"TC_DIAG: Error during final path check in __main__: {e_main_path_check}",
            exc_info=True)

    def os_signal_handler(sig: int, frame: Optional[Any]):
        logger.info(f"操作系统信号 {sig} 被TC接收。通过SHUTDOWN_EVENT计划优雅关闭。")
        SHUTDOWN_EVENT.set()

    for sig_name in ('SIGINT', 'SIGTERM'):
        sig_val = getattr(signal, sig_name, None)
        if sig_val:
            try:
                event_loop.add_signal_handler(
                    sig_val, lambda s=sig_val: os_signal_handler(s, None))
                logger.info(f"在TC中成功为 {sig_name} 设置asyncio信号处理器。")
            except (NotImplementedError, RuntimeError) as e:
                logger.warning(
                    f"无法为 {sig_name} 设置asyncio信号处理器 ({e})。回退到signal.signal。")
                try:
                    signal.signal(sig_val, os_signal_handler)
                    logger.info(f"在TC中成功为 {sig_name} 设置signal.signal处理器。")
                except Exception as e_sig_fallback:
                    logger.error(
                        f"无法在TC中为 {sig_name} 设置任何信号处理器: {e_sig_fallback}")
            except Exception as e_sig_setup:
                logger.error(f"在TC中为 {sig_name} 设置信号处理器时发生一般错误: {e_sig_setup}")

    main_task_tc: Optional[asyncio.Task] = None
    try:
        logger.info("TC: 启动主客户端任务。")
        main_task_tc = event_loop.create_task(main_tc_client(event_loop))
        event_loop.run_forever()
    except KeyboardInterrupt:
        logger.info("TC在__main__中捕获到KeyboardInterrupt。")
        if not SHUTDOWN_EVENT.is_set(): SHUTDOWN_EVENT.set()
    except SystemExit as e_sysexit:
        logger.info(f"TC捕获到SystemExit: {e_sysexit}")
        if not SHUTDOWN_EVENT.is_set(): SHUTDOWN_EVENT.set()
    finally:
        logger.info("TC __main__ 'finally'块: 清理事件循环。")

        if main_task_tc and not main_task_tc.done():
            logger.info("取消主TC任务...")
            main_task_tc.cancel()
            try:
                event_loop.run_until_complete(main_task_tc)
            except asyncio.CancelledError:
                logger.info("TC主任务成功取消。")
            except Exception as e_mt_cancel:
                logger.error(f"等待TC主任务取消时出错: {e_mt_cancel}", exc_info=True)

        if not SHUTDOWN_EVENT.is_set():
            logger.warning("SHUTDOWN_EVENT未设置，在外部finally中立即设置。")
            SHUTDOWN_EVENT.set()

        if event_loop.is_running():
            logger.info("从TC __main__ finally停止事件循环。")
            event_loop.stop()

        if not event_loop.is_closed():
            try:
                all_remaining_tasks = [
                    t for t in asyncio.all_tasks(loop=event_loop)
                    if not t.done()
                ]
                if all_remaining_tasks:
                    logger.debug(
                        f"在TC中关闭循环前收集 {len(all_remaining_tasks)} 个剩余任务。")
                    event_loop.run_until_complete(
                        asyncio.gather(*all_remaining_tasks,
                                       return_exceptions=True))

                logger.debug("在TC中关闭异步生成器。")
                event_loop.run_until_complete(event_loop.shutdown_asyncgens())
            except RuntimeError as e_loop_cleanup:
                logger.warning(
                    f"TC中循环清理期间的RuntimeError (任务/异步生成器): {e_loop_cleanup}")
            except Exception as e_final_gather:
                logger.error(f"TC中最终任务收集/生成器关闭期间的异常: {e_final_gather}",
                             exc_info=True)
            finally:
                if not event_loop.is_closed():
                    event_loop.close()
                    logger.info("TC事件循环在__main__中关闭。")

        logger.info("TC应用程序已完全关闭。")
