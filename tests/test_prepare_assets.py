import json
import subprocess

from gt_generation.gt_toolkit import prepare


def test_non_arvo_memory_env_uses_configured_image_and_context(tmp_path, monkeypatch):
    calls = []

    def fake_sh(command, timeout=None):
        calls.append(command)
        return type("Result", (), {"stdout": "", "returncode": 0})()

    context = tmp_path / "docker-context"
    context.mkdir()
    monkeypatch.setenv("GT_REPO_DOCKER_IMAGE", "custom-memory:latest")
    monkeypatch.setenv("GT_REPO_DOCKER_CONTEXT", str(context))
    monkeypatch.setattr(prepare, "_sh", fake_sh)

    assert prepare._ensure_memory_env() is True
    assert calls[-1] == ["docker", "build", "-t", "custom-memory:latest", str(context)]


def test_prepare_preserves_input_as_sample_info(tmp_path, monkeypatch):
    sample = tmp_path / "input.json"
    sample.write_text(
        json.dumps({"sample_id": "arvo_1", "source_dataset": "ARVO-Meta"})
    )
    result_dir = tmp_path / "result"
    monkeypatch.setattr(
        prepare,
        "_prepare_arvo",
        lambda data, directory: {"prepared": True, "sample_id": data["sample_id"]},
    )

    report = prepare.prepare(str(sample), str(result_dir))

    assert report["prepared"] is True
    assert json.loads((result_dir / "sample_info.json").read_text()) == json.loads(
        sample.read_text()
    )


def test_prepare_refreshes_result_local_sample_info_without_rewriting_it(
    tmp_path, monkeypatch
):
    result_dir = tmp_path / "result"
    result_dir.mkdir()
    sample = result_dir / "sample_info.json"
    raw = '{"sample_id":"arvo_1","source_dataset":"ARVO-Meta"}\n'
    sample.write_text(raw)
    monkeypatch.setattr(
        prepare,
        "_prepare_arvo",
        lambda data, directory: {"prepared": True, "sample_id": data["sample_id"]},
    )

    prepare.prepare(str(sample), str(result_dir))

    assert sample.read_text() == raw


def test_prepare_stages_inline_default_crash_trace_without_rewriting_sample(tmp_path, monkeypatch):
    sample_data = {
        "sample_id": "arvo_1",
        "source_dataset": "ARVO-Meta",
        "default_crash_trace": "ASAN: exact public trace\n",
    }
    sample = tmp_path / "input.json"
    sample.write_text(json.dumps(sample_data))
    result_dir = tmp_path / "result"
    monkeypatch.setattr(
        prepare,
        "_prepare_arvo",
        lambda data, directory: {"prepared": True, "sample_id": data["sample_id"]},
    )

    report = prepare.prepare(str(sample), str(result_dir))

    assert (result_dir / "default_crash_trace.txt").read_text() == sample_data[
        "default_crash_trace"
    ]
    assert report["public_context"]["default_crash_trace_staged"] is True
    assert json.loads((result_dir / "sample_info.json").read_text()) == sample_data


def test_prepare_stages_default_crash_trace_from_declared_path(tmp_path, monkeypatch):
    trace = tmp_path / "public-error.txt"
    trace.write_text("original crash state")
    sample = tmp_path / "input.json"
    sample.write_text(
        json.dumps(
            {
                "sample_id": "arvo_1",
                "source_dataset": "ARVO-Meta",
                "default_crash_trace_path": trace.name,
            }
        )
    )
    result_dir = tmp_path / "result"
    monkeypatch.setattr(
        prepare,
        "_prepare_arvo",
        lambda data, directory: {"prepared": True, "sample_id": data["sample_id"]},
    )

    prepare.prepare(str(sample), str(result_dir))

    assert (result_dir / "default_crash_trace.txt").read_text() == trace.read_text()


def test_prepare_stages_default_crash_trace_from_public_poc_text(tmp_path, monkeypatch):
    poc_dir = tmp_path / "pocs" / "sample_1"
    poc_dir.mkdir(parents=True)
    (poc_dir / "poc").write_text(
        "## Command\n./target @@\n\n"
        "=================================================================\n"
        "==123==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x1\n"
        "    #0 0xabc in crash src/file.c:10\n"
        "SUMMARY: AddressSanitizer: heap-buffer-overflow src/file.c:10 in crash\n"
        "==123==ABORTING\n"
        "\n## Attachment\npoc.zip\n"
    )
    sample = tmp_path / "input.json"
    sample.write_text(
        json.dumps(
            {
                "sample_id": "sample_1",
                "source_family": "secbench",
                "repo": "https://example.test/repo.git",
                "poc_path": "pocs/sample_1/poc",
            }
        )
    )
    result_dir = tmp_path / "result"
    monkeypatch.setattr(
        prepare,
        "_prepare_repo",
        lambda data, directory: {"prepared": True, "sample_id": data["sample_id"]},
    )

    report = prepare.prepare(str(sample), str(result_dir))

    trace = (result_dir / "default_crash_trace.txt").read_text()
    assert trace.startswith("=================================================================")
    assert "ERROR: AddressSanitizer" in trace
    assert "==123==ABORTING" in trace
    assert "## Attachment" not in trace
    assert report["public_context"]["source_kind"] == "public_poc_sanitizer_report"


def test_extract_sanitizer_trace_from_saved_github_html_clipboard_snippet():
    html = (
        '<div data-snippet-clipboard-copy-content="prefix\\n'
        '==7==ERROR: AddressSanitizer: heap-use-after-free on address 0x1\\n'
        'SUMMARY: AddressSanitizer: heap-use-after-free src/x.c:2 in f\\n'
        '==7==ABORTING"></div>'
    )

    trace = prepare._extract_sanitizer_trace_text(html)

    assert trace.startswith("==7==ERROR: AddressSanitizer")
    assert "SUMMARY: AddressSanitizer" in trace
    assert trace.endswith("==7==ABORTING\n")


def test_prepare_can_capture_default_arvo_crash_trace_from_stock_image(
    tmp_path, monkeypatch
):
    (tmp_path / "poc").write_bytes(b"poc")
    monkeypatch.setattr(
        prepare,
        "_sh",
        lambda command, timeout=None: subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="ERROR: AddressSanitizer: bad-free\n",
        ),
    )

    report = prepare._capture_arvo_default_crash_trace(
        {"sample_id": "arvo_1", "source_dataset": "ARVO-Meta"},
        tmp_path,
    )

    assert report["default_crash_trace_staged"] is True
    assert report["returncode"] == 1
    assert (tmp_path / "default_crash_trace.txt").read_text().startswith("ERROR:")
