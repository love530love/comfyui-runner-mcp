# comfyui-runner-mcp

A small, dependency-light MCP server for [ComfyUI](https://github.com/comfyanonymous/ComfyUI). It runs inside ComfyUI's Python environment and gives an AI agent headless control over the local instance: start/stop, workflow submission, job polling, output and model discovery.

This is the connector extracted from a production ComfyUI workstation. It exists because IDE-based MCP servers start a fresh shell per command and do not inherit ComfyUI's virtual environment, while browser-driven MCP servers need a running UI. `comfyui-runner-mcp` talks to ComfyUI's HTTP API directly and survives IDE restarts.

## Features

- **Process control**: `comfyui_status`, `comfyui_restart`, `comfyui_stop` with hidden-console startup, optional VS 2022 x64 developer environment, optional UAC elevation, and port-release verification.
- **Headless workflows**: `submit_workflow` with blocking or non-blocking mode, plus `comfyui_job_status` and `comfyui_wait_job` for long video jobs.
- **Complete output parsing**: images (`SaveImage`), videos (`VHS_VideoCombine`, `SaveVideo`) and audio are all reported.
- **Model and node discovery**: `list_models` and `get_object_info` so agents can build valid API-format workflows.
- **No extra runtime dependencies**: standard library HTTP plus the `mcp` package already required by the server.
- **Path containment**: `read_file` cannot escape `COMFYUI_DIR`.

## Requirements

- Python 3.10+
- A ComfyUI checkout with its virtual environment
- `mcp` installed in that environment (`pip install mcp` if missing)
- Windows, Linux or macOS (Windows uses a hidden console and `vcvars64.bat` when present; POSIX uses `start_new_session`)

## Quick start

```bash
# from the ComfyUI virtual environment
pip install mcp

# run the server (stdio transport)
COMFYUI_DIR=/path/to/ComfyUI python server.py
```

### Client configuration

Qoder / WorkBuddy / Cursor style `mcp.json`:

```json
{
  "mcpServers": {
    "comfyui-runner": {
      "command": "C:/ComfyUI/.venv/Scripts/python.exe",
      "args": ["C:/tools/comfyui-runner-mcp/server.py"],
      "env": {
        "COMFYUI_DIR": "C:/ComfyUI"
      }
    }
  }
}
```

`command` must be the Python interpreter of ComfyUI's environment. `COMFYUI_DIR` must point at the directory containing `main.py`.

### Environment variables

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `COMFYUI_DIR` | yes | - | ComfyUI installation directory |
| `COMFYUI_HOST` | no | `127.0.0.1` | ComfyUI host |
| `COMFYUI_PORT` | no | auto `8188-8190` | Preferred port; auto-detection verifies `/system_stats` |
| `COMFYUI_START_ARGS` | no | `--port <port>` | Arguments passed to `main.py` on restart |
| `COMFYUI_PYTHON` | no | `<COMFYUI_DIR>/.venv` python | Interpreter used by `execute_python` |
| `COMFYUI_VSDEV` | no | on | Load the VS 2022 x64 developer environment via `vcvars64.bat` before `main.py` |
| `COMFYUI_ADMIN` | no | off | Start ComfyUI elevated; Windows shows a UAC prompt |

## Tools

| Tool | Purpose |
| --- | --- |
| `execute_python` | Run Python with the ComfyUI interpreter in `COMFYUI_DIR` |
| `execute_shell` | Run a shell command with the venv `Scripts`/`bin` directory on `PATH` |
| `comfyui_status` | Port, version, Python/PyTorch, devices, queue depth |
| `comfyui_restart` | Stop then start ComfyUI in a hidden console, return immediately |
| `comfyui_stop` | Kill the ComfyUI process tree and verify ports are released; an elevated instance is stopped through UAC |
| `submit_workflow` | POST an API-format workflow; optionally wait for completion |
| `comfyui_job_status` | One-shot job state and output files |
| `comfyui_wait_job` | Block until a job completes or times out |
| `list_outputs` | Recent files in `output/` |
| `list_models` | Model categories or files under `models/` |
| `get_object_info` | Node input schema and allowed values |
| `read_file` | Read a text file inside `COMFYUI_DIR` |

### Long video jobs

MCP clients often time out on calls that block for minutes. For video:

```text
submit_workflow(workflow_json=..., wait=false)  -> prompt_id
comfyui_wait_job(prompt_id=..., timeout=600)
```

## Notes

- Restart logs go to `<COMFYUI_DIR>/comfyui_runner.log`.
- `COMFYUI_VSDEV` locates `vcvars64.bat` through `VSINSTALLDIR`, `vswhere.exe`, then the standard VS 2022 install folders. If none is found, ComfyUI starts without the developer environment.
- `COMFYUI_ADMIN` launches `cmd.exe` through `ShellExecuteEx(runas)`, so Windows shows a UAC prompt. UAC must also be approved for `comfyui_stop`/`comfyui_restart` to terminate that instance. The launch PID is recorded in `<COMFYUI_DIR>/user/comfyui_runner.launch.json` because elevated command lines are not readable from a normal process.
- Auto-detection probes `8188`, `8189` and `8190` and only accepts a port that answers `/system_stats`.
- The server never downloads models and makes no outbound internet requests. It only talks to the configured local ComfyUI instance.
- `execute_shell` and `execute_python` are powerful by design. Run the server only for clients you trust.

## 中文说明

`comfyui-runner-mcp` 是一个直接运行在 ComfyUI 虚拟环境里的 stdio MCP 服务器，用于无头模式下的 ComfyUI 运维和生图/生视频：

1. 把 `COMFYUI_DIR` 设为 ComfyUI 根目录（含 `main.py`）。
2. 客户端配置里的 `command` 用该环境的 Python，`args` 指向本仓库的 `server.py`。
3. 生视频建议 `wait=false` 提交后，用 `comfyui_wait_job` 轮询，避免 MCP 客户端超时。

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
```

## License

MIT
