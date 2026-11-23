#!/usr/bin/env python3
"""ComfyUI Runner MCP Server.

A small stdio MCP server that runs inside ComfyUI's Python environment and
exposes process control plus headless workflow submission over the local
ComfyUI HTTP API.

Environment variables:
    COMFYUI_DIR         ComfyUI installation directory (required)
    COMFYUI_HOST        ComfyUI host (default: 127.0.0.1)
    COMFYUI_PORT        Preferred port (default: auto-detect 8188-8190)
    COMFYUI_START_ARGS  Extra ``main.py`` arguments used on restart
    COMFYUI_PYTHON      Interpreter used by execute_python (default: venv python)

Run:
    python server.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

SERVER_VERSION = "0.1.0"
DEFAULT_PORTS = (8188, 8189, 8190)
MODEL_EXTENSIONS = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf"}
OUTPUT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".gif", ".wav", ".mp3", ".flac"}

server = Server("comfyui-runner")


class Config:
    def __init__(self) -> None:
        raw_root = os.environ.get("COMFYUI_DIR", "").strip()
        if not raw_root:
            raise RuntimeError(
                "COMFYUI_DIR is not set. Point it at the ComfyUI installation directory."
            )
        self.root = Path(raw_root).expanduser().resolve()
        if not (self.root / "main.py").is_file():
            raise RuntimeError(f"COMFYUI_DIR does not look like a ComfyUI checkout: {self.root}")

        self.host = os.environ.get("COMFYUI_HOST", "127.0.0.1").strip() or "127.0.0.1"
        raw_port = os.environ.get("COMFYUI_PORT", "").strip()
        self.port_override = int(raw_port) if raw_port else None
        self.start_args = shlex.split(os.environ.get("COMFYUI_START_ARGS", ""))

        raw_python = os.environ.get("COMFYUI_PYTHON", "").strip()
        if raw_python:
            self.python = Path(raw_python).expanduser()
        elif os.name == "nt":
            self.python = self.root / ".venv" / "Scripts" / "python.exe"
        else:
            self.python = self.root / ".venv" / "bin" / "python"
        if not self.python.is_file():
            self.python = Path(sys.executable)

        self.output_dir = self.root / "output"
        self.log_file = self.root / "comfyui_runner.log"


_config: Config | None = None


def config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _http_json(port: int, method: str, path: str, payload: dict | None = None, timeout: float = 10) -> dict:
    cfg = config()
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urlrequest.Request(
        f"http://{cfg.host}:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"ComfyUI HTTP {exc.code} for {path}: {detail}") from exc
    return json.loads(raw) if raw else {}


def resolve_port() -> int | None:
    """Return the first port that answers ComfyUI's /system_stats endpoint."""
    cfg = config()
    candidates = [cfg.port_override] if cfg.port_override else list(DEFAULT_PORTS)
    for port in candidates:
        if not port or not _port_open(cfg.host, port):
            continue
        try:
            _http_json(port, "GET", "/system_stats", timeout=1.5)
            return port
        except Exception:
            continue
    return None


def require_port() -> int:
    port = resolve_port()
    if port is None:
        raise RuntimeError(
            "ComfyUI is not running (no responsive port in 8188-8190). Use comfyui_restart first."
        )
    return port


def normalize_workflow(raw: str | dict) -> dict:
    workflow = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(workflow, dict) or not workflow:
        raise ValueError("workflow_json must be a non-empty API-format object")
    return workflow


def safe_child(root: Path, relative: str) -> Path:
    full = (root / relative).expanduser().resolve()
    root_resolved = root.resolve()
    if full != root_resolved and root_resolved not in full.parents:
        raise ValueError(f"path escapes allowed root: {relative}")
    return full


def collect_output_files(history_entry: dict) -> list[dict]:
    """Collect every file produced by a completed prompt.

    Handles SaveImage (``images``), VHS_VideoCombine (``gifs``), SaveVideo and
    audio nodes alike, so video jobs are no longer reported as empty.
    """
    files: list[dict] = []
    for node_id, node_out in (history_entry.get("outputs") or {}).items():
        if not isinstance(node_out, dict):
            continue
        for kind in ("images", "gifs", "videos", "audio"):
            for item in node_out.get(kind) or []:
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                files.append(
                    {
                        "node": node_id,
                        "kind": kind,
                        "filename": item["filename"],
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output"),
                        "format": item.get("format"),
                    }
                )
    return files


