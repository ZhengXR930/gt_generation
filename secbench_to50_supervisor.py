#!/usr/bin/env python3
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path("/home/xinran/gt_generation")
CODE = ROOT / "gt_generation"
RUN_DIR = Path("/tmp/gt_secbench_to50_20260731")
INPUTS = RUN_DIR / "inputs"
LOGS = RUN_DIR / "logs"
TARGET_SECBENCH_OK = 50
MAX_PARALLEL = 4

REQUIRED = [
    "sample_info.json",
    "build.sh",
    "poc",
    "ground_truth.json",
    "verified_invariants.json",
    "verified_assertions.json",
    "assertion_results.json",
    "perturbation_results.json",
    "reachability_report.json",
]


def audit_ok(sample_id: str) -> bool:
    result_dir = ROOT / "gt_results" / sample_id
    if not result_dir.is_dir():
        return False
    for name in REQUIRED:
        path = result_dir / name
        if not path.is_file():
            return False
        if name != "poc" and path.stat().st_size <= 0:
            return False
    result = subprocess.run(
        [sys.executable, "-m", "gt_toolkit", "audit-package", "--result-dir", str(result_dir)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(CODE)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def secbench_ok_count() -> int:
    return sum(1 for d in (ROOT / "gt_results").glob("secbench_*") if audit_ok(d.name))


def state_of(sample_id: str) -> tuple[str, str]:
    state_path = ROOT / "gt_results" / sample_id / "gt_generation_state.json"
    if not state_path.exists():
        return ("unattempted", "")
    try:
        data = json.loads(state_path.read_text())
        return (data.get("status") or "unknown", data.get("current_stage") or "")
    except Exception:
        return ("unknown", "")


def active_samples() -> set[str]:
    result = subprocess.run(
        "ps -eo cmd | grep 'gt_generation/runner.py' | grep -v grep",
        shell=True,
        text=True,
        capture_output=True,
    )
    active: set[str] = set()
    for line in result.stdout.splitlines():
        marker = "/gt_results/"
        if marker in line:
            active.add(line.rsplit(marker, 1)[1].split()[0].split("/")[0])
    return active


def build_queue(selection: list[dict]) -> list[str]:
    unattempted: list[str] = []
    salvage: list[str] = []
    allowed_projects = {
        "mupdf",
        "php-src",
        "libxml2",
        "openexr",
        "gpac",
        "mruby",
        "libdwarf-code",
        "upx",
    }
    for sample in selection:
        sid = sample.get("sample_id", "")
        if not sid.startswith("secbench_"):
            continue
        if sample.get("project") not in allowed_projects:
            continue
        if audit_ok(sid):
            continue
        status, stage = state_of(sid)
        if status == "unattempted":
            unattempted.append(sid)
        elif status == "failed" and stage in {
            "03_trace_review",
            "04_assertion_validator",
            "05_validate",
        }:
            salvage.append(sid)

    project_order = {
        "mupdf": 0,
        "php-src": 1,
        "gpac": 2,
        "openexr": 3,
        "libxml2": 4,
        "mruby": 5,
        "libdwarf-code": 6,
        "upx": 7,
    }

    by_id = {s["sample_id"]: s for s in selection if isinstance(s, dict) and s.get("sample_id")}

    def key(sample_id: str) -> tuple[int, str]:
        return (project_order.get(by_id.get(sample_id, {}).get("project"), 99), sample_id)

    return list(dict.fromkeys(sorted(unattempted, key=key) + sorted(salvage, key=key)))


def launch(sample_id: str, sample: dict, launched: dict) -> None:
    INPUTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    input_path = INPUTS / f"{sample_id}.json"
    input_path.write_text(json.dumps(sample, indent=2, ensure_ascii=False) + "\n")

    result_dir = ROOT / "gt_results" / sample_id
    result_dir.mkdir(parents=True, exist_ok=True)
    adapter = CODE / "adapters" / "codex" / "gt_agent_codex.sh"
    provenance = {
        "schema_version": "gt-generation-provenance-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cli": "codex",
        "model": "gpt-5.4",
        "reasoning_effort": "medium",
        "strict_config": True,
        "adapter": str(adapter),
        "adapter_sha256": hashlib.sha256(adapter.read_bytes()).hexdigest(),
        "docker_track": "repo",
        "repo_docker_image": "gt-memory-env:latest",
        "repo_docker_context": str(ROOT / "docker" / "gt-memory-env"),
        "autofill_run_dir": str(RUN_DIR),
        "target": "secbench_50",
    }
    (result_dir / "generation_provenance.json").write_text(
        json.dumps(provenance, indent=2, ensure_ascii=False) + "\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT)
        + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
        "GT_AGENT_COMMAND": str(adapter),
        "GT_AGENT_MODEL": "gpt-5.4",
        "GT_AGENT_REASONING_EFFORT": "medium",
        "GT_CODEX_STRICT_CONFIG": "1",
        "GT_REPO_DOCKER_IMAGE": "gt-memory-env:latest",
        "GT_REPO_DOCKER_CONTEXT": str(ROOT / "docker" / "gt-memory-env"),
    }
    log_path = LOGS / f"{sample_id}.log"
    stream = log_path.open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            str(CODE / "runner.py"),
            "--sample",
            str(input_path),
            "--result-dir",
            str(result_dir),
        ],
        cwd=ROOT,
        env=env,
        stdout=stream,
        stderr=subprocess.STDOUT,
    )
    launched[sample_id] = {
        "proc": proc,
        "stream": stream,
        "log": str(log_path),
        "started": time.monotonic(),
    }
    print("TO50_LAUNCHED", sample_id, flush=True)


