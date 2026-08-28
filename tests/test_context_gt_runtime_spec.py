import json

from gt_generation.gt_toolkit.context_trace import _debug_command_from_runtime_spec


def test_context_gt_runs_shebang_runtime_wrappers_under_bash(tmp_path):
    sample = tmp_path / "sample"
    wrapper = sample / "_work" / "runtime" / "sample" / "run_poc.sh"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        '''#!/usr/bin/env bash
exec /gt/_work/src/target "$@"
''',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    (sample / "runtime_spec.json").write_text(
        json.dumps(
            {
                "sample_id": "sample",
                "backend": "local_workspace",
                "image": "gt-memory-env:latest",
                "workdir": "/gt/_work/src",
                "executable": "/gt/_work/runtime/sample/run_poc.sh",
                "arguments": ["{poc}"],
                "environment": {"ASAN_OPTIONS": "detect_leaks=0"},
                "input_placeholder": "{poc}",
                "source": "test",
            }
        ),
        encoding="utf-8",
    )

    command = _debug_command_from_runtime_spec(sample)

    assert "ASAN_OPTIONS=detect_leaks=0" in command
    assert "/bin/bash" in command
    assert "/gt/_work/runtime/sample/run_poc.sh" in command
    assert "/gt/poc" in command