def summarize_history(history_entry: dict, prompt_id: str) -> dict:
    status = history_entry.get("status") or {}
    completed = bool(status.get("completed"))
    status_str = str(status.get("status_str", "success"))
    if not completed:
        state = "running"
    elif status_str == "error":
        state = "error"
    else:
        state = "completed"
    return {
        "status": state,
        "prompt_id": prompt_id,
        "completed": completed,
        "status_str": status_str,
        "files": collect_output_files(history_entry),
        "messages": status.get("messages", []),
    }


def _queue_prompt_ids(port: int) -> dict[str, str]:
    queue = _http_json(port, "GET", "/queue", timeout=5)
    ids: dict[str, str] = {}
    for key, state in (("queue_running", "running"), ("queue_pending", "pending")):
        for item in queue.get(key) or []:
            if isinstance(item, (list, tuple)) and len(item) > 1:
                ids[str(item[1])] = state
    return ids


def job_state(prompt_id: str) -> dict:
    port = require_port()
    history = _http_json(port, "GET", f"/history/{prompt_id}", timeout=10)
    entry = history.get(prompt_id)
    if entry:
        return summarize_history(entry, prompt_id)
    state = _queue_prompt_ids(port).get(prompt_id)
    return {
        "status": state or "unknown",
        "prompt_id": prompt_id,
        "files": [],
        "messages": [],
    }


async def _wait_for_prompt(prompt_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + max(1.0, timeout)
    last: dict = {"status": "queued", "prompt_id": prompt_id, "files": [], "messages": []}
    while time.monotonic() < deadline:
        last = job_state(prompt_id)
        if last["status"] in ("completed", "error"):
            return last
        await asyncio.sleep(2)
    last["status"] = "timeout"
    return last


TOOLS = [
    Tool(
        name="execute_python",
        description="Run Python code with the ComfyUI interpreter inside COMFYUI_DIR.",
        inputSchema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source to execute"},
                "timeout": {"type": "integer", "description": "Timeout in milliseconds", "default": 30000},
            },
            "required": ["code"],
        },
    ),
    Tool(
        name="execute_shell",
        description="Run a shell command in COMFYUI_DIR. The venv Scripts/bin directory is prepended to PATH.",
        inputSchema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in milliseconds", "default": 30000},
            },
            "required": ["command"],
        },
    ),
    Tool(
        name="comfyui_status",
        description="Report ComfyUI status: port, version, Python, PyTorch, devices, and queue depth.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="comfyui_restart",
        description=(
            "Stop ComfyUI and start it again as a detached background process. Returns immediately; "
            "poll comfyui_status until the API answers."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "extra_args": {
                    "type": "string",
                    "description": "Override COMFYUI_START_ARGS for this restart, e.g. '--port 8189 --use-pytorch-cross-attention'",
                }
            },
        },
    ),
    Tool(
        name="comfyui_stop",
        description="Stop the ComfyUI process tree and verify that its ports are released.",
        inputSchema={
            "type": "object",
            "properties": {
                "kill_mcp": {
                    "type": "boolean",
                    "description": "Also stop processes listening on port 9100 (legacy comfyui-mcp server)",
                    "default": False,
                }
            },
        },
    ),
    Tool(
        name="submit_workflow",
        description=(
            "Submit an API-format workflow to /prompt. By default waits for completion and returns all "
            "output files (images, videos, audio). Set wait=false for long video jobs and poll "
            "comfyui_job_status / comfyui_wait_job with the returned prompt_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workflow_json": {
                    "type": "string",
                    "description": "Workflow object or JSON string in ComfyUI API format",
                },
                "wait": {"type": "boolean", "description": "Wait for completion", "default": True},
                "timeout": {"type": "integer", "description": "Wait timeout in seconds", "default": 120},
            },
            "required": ["workflow_json"],
        },
    ),
    Tool(
        name="comfyui_job_status",
        description="Report one job's state (running/pending/completed/error/unknown) and its output files.",
        inputSchema={
            "type": "object",
            "properties": {"prompt_id": {"type": "string", "description": "Prompt id returned by submit_workflow"}},
            "required": ["prompt_id"],
        },
    ),
    Tool(
        name="comfyui_wait_job",
        description="Block until a job completes or the timeout expires.",
        inputSchema={
            "type": "object",
            "properties": {
                "prompt_id": {"type": "string", "description": "Prompt id returned by submit_workflow"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 300},
            },
            "required": ["prompt_id"],
        },
    ),
    Tool(
        name="list_outputs",
        description="List recent files in ComfyUI's output directory.",
        inputSchema={
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Maximum files to return", "default": 10},
                "prefix": {"type": "string", "description": "Filename substring filter"},
            },
        },
    ),
    Tool(
        name="list_models",
        description=(
            "List model files. Without 'folder', return top-level model categories with file counts. "
            "With 'folder' (e.g. 'diffusion_models' or 'checkpoints/SDXL'), list matching files."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Model subfolder relative to models/"},
                "limit": {"type": "integer", "description": "Maximum files when listing a folder", "default": 200},
            },
        },
    ),
    Tool(
        name="get_object_info",
        description="Fetch /object_info for one node class to discover required inputs and allowed values.",
        inputSchema={
            "type": "object",
            "properties": {"class_type": {"type": "string", "description": "ComfyUI node class name"}},
            "required": ["class_type"],
        },
    ),
    Tool(
        name="read_file",
        description="Read a UTF-8 text file inside COMFYUI_DIR. Paths escaping the directory are rejected.",
        inputSchema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to COMFYUI_DIR"}},
            "required": ["path"],
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    handler = _HANDLERS.get(name)
    if handler is None:
        return [_json_text({"error": f"unknown tool: {name}"})]
    try:
        result = await handler(arguments or {})
        if isinstance(result, list):
            return result
        return [_json_text(result)]
    except Exception as exc:
        return [_json_text({"error": f"{type(exc).__name__}: {exc}"})]


def _json_text(payload: dict) -> TextContent:
    return TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))


