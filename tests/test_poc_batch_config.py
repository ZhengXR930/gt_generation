import importlib
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
POC_GENERATOR = ROOT / "poc_generation" / "poc_generator"
sys.path.insert(0, str(POC_GENERATOR))

run_config_batch = importlib.import_module("run_config_batch")
rerun_model_batches = importlib.import_module("rerun_model_batches")
run_sample = importlib.import_module("run_sample")


def test_gpt54_mini_strict_config_uses_modelhub_endpoint():
    config = rerun_model_batches.load_config("poc_config.gpt54_mini_strict_gt.json")

    assert config["model"] == "gpt-5.4-mini-2026-03-17"
    assert config["api_key_env"] == "OPENAI_API_KEY"
    assert config["base_url"] == (
        "https://aidp.bytedance.net/api/modelhub/online/v2/crawl/openai/"
        "deployments/gpt-5.4-mini-2026-03-17/chat/completions"
        "?api-version=2024-03-01-preview"
    )
    assert config["openhands_repo"] == str(ROOT / "external" / "OpenHands")


def test_strict_gt_selector_reads_only_complete_section(tmp_path, monkeypatch):
    status = tmp_path / "GT_STATUS.md"
    status.write_text(
        "# Status\n\n## Complete\n- arvo_1\n- osv_2\n\n## Incomplete\n- arvo_3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(run_config_batch, "GT_ROOT", tmp_path)

    assert run_config_batch.select_samples(
        {"sample_selector": "strict_gt_complete"}
    ) == ["arvo_1", "osv_2"]


def test_batch_runner_passes_api_version(tmp_path, monkeypatch):
    config = {
        "results_namespace": "test",
        "model": "gpt-5.4-mini-2026-03-17",
        "base_url": "https://example.test/chat/completions?api-version=v1",
        "api_version": "2024-03-01-preview",
        "api_key_env": "OPENAI_API_KEY",
        "max_iter": 100,
        "max_attempts": 1,
        "server": "http://server",
        "difficulty": "level1",
        "openhands_repo": str(ROOT / "external" / "OpenHands"),
    }
    monkeypatch.setattr(rerun_model_batches, "RESULTS_ROOT", tmp_path / "results")
    monkeypatch.setattr(rerun_model_batches, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(rerun_model_batches, "LOCK_ROOT", tmp_path / "locks")
    captured = {}

    class Completed:
        returncode = 0

    def fake_run(command, **kwargs):
        captured["command"] = command
        return Completed()

    monkeypatch.setattr(rerun_model_batches.subprocess, "run", fake_run)
    result = rerun_model_batches.run_one(config, "arvo_1")

    command = captured["command"]
    assert command[command.index("--api-version") + 1] == "2024-03-01-preview"
    assert result["status"] == "complete"


def test_runtime_server_url_uses_active_docker_bridge_gateway(monkeypatch):
    bridge = SimpleNamespace(
        attrs={"IPAM": {"Config": [{"Gateway": "172.20.0.1"}]}}
    )
    client = SimpleNamespace(
        networks=SimpleNamespace(get=lambda name: bridge)
    )
    monkeypatch.delenv("OPENHANDS_EVAL_HOST_GATEWAY", raising=False)
    monkeypatch.setattr(run_sample.docker, "from_env", lambda: client)

    assert run_sample.runtime_server_url(
        "http://host.docker.internal:8666"
    ) == "http://172.20.0.1:8666"
