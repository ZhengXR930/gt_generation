import json
import subprocess

from gt_generation.gt_toolkit import prepare


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
