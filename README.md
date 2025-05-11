# SpectraScope-Term

## 项目简介

**SpectraScope-Term** 是一个基于MCP的高级多终端代理服务器。

它赋予 AI 模型独特的能力：不仅能与 Linux 终端交互，更能"观测"并控制在你桌面上真实可见的多个终端模拟器窗口。

并且 AI 的每一步操作都清晰可见

## 项目特点

与传统，只能执行终端命令的MCP终端工具不同，**SpectraScope-Term** 的核心特性在于**可视化、透明度**，AI不在只是简单的执行命令，而是真实的"操作"终端：

并且AI在终端中执行的每一个命令、发送的每一个按键，都会**实时、准确地反映在桌面上对应的终端窗口中**。可以清晰地“看见”AI正在做什么，不再是黑箱操作。

## 配置文件

**SpectraScope-Term** 通过终端模拟器来实现终端功能，内置了如下模拟器

*	xfce4-terminal
*	gnome-terminal
*	konsole
*	qterminal
*	xterm
*	kitty
*	alacritty

> 测试使用的环境是Kali Linux，使用的是`xfce4-terminal`，可正常使用。

终端客户端 (`client.py`) 通过 `tc_config.json` 进行配置，定义AI如何启动和使用终端。

*   **配置文件位置**：
    *   默认情况下，`tc_config.json` 文件应与 `client.py` 脚本放置在**同一目录**。
    *   你也可以通过在启动 `server.py` 时设置 `MCP_TC_CONFIG_PATH` 环境变量（或使用 `--mcp-tc-config-path` 命令行参数）来指定 `tc_config.json` 的绝对路径。

*   **核心配置项**：
    *   `terminal_command`: 一个列表，用于精确指定启动你偏好的终端模拟器的命令和参数。AI创建终端时将优先使用此模板。
        *   占位符 `{title}` 会被AI动态替换为终端窗口的标题。
        *   命令的最后一部分通常是执行标志 (如 `--command`, `-e`)，其后将自动附加 `tmux` 启动命令。
        *   **示例** (使用xfce4-terminal): `["xfce4-terminal", "--disable-server", "--title", "{title}", "--command"]`
    *   `default_shell_command`: 指定新终端默认启动的shell及其参数。
        *   **示例**: `"zsh -il"` 或 `"bash -il"` (默认)。
        
*   **示例 `tc_config.json`**：

    ```json
	{
		"terminal_command": [
			"xfce4-terminal",
			"--disable-server",
			"--title",
			"{title}",
			"--command"
		],
		"default_shell_command": "zsh -il"
	}
    ```
    
    上述配置将使得 **SpectraScope-Term** 在AI请求创建新终端时，使用 `xfce4-terminal` 终端模拟器，并默认在其中启动 `zsh -il`。

## 系统要求

*   **操作系统**：目前仅支持Linux
*   **核心依赖**：
    *   Python 3.x (测试使用 Python 3.10.17 版本)
    *   `tmux` (必须已安装，并且其路径在系统的 `PATH` 环境变量中)。
    *   至少一个支持的桌面终端模拟器 (例如 `xfce4-terminal`, `gnome-terminal`, `konsole`, `kitty`, `alacritty`, `xterm` 等，具体取决于你的 `tc_config.json` 配置或系统默认设置)。

## 演示视频

https://github.com/user-attachments/assets/1511707a-7c56-4de7-b5a8-c4b10507bc14


