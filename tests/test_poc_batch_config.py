import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness_runtime.openhands import arvo as openhands_arvo
from poc_generation import run_harness


def test_gpt54_mini_strict_config_uses_modelhub_endpoint():
    config = {
        "harness": "codex",
        "model": "gpt-5.4-mini-2026-03-17",
        "namespace": "codex-gpt54-mini",
        "api_key_env": "OPENAI_API_KEY",
        "base_url": (
            "https://aidp.bytedance.net/api/modelhub/online/v2/crawl/openai/"
            "deployments/gpt-5.4-mini-2026-03-17/chat/completions"
        ),
        "api_version": "2024-03-01-preview",
        "openhands_repo": str(ROOT / "external" / "OpenHands"),
    }
    args = SimpleNamespace(
        harness=None,
        model=None,
        namespace=None,
        base_url=None,
        api_key_env=None,
        api_version=None,
        max_iter=None,
        max_attempts=3,
        timeout=None,
        server=None,
        difficulty=None,
    )
    request = run_harness.build_request(args, config, "arvo_1")
    assert config["model"] == "gpt-5.4-mini-2026-03-17"
    assert request.api_key_env == "OPENAI_API_KEY"
    assert request.base_url.endswith("/chat/completions")
    assert request.api_version == "2024-03-01-preview"
    assert request.openhands_repo == ROOT / "external" / "OpenHands"


def test_valid_gt_selector_reads_authoritative_manifest(tmp_path, monkeypatch):
    valid_gt_dir = tmp_path / "gt_results"
    valid_gt_dir.mkdir()
    valid_gt = valid_gt_dir / "valid_gt.json"
    valid_gt.write_text(
        json.dumps({"samples": ["arvo_1", "osv_2", "arvo_3"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(run_harness, "REPO_ROOT", tmp_path)

    args = SimpleNamespace(
        sample=[],
        samples_file=None,
        sample_selector="valid_gt_arvo",
        start_index=1,
        limit=1,
    )

    assert run_harness.load_samples(args, {}) == ["arvo_3"]


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
    config["harness"] = "codex"
    config["samples"] = ["arvo_1"]
    args = SimpleNamespace(
        sample=[],
        samples_file=None,
        sample_selector=None,
        start_index=0,
        limit=0,
        harness=None,
        model=None,
        namespace=None,
        base_url=None,
        api_key_env=None,
        api_version=None,
        max_iter=None,
        max_attempts=1,
        timeout=None,
        server=None,
        difficulty=None,
        overwrite=True,
        dry_run=False,
    )
    monkeypatch.setattr(run_harness, "LOG_ROOT", tmp_path / "logs")
    captured = {}

    class Completed:
        returncode = 0

    def fake_run(command, **kwargs):
        captured["command"] = command
        sample_dir = tmp_path / "test" / "arvo_1"
        (sample_dir / "checkpoint").mkdir(parents=True)
        (sample_dir / "manifest.json").write_text(json.dumps({"status": "agent_finished"}))
        (sample_dir / "analysis.json").write_text(json.dumps({
            "sample_id": "arvo_1",
            "fine_trace": [],
            "vuln_logic": {},
        }))
        return Completed()

    monkeypatch.setattr(run_harness.subprocess, "run", fake_run)
    monkeypatch.setattr(run_harness, "result_is_complete", lambda sample_dir: True)
    monkeypatch.setattr(run_harness, "maybe_run_reachability", lambda *args, **kwargs: {"status": "disabled"})
    result = run_harness.run_one(args, config, "arvo_1")

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
    class DockerModule:
        @staticmethod
        def from_env():
            return client

    monkeypatch.setattr(openhands_arvo, "_docker_sdk", lambda: (DockerModule, object))

    assert openhands_arvo.runtime_server_url(
        "http://host.docker.internal:8666"
    ) == "http://172.20.0.1:8666"