async def _execute_python(args: dict) -> dict:
    cfg = config()
    timeout = float(args.get("timeout", 30000)) / 1000
    proc = await asyncio.create_subprocess_exec(
        str(cfg.python),
        "-c",
        args["code"],
        cwd=str(cfg.root),
        stdin=subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"status": "timeout", "output": f"timed out after {timeout:.1f}s"}
    return {
        "exit_code": proc.returncode,
        "output": _decode(stdout).strip(),
        "stderr": _decode(stderr).strip(),
    }


async def _execute_shell(args: dict) -> dict:
    cfg = config()
    timeout = float(args.get("timeout", 30000)) / 1000
    env = os.environ.copy()
    env["PATH"] = str(cfg.python.parent) + os.pathsep + env.get("PATH", "")
    env["PYTHONUTF8"] = "1"
    proc = await asyncio.create_subprocess_shell(
        args["command"],
        cwd=str(cfg.root),
        stdin=subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"status": "timeout", "output": f"timed out after {timeout:.1f}s"}
    return {
        "exit_code": proc.returncode,
        "output": _decode(stdout).strip(),
        "stderr": _decode(stderr).strip(),
    }


async def _comfyui_status(_: dict) -> dict:
    cfg = config()
    port = resolve_port()
    if port is None:
        return {"status": "not_running", "hint": "Use comfyui_restart to start ComfyUI."}
    stats = _http_json(port, "GET", "/system_stats", timeout=5)
    system = stats.get("system") or {}
    devices = [
        {
            "name": device.get("name"),
            "type": device.get("type"),
            "vram_total_mb": round((device.get("vram_total") or 0) / 1024 / 1024),
            "vram_free_mb": round((device.get("vram_free") or 0) / 1024 / 1024),
            "torch_vram_free_mb": round((device.get("torch_vram_free") or 0) / 1024 / 1024),
        }
        for device in stats.get("devices") or []
    ]
    queue = _queue_prompt_ids(port)
    return {
        "status": "running",
        "port": port,
        "version": system.get("comfyui_version"),
        "python": system.get("python_version"),
        "pytorch": system.get("pytorch_version"),
        "argv": system.get("argv") or [],
        "devices": devices,
        "queue": {
            "running": sum(1 for state in queue.values() if state == "running"),
            "pending": sum(1 for state in queue.values() if state == "pending"),
        },
        "output_dir": str(cfg.output_dir),
    }


