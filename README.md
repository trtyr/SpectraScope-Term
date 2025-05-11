# SpectraScope-Term

English | [简体中文](README_zh.md)

## License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for more details.

## Project Introduction

**SpectraScope-Term** is an advanced multi-terminal proxy server based on MCP.

It empowers AI models with the unique ability to not only interact with Linux terminals but also to "observe" and control multiple terminal emulator windows that are truly visible on your desktop.

Furthermore, every step the AI takes is clearly visible.

## Project Features

Unlike traditional MCP terminal tools that can only execute terminal commands, the core features of **SpectraScope-Term** lie in **visualization and transparency**. The AI doesn't just simply execute commands; it genuinely "operates" the terminal:

Every command executed and every keystroke sent by the AI in the terminal is **reflected in real-time and accurately in the corresponding terminal window on the desktop**. You can clearly "see" what the AI is doing, eliminating black-box operations.

## Configuration File

**SpectraScope-Term** achieves terminal functionality through terminal emulators. It has built-in support for the following emulators:

*   xfce4-terminal
*   gnome-terminal
*   konsole
*   qterminal
*   xterm
*   kitty
*   alacritty

> The testing environment was Kali Linux, using `xfce4-terminal`, which worked correctly.

The terminal client (`client.py`) is configured via `tc_config.json`, which defines how the AI launches and uses terminals.

*   **Configuration File Location**:
    *   By default, the `tc_config.json` file should be placed in the **same directory** as the `client.py` script.
    *   You can also specify the absolute path to `tc_config.json` by setting the `MCP_TC_CONFIG_PATH` environment variable (or using the `--mcp-tc-config-path` command-line argument) when starting `server.py`.

*   **Core Configuration Options**:
    *   `direct_terminal_command_template` (Note: in your original text, you used `terminal_command`, but the Python code uses `direct_terminal_command_template`. I'll use the latter for consistency with the code, or you can adjust the code/config key name): A list used to precisely specify the command and arguments for launching your preferred terminal emulator. The AI will prioritize this template when creating terminals.
        *   The placeholder `{title}` will be dynamically replaced by the AI with the terminal window's title.
        *   The last part of the command is typically an execution flag (e.g., `--command`, `-e`), after which the `tmux` startup command will be automatically appended.
        *   **Example** (using xfce4-terminal): `["xfce4-terminal", "--disable-server", "--title", "{title}", "--command"]`
    *   `default_shell_command`: Specifies the default shell and its arguments to launch in new terminals.
        *   **Example**: `"zsh -il"` or `"bash -il"` (default).

*   **Example `tc_config.json`**:

    ```json
    {
        "direct_terminal_command_template": [
            "xfce4-terminal",
            "--disable-server",
            "--title",
            "{title}",
            "--command"
        ],
        "default_shell_command": "zsh -il"
    }
    ```

    The configuration above will cause **SpectraScope-Term** to use the `xfce4-terminal` emulator when the AI requests the creation of a new terminal, and it will default to launching `zsh -il` within it.

## System Requirements

*   **Operating System**: Currently, only Linux is supported.
*   **Core Dependencies**:
    *   Python 3.x (Tested with Python 3.10.17)
    *   `tmux` (must be installed and its path included in the system's `PATH` environment variable).
    *   At least one supported desktop terminal emulator (e.g., `xfce4-terminal`, `gnome-terminal`, `konsole`, `kitty`, `alacritty`, `xterm`, etc., depending on your `tc_config.json` configuration or system defaults).

## Demo Video

https://github.com/user-attachments/assets/1511707a-7c56-4de7-b5a8-c4b10507bc14
