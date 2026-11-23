import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("comfyui_runner_server", ROOT / "server.py")
server = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = server
SPEC.loader.exec_module(server)


def test_collect_output_files_handles_images_and_video():
    entry = {
        "outputs": {
            "8": {"images": [{"filename": "shot.png", "subfolder": "", "type": "output"}]},
            "12": {
                "gifs": [
                    {
                        "filename": "shot.mp4",
                        "subfolder": "videos",
                        "type": "output",
                        "format": "video/h264-mp4",
                    }
                ]
            },
        }
    }
    files = server.collect_output_files(entry)
    assert {item["filename"] for item in files} == {"shot.png", "shot.mp4"}
    assert {item["kind"] for item in files} == {"images", "gifs"}


def test_collect_output_files_ignores_malformed_items():
    entry = {"outputs": {"1": {"images": [None, {"subfolder": "x"}]}, "2": "not-a-dict"}}
    assert server.collect_output_files(entry) == []


def test_summarize_history_completed_and_error():
    completed = server.summarize_history(
        {"status": {"completed": True, "status_str": "success"}, "outputs": {}}, "pid-1"
    )
    assert completed["status"] == "completed"
    assert completed["prompt_id"] == "pid-1"

    failed = server.summarize_history(
        {"status": {"completed": True, "status_str": "error", "messages": [["execution_error"]]}},
        "pid-2",
    )
    assert failed["status"] == "error"
    assert failed["messages"] == [["execution_error"]]

    interrupted = server.summarize_history(
        {"status": {"completed": False, "status_str": "error", "messages": [["execution_error"]]}},
        "pid-3",
    )
    assert interrupted["status"] == "error"

    running = server.summarize_history(
        {"status": {"completed": False, "status_str": "success"}, "outputs": {}}, "pid-4"
    )
    assert running["status"] == "running"


def test_normalize_workflow_accepts_json_string_and_object():
    object_form = {"1": {"class_type": "SaveImage", "inputs": {}}}
    assert server.normalize_workflow(object_form) is object_form
    assert server.normalize_workflow(json.dumps(object_form)) == object_form
    with pytest.raises(ValueError):
        server.normalize_workflow({})


def test_safe_child_rejects_escape(tmp_path):
    inside = server.safe_child(tmp_path, "output/shot.png")
    assert inside == (tmp_path / "output" / "shot.png").resolve()
    with pytest.raises(ValueError):
        server.safe_child(tmp_path, "../outside.txt")


def test_config_reads_environment(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("COMFYUI_DIR", str(tmp_path))
    monkeypatch.setenv("COMFYUI_PORT", "8199")
    monkeypatch.setenv("COMFYUI_START_ARGS", "--port 8199 --use-pytorch-cross-attention")
    monkeypatch.setenv("COMFYUI_PYTHON", sys.executable)
    monkeypatch.setenv("COMFYUI_VSDEV", "0")
    server._config = None

    cfg = server.config()
    assert cfg.root == tmp_path.resolve()
    assert cfg.port_override == 8199
    assert cfg.start_args == ["--port", "8199", "--use-pytorch-cross-attention"]
    assert cfg.output_dir == tmp_path.resolve() / "output"
    server._config = None


def test_launch_script_sets_cwd_vsdev_and_python(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("COMFYUI_DIR", str(tmp_path))
    monkeypatch.setenv("COMFYUI_PYTHON", sys.executable)
    monkeypatch.setenv("COMFYUI_VSDEV", "0")
    server._config = None

    cfg = server.config()
    cfg.log_file = tmp_path / "run.log"
    cfg.launch_script = tmp_path / "launch.bat"
    cfg.vcvars = tmp_path / "vcvars64.bat"
    script = server._launch_script(cfg, ["--port", "8888"])
    text = script.read_text(encoding="utf-8")

    assert f'cd /d "{tmp_path.resolve()}"' in text
    assert 'call "' in text and "vcvars64.bat" in text
    assert "main.py --port 8888" in text
    assert f'"{sys.executable}"' in text
    server._config = None


def test_config_requires_comfyui_dir(monkeypatch):
    monkeypatch.delenv("COMFYUI_DIR", raising=False)
    server._config = None
    with pytest.raises(RuntimeError, match="COMFYUI_DIR"):
        server.config()
    server._config = None


def test_config_rejects_non_comfyui_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("COMFYUI_DIR", str(tmp_path))
    server._config = None
    with pytest.raises(RuntimeError, match="does not look like a ComfyUI checkout"):
        server.config()
    server._config = None