def main() -> int:
    selection = json.loads((ROOT / "dataset" / "selected_1000.json").read_text())
    by_id = {s["sample_id"]: s for s in selection if isinstance(s, dict) and s.get("sample_id")}
    queue = build_queue(selection)
    print(
        "TO50_START",
        json.dumps(
            {"sec_ok": secbench_ok_count(), "queue_len": len(queue), "first": queue[:12]},
            ensure_ascii=False,
        ),
        flush=True,
    )
    launched: dict = {}
    idx = 0
    while True:
        sec_ok = secbench_ok_count()
        if sec_ok >= TARGET_SECBENCH_OK:
            print("TO50_REACHED", json.dumps({"sec_ok": sec_ok}, ensure_ascii=False), flush=True)
            for rec in launched.values():
                proc = rec["proc"]
                if proc.poll() is None:
                    proc.terminate()
            time.sleep(5)
            for rec in launched.values():
                proc = rec["proc"]
                if proc.poll() is None:
                    proc.kill()
            return 0

        for sid, rec in list(launched.items()):
            proc = rec["proc"]
            if proc.poll() is not None and not rec.get("reported"):
                try:
                    rec["stream"].close()
                except Exception:
                    pass
                ok = audit_ok(sid) if proc.returncode == 0 else False
                print(
                    "TO50_RESULT",
                    json.dumps(
                        {
                            "sample_id": sid,
                            "returncode": proc.returncode,
                            "audit_ok": ok,
                            "duration_seconds": round(time.monotonic() - rec["started"], 3),
                            "log": rec["log"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                rec["reported"] = True

        active = active_samples()
        while len(active) < MAX_PARALLEL and idx < len(queue) and sec_ok < TARGET_SECBENCH_OK:
            sid = queue[idx]
            idx += 1
            if sid in active or audit_ok(sid):
                continue
            status, stage = state_of(sid)
            if status == "failed" and stage == "01_reproducer":
                continue
            launch(sid, by_id[sid], launched)
            active.add(sid)

        stages = {}
        for sid in sorted(active):
            state_path = ROOT / "gt_results" / sid / "gt_generation_state.json"
            stage = "starting"
            try:
                data = json.loads(state_path.read_text())
                stage = data.get("current_stage") or stage
            except Exception:
                pass
            stages[sid] = stage
        print(
            "TO50_HEARTBEAT",
            json.dumps(
                {
                    "sec_ok": sec_ok,
                    "active_count": len(active),
                    "active": stages,
                    "queue_index": idx,
                    "queue_len": len(queue),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if not active and idx >= len(queue):
            print(
                "TO50_EXHAUSTED",
                json.dumps({"sec_ok": sec_ok, "queue_len": len(queue)}, ensure_ascii=False),
                flush=True,
            )
            return 2
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