def _find_comfyui_pids(ports: set[int]) -> dict[int, str]:
    cfg = config()
    found: dict[int, str] = {}
    try:
        import psutil
    except ImportError:
        psutil = None

    if psutil is not None:
        try:
            for conn in psutil.net_connections(kind="inet"):
                if (
                    conn.status == psutil.CONN_LISTEN
                    and conn.laddr
                    and conn.laddr.port in ports
                    and conn.pid
                ):
                    found[conn.pid] = f"port {conn.laddr.port}"
        except Exception:
            pass
        for proc in psutil.process_iter(["pid", "cmdline", "cwd"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                if not any(Path(arg).name.lower() == "main.py" for arg in cmdline if arg):
                    continue
                cwd = proc.info.get("cwd") or ""
                joined = " ".join(cmdline).lower()
                cwd_matches = bool(cwd) and Path(cwd).resolve() == cfg.root
                if cwd_matches or str(cfg.root).lower() in joined:
                    found.setdefault(proc.pid, "main.py process")
            except Exception:
                continue
    else:
        raw = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=15).stdout
        for line in _decode(raw).splitlines():
            parts = line.split()
            if len(parts) < 5 or not parts[-1].isdigit() or "LISTEN" not in line.upper():
                continue
            local = parts[1]
            if ":" not in local:
                continue
            try:
                port = int(local.rsplit(":", 1)[1])
            except ValueError:
                continue
            if port in ports:
                found[int(parts[-1])] = f"port {port}"
    return found


def _kill_pid(pid: int) -> str:
    if os.name == "nt":
        proc = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=20,
        )
        output = _decode(proc.stdout + proc.stderr).strip()
        return "killed" if proc.returncode == 0 else output
    os.kill(pid, signal.SIGTERM)
    return "signalled"


def _stop_comfyui(kill_mcp: bool) -> dict:
    cfg = config()
    ports = set(DEFAULT_PORTS)
    if cfg.port_override:
        ports.add(cfg.port_override)
    if kill_mcp:
        ports.add(9100)

    pids = _find_comfyui_pids(ports)
    killed: list[dict] = []
    failed: list[dict] = []
    for pid, source in pids.items():
        result = _kill_pid(pid)
        if result == "killed":
            killed.append({"pid": pid, "source": source})
        else:
            failed.append({"pid": pid, "source": source, "detail": result})

    deadline = time.monotonic() + 5
    still_open = []
    while time.monotonic() < deadline:
        still_open = [port for port in ports if _port_open(cfg.host, port, timeout=0.3)]
        if not still_open:
            break
        time.sleep(0.5)

    return {
        "stopped": not still_open,
        "killed": killed,
        "failed": failed,
        "ports_still_open": still_open,
    }


async def _comfyui_stop(args: dict) -> dict:
    return _stop_comfyui(bool(args.get("kill_mcp", False)))


async def _comfyui_restart(args: dict) -> dict:
    cfg = config()
    stop_result = _stop_comfyui(kill_mcp=False)
    await asyncio.sleep(1)

    raw_extra = str(args.get("extra_args", "")).strip()
    start_args = shlex.split(raw_extra) if raw_extra else list(cfg.start_args)
    if not any(arg == "--port" or arg.startswith("--port=") for arg in start_args):
        start_args += ["--port", str(cfg.port_override or 8189)]

    command = [str(cfg.python), "main.py", *start_args]
    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    with open(cfg.log_file, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            command,
            cwd=str(cfg.root),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
            env={**os.environ, "PYTHONUTF8": "1"},
        )

    return {
        "status": "starting",
        "pid": proc.pid,
        "command": " ".join(command),
        "log_file": str(cfg.log_file),
        "stop": stop_result,
        "hint": "Poll comfyui_status until status is running.",
    }


async def _submit_workflow(args: dict) -> dict:
    workflow = normalize_workflow(args["workflow_json"])
    port = require_port()
    response = _http_json(port, "POST", "/prompt", {"prompt": workflow}, timeout=30)
    prompt_id = response.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return a prompt_id: {response}")
    if not bool(args.get("wait", True)):
        return {
            "status": "queued",
            "prompt_id": prompt_id,
            "number": response.get("number"),
            "hint": "Use comfyui_wait_job with this prompt_id.",
        }
    return await _wait_for_prompt(prompt_id, float(args.get("timeout", 120)))


