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
    COMFYUI_VSDEV       Initialize the VS 2022 x64 developer environment (default: on)
    COMFYUI_ADMIN       Start ComfyUI elevated via UAC (default: off)

Run:
    python server.py
"""

from __future__ import annotations

import asyncio
import ctypes
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

SERVER_VERSION = "0.2.0"
DEFAULT_PORTS = (8188, 8189, 8190)
MODEL_EXTENSIONS = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf"}
OUTPUT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".gif", ".wav", ".mp3", ".flac"}

server = Server("comfyui-runner", version=SERVER_VERSION)

STILL_ACTIVE = 259
SEE_MASK_NOCLOSEPROCESS = 0x00000040
ERROR_CANCELLED = 1223
WAIT_TIMEOUT = 0x102


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _find_vcvars64() -> Path | None:
    if os.name != "nt":
        return None
    env_dir = os.environ.get("VSINSTALLDIR", "").strip()
    if env_dir:
        candidate = Path(env_dir) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
        if candidate.is_file():
            return candidate

    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if vswhere.is_file():
        try:
            found = subprocess.run(
                [
                    str(vswhere),
                    "-latest",
                    "-products",
                    "*",
                    "-requires",
                    "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                    "-property",
                    "installationPath",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            found = None
        if found is not None and found.returncode == 0:
            for line in found.stdout.splitlines():
                install = line.strip()
                if not install:
                    continue
                candidate = Path(install) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
                if candidate.is_file():
                    return candidate

    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if not base:
            continue
        matches = sorted(Path(base).glob("Microsoft Visual Studio/2022/*/VC/Auxiliary/Build/vcvars64.bat"))
        if matches:
            return matches[-1]
    return None


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)
    if not handle:
        return False
    code = wintypes.DWORD()
    ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return bool(ok) and code.value == STILL_ACTIVE


def _run_elevated(executable: str, parameters: str, cwd: Path, wait_ms: int | None = None) -> dict:
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = executable
    sei.lpParameters = parameters
    sei.lpDirectory = str(cwd)
    sei.nShow = 0

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
    kernel32.GetProcessId.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            return {"status": "elevation_cancelled", "hint": "The UAC prompt was declined."}
        return {"status": "launch_failed", "error": f"ShellExecuteExW failed (WinError {err})"}

    pid = kernel32.GetProcessId(sei.hProcess) if sei.hProcess else None
    result: dict = {"status": "started", "pid": pid}
    if wait_ms is not None and sei.hProcess:
        wait_result = kernel32.WaitForSingleObject(sei.hProcess, wait_ms)
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code)):
            result["exit_code"] = code.value
        result["timed_out"] = wait_result == WAIT_TIMEOUT
    if sei.hProcess:
        kernel32.CloseHandle(sei.hProcess)
    return result


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
        self.launch_state = self.root / "user" / "comfyui_runner.launch.json"
        self.launch_script = self.root / "user" / "comfyui_runner_launch.bat"
        self.vsdev = _env_flag("COMFYUI_VSDEV", True)
        self.admin = _env_flag("COMFYUI_ADMIN", False)
        self.vcvars = _find_vcvars64() if (os.name == "nt" and self.vsdev) else None


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
        cfg = config()
        where = f"port {cfg.port_override}" if cfg.port_override else "ports 8188-8190"
        raise RuntimeError(f"ComfyUI is not running (no responsive {where}). Use comfyui_restart first.")
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
    if status_str == "error":
        state = "error"
    elif completed:
        state = "completed"
    else:
        state = "running"
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
                    "anyOf": [{"type": "string"}, {"type": "object"}],
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
    launch = _read_launch_state(cfg)
    return {
        "status": "running",
        "port": port,
        "version": system.get("comfyui_version"),
        "python": system.get("python_version"),
        "pytorch": system.get("pytorch_version"),
        "argv": system.get("argv") or [],
        "devices": devices,
        "admin_launch": bool(launch.get("admin")),
        "vsdev": launch.get("vsdev"),
        "queue": {
            "running": sum(1 for state in queue.values() if state == "running"),
            "pending": sum(1 for state in queue.values() if state == "pending"),
        },
        "output_dir": str(cfg.output_dir),
    }


def _cmd_arg(value: str) -> str:
    if value and not any(ch in value for ch in ' \t"&|<>^()'):
        return value
    return f'"{value}"'


def _launch_script(cfg: Config, start_args: list[str]) -> Path:
    python_cmd = " ".join([f'"{cfg.python}"', "main.py", *[_cmd_arg(arg) for arg in start_args]])
    lines = ["@echo off", f'cd /d "{cfg.root}"']
    if cfg.vcvars is not None:
        lines.append(f'call "{cfg.vcvars}" >nul 2>&1')
    lines.append('set "PYTHONUTF8=1"')
    lines.append(f'{python_cmd} > "{cfg.log_file}" 2>&1')
    cfg.launch_script.parent.mkdir(parents=True, exist_ok=True)
    cfg.launch_script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cfg.launch_script


def _write_launch_state(cfg: Config, pid: int, port: int, script: Path) -> str | None:
    try:
        cfg.launch_state.parent.mkdir(parents=True, exist_ok=True)
        cfg.launch_state.write_text(
            json.dumps(
                {
                    "pid": pid,
                    "port": port,
                    "admin": cfg.admin,
                    "vsdev": str(cfg.vcvars) if cfg.vcvars else None,
                    "script": str(script),
                    "started_at": round(time.time(), 3),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        return str(exc)
    return None


def _read_launch_state(cfg: Config) -> dict:
    try:
        data = json.loads(cfg.launch_state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _clear_launch_state(cfg: Config) -> None:
    try:
        cfg.launch_state.unlink()
    except OSError:
        pass


def _find_comfyui_pids() -> dict[int, str]:
    cfg = config()
    found: dict[int, str] = {}
    try:
        import psutil
    except ImportError:
        return found

    for proc in psutil.process_iter(["pid", "cmdline", "cwd"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if not any(Path(arg).name.lower() == "main.py" for arg in cmdline if arg):
                continue
            cwd = proc.info.get("cwd") or ""
            joined = " ".join(cmdline).lower()
            cwd_matches = bool(cwd) and Path(cwd).resolve() == cfg.root
            if cwd_matches or str(cfg.root).lower() in joined:
                found[proc.pid] = "main.py process"
        except (psutil.Error, OSError):
            continue
    return found


def _kill_pid(pid: int) -> str:
    try:
        import psutil
    except ImportError:
        psutil = None

    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            children = proc.children(recursive=True)
            for child in children:
                try:
                    child.kill()
                except psutil.Error:
                    continue
            proc.kill()
            psutil.wait_procs([proc, *children], timeout=5)
            return "killed"
        except psutil.NoSuchProcess:
            return "gone"
        except psutil.Error as exc:
            return str(exc)

    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            return "taskkill timeout"
        output = _decode(proc.stdout + proc.stderr).strip()
        return "killed" if proc.returncode == 0 else output
    os.kill(pid, signal.SIGTERM)
    return "signalled"


def _state_process_alive(state: dict) -> bool:
    pid = int(state.get("pid") or 0)
    if not pid or not _pid_alive(pid):
        return False
    started_at = state.get("started_at")
    if not started_at:
        return True
    try:
        import psutil
    except ImportError:
        return True
    try:
        created = psutil.Process(pid).create_time()
    except psutil.Error:
        return True
    return abs(created - float(started_at)) < 300


def _stop_comfyui(kill_mcp: bool) -> dict:
    cfg = config()
    state = _read_launch_state(cfg)
    ports = set()
    if cfg.port_override:
        ports.add(cfg.port_override)
    if state.get("port"):
        ports.add(int(state["port"]))
    if not ports:
        ports.update(DEFAULT_PORTS)
    if kill_mcp:
        ports.add(9100)

    admin_launch = bool(state.get("admin"))
    state_alive = _state_process_alive(state)

    pids = _find_comfyui_pids()
    if state_alive:
        pids = {int(state["pid"]): "launch process"}

    killed: list[dict] = []
    failed: list[dict] = []
    for pid, source in pids.items():
        result = _kill_pid(pid)
        if result in {"killed", "gone"}:
            killed.append({"pid": pid, "source": source})
            continue
        if admin_launch:
            elevated = _run_elevated("taskkill.exe", f"/F /T /PID {pid}", cfg.root, wait_ms=20000)
            if (
                elevated.get("status") == "started"
                and not elevated.get("timed_out")
                and elevated.get("exit_code") == 0
            ):
                killed.append({"pid": pid, "source": source, "elevated": True})
                continue
            failed.append({"pid": pid, "source": source, "detail": result, "elevated": elevated})
            continue
        failed.append({"pid": pid, "source": source, "detail": result})

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        remaining = set(_find_comfyui_pids())
        if _state_process_alive(state):
            remaining.add(int(state["pid"]))
        still_open = [port for port in ports if _port_open(cfg.host, port, timeout=0.3)]
        if not remaining and not still_open:
            break
        time.sleep(0.5)

    remaining = set(_find_comfyui_pids())
    if _state_process_alive(state):
        remaining.add(int(state["pid"]))
    still_open = [port for port in ports if _port_open(cfg.host, port, timeout=0.3)]
    stopped = not remaining and not still_open
    if not _state_process_alive(state) and not remaining:
        _clear_launch_state(cfg)

    return {
        "stopped": stopped,
        "admin_launch": admin_launch,
        "killed": killed,
        "failed": failed,
        "remaining": sorted(remaining),
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
        start_args += ["--port", str(cfg.port_override or 8188)]

    port = cfg.port_override or 8188
    for index, arg in enumerate(start_args):
        if arg == "--port" and index + 1 < len(start_args):
            port = int(start_args[index + 1])
        elif arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])

    if not stop_result["stopped"] and stop_result["failed"]:
        return {
            "status": "stop_failed",
            "port": port,
            "stop": stop_result,
            "hint": "ComfyUI could not be stopped. Approve the UAC prompt or stop it manually, then retry.",
        }

    if _port_open(cfg.host, port):
        return {
            "status": "port_busy",
            "port": port,
            "stop": stop_result,
            "hint": "Another process owns this port. Retry with extra_args='--port 8888' or update comfyui_port.",
        }

    script = _launch_script(cfg, start_args)
    command_line = f'cmd.exe /d /s /c ""{script}""'
    if cfg.admin:
        elevated = _run_elevated("cmd.exe", f'/d /s /c ""{script}""', cfg.root)
        if elevated.get("status") != "started":
            return {
                "status": elevated.get("status", "launch_failed"),
                "port": port,
                "stop": stop_result,
                "script": str(script),
                "error": elevated.get("error"),
                "hint": elevated.get("hint"),
            }
        pid = int(elevated["pid"])
    else:
        creation_flags = 0
        if os.name == "nt":
            # A hidden console keeps MKL/Fortran nodes from aborting when the MCP client exits.
            creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            command_line,
            cwd=str(cfg.root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        pid = proc.pid

    state_error = _write_launch_state(cfg, pid, port, script)
    return {
        "status": "starting",
        "pid": pid,
        "port": port,
        "admin": cfg.admin,
        "vsdev": str(cfg.vcvars) if cfg.vcvars else None,
        "script": str(script),
        "command": command_line,
        "log_file": str(cfg.log_file),
        "stop": stop_result,
        "state_error": state_error,
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