async def _comfyui_job_status(args: dict) -> dict:
    return job_state(args["prompt_id"])


async def _comfyui_wait_job(args: dict) -> dict:
    return await _wait_for_prompt(args["prompt_id"], float(args.get("timeout", 300)))


async def _list_outputs(args: dict) -> dict:
    cfg = config()
    count = max(1, int(args.get("count", 10)))
    prefix = str(args.get("prefix", ""))
    files = []
    if cfg.output_dir.is_dir():
        for path in cfg.output_dir.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in OUTPUT_EXTENSIONS:
                continue
            if prefix and prefix not in path.name:
                continue
            stat = path.stat()
            files.append(
                {
                    "name": path.name,
                    "path": str(path.relative_to(cfg.output_dir)),
                    "size_mb": round(stat.st_size / 1024 / 1024, 2),
                    "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                    "_mtime": stat.st_mtime,
                }
            )
    files.sort(key=lambda item: item["_mtime"], reverse=True)
    for item in files:
        item.pop("_mtime", None)
    return {"output_dir": str(cfg.output_dir), "files": files[:count]}


async def _list_models(args: dict) -> dict:
    cfg = config()
    models_root = cfg.root / "models"
    folder = str(args.get("folder", "")).strip().strip("/\\")
    limit = max(1, int(args.get("limit", 200)))

    if not folder:
        categories = []
        if models_root.is_dir():
            for child in sorted(models_root.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                count = sum(
                    1
                    for path in child.rglob("*")
                    if path.is_file() and path.suffix.lower() in MODEL_EXTENSIONS
                )
                categories.append({"folder": child.name, "files": count})
        return {"models_dir": str(models_root), "categories": categories}

    target = safe_child(models_root, folder)
    if not target.is_dir():
        raise RuntimeError(f"model folder not found: {folder}")
    files = []
    for path in target.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in MODEL_EXTENSIONS:
            continue
        stat = path.stat()
        files.append(
            {
                "name": str(path.relative_to(models_root)).replace("\\", "/"),
                "size_gb": round(stat.st_size / 1024 / 1024 / 1024, 2),
            }
        )
    files.sort(key=lambda item: item["name"])
    return {"folder": folder, "total": len(files), "files": files[:limit]}


async def _get_object_info(args: dict) -> dict:
    class_type = str(args["class_type"]).strip()
    port = require_port()
    data = _http_json(port, "GET", f"/object_info/{class_type}", timeout=15)
    if class_type not in data:
        raise RuntimeError(f"node class not found: {class_type}")
    spec = data[class_type]
    inputs = {}
    for section in ("required", "optional"):
        for name, definition in ((spec.get("input") or {}).get(section) or {}).items():
            inputs[name] = {"section": section, "definition": definition}
    return {
        "class_type": class_type,
        "display_name": spec.get("display_name"),
        "category": spec.get("category"),
        "outputs": spec.get("output"),
        "output_is_list": spec.get("output_is_list"),
        "inputs": inputs,
    }


async def _read_file(args: dict) -> dict:
    cfg = config()
    path = safe_child(cfg.root, str(args["path"]))
    if not path.is_file():
        raise RuntimeError(f"file not found: {args['path']}")
    content = path.read_text(encoding="utf-8", errors="replace")
    if len(content) > 20000:
        content = content[:20000] + "\n... (truncated)"
    return {"path": str(path), "content": content}


_HANDLERS = {
    "execute_python": _execute_python,
    "execute_shell": _execute_shell,
    "comfyui_status": _comfyui_status,
    "comfyui_restart": _comfyui_restart,
    "comfyui_stop": _comfyui_stop,
    "submit_workflow": _submit_workflow,
    "comfyui_job_status": _comfyui_job_status,
    "comfyui_wait_job": _comfyui_wait_job,
    "list_outputs": _list_outputs,
    "list_models": _list_models,
    "get_object_info": _get_object_info,
    "read_file": _read_file,
}


async def main() -> None:
    config()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
