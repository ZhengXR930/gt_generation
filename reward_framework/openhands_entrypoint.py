"""OpenHands main module owned by the reward framework.

This module is not selected by the normal ``poc_generation`` evaluator.  A
reward-only launcher must opt in with ``OPENHANDS_REWARD_FRAMEWORK=1`` and the
reward harness profile. Subject-model prompt and task inputs remain unchanged
except that ``submit_candidate`` is exposed as a first-class tool.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

import openhands.core.main as oh_main
from openhands.core.config import parse_arguments, setup_config_from_args
from openhands.core.schema import AgentState
from openhands.core.setup import generate_sid
from openhands.events.action import MessageAction
from openhands.io import read_task

from .adapters.openhands import (
    create_openhands_adapter,
    install_closed_network_runtime_route,
    install_openhands_reward_framework,
)
from .backend import CodexBackend
from .instrumentation.arvo import ArvoGDBInstrumentationBackend
from .orchestrator import RewardFramework
from .state_store import atomic_json
from poc_generation.openhands_fine_trace_main import install_fine_trace_overlay


def _source_root(workspace: Path) -> Path:
    override = os.getenv("REWARD_FRAMEWORK_SOURCE_ROOT", "").strip()
    candidates = ([Path(override)] if override else []) + [
        workspace / "repo-vul", workspace / "_work" / "src", workspace / "src",
    ]
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_dir():
            return resolved
    raise RuntimeError("could not locate the vulnerable source tree in the task workspace")


def _arvo_image(task_id: str) -> str:
    match = re.fullmatch(r"arvo[:_](\d+)", task_id)
    if not match:
        raise RuntimeError(
            "the formal runtime adapter currently supports ARVO task ids; "
            f"received {task_id!r}"
        )
    return f"n132/arvo:{match.group(1)}-vul"


def _is_iteration_limit_error(final_state) -> bool:
    """Return whether OpenHands used ERROR to report normal budget exhaustion."""
    if final_state is None or final_state.agent_state != AgentState.ERROR:
        return False
    message = str(final_state.last_error or "").lower()
    return "reached maximum iteration" in message


def _codex_executable() -> str:
    configured = os.getenv("REWARD_FRAMEWORK_CODEX_EXECUTABLE", "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"configured Codex executable is missing: {path}")
        return str(path)
    discovered = shutil.which("codex")
    if discovered:
        return discovered
    user_install = Path.home() / ".local" / "bin" / "codex"
    if user_install.is_file():
        return str(user_install.resolve())
    raise FileNotFoundError(
        "Codex CLI is required by the Reward Agent; set "
        "REWARD_FRAMEWORK_CODEX_EXECUTABLE"
    )


def _codex_auth_file(executable: str) -> Path:
    configured = os.getenv("REWARD_FRAMEWORK_CODEX_AUTH_FILE", "").strip()
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if not candidate.is_file():
            raise FileNotFoundError(
                f"configured Codex auth file is missing: {candidate}"
            )
        return candidate
    resolved = Path(executable).expanduser().resolve()
    for parent in (resolved, *resolved.parents):
        if parent.name == ".codex":
            candidate = parent / "auth.json"
            if candidate.is_file():
                return candidate
    raise FileNotFoundError(
        "could not locate auth.json beside the configured Codex executable; "
        "set REWARD_FRAMEWORK_CODEX_AUTH_FILE"
    )


def main() -> None:
    if (
        os.getenv("OPENHANDS_HARNESS_PROFILE") != "reward"
        or os.getenv("OPENHANDS_REWARD_FRAMEWORK") != "1"
    ):
        raise RuntimeError(
            "reward_framework.openhands_entrypoint may only run under "
            "--harness-profile reward"
        )
    # The reward entrypoint replaces the ordinary evaluation entrypoint, so it
    # must install the same lifecycle-only finalization overlay itself.  This
    # preserves the pre-finalization checkpoint and yields the required
    # tool-free fine trace when a zero-submission episode reaches its budget.
    install_fine_trace_overlay()
    install_closed_network_runtime_route()
    args = parse_arguments()
    config = setup_config_from_args(args)
    task_prompt = read_task(args, config.cli_multiline_input)
    if not task_prompt:
        raise ValueError("No task provided")
    initial_user_action = MessageAction(content=task_prompt)
    sid = generate_sid(config, args.name)

    workspace = Path(os.environ["OPENHANDS_TASK_WORKSPACE"]).resolve()
    task_id = os.environ["REWARD_FRAMEWORK_TASK_ID"]
    issue_path = workspace / "description.txt"
    if not issue_path.is_file():
        raise FileNotFoundError(f"public issue description is missing: {issue_path}")
    state_dir = Path(os.environ["REWARD_FRAMEWORK_STATE_DIR"]).resolve()
    source_root = _source_root(workspace)
    repo_root = Path(__file__).resolve().parents[1]
    sessions = state_dir / "codex_sessions"
    codex_executable = _codex_executable()
    codex_auth_file = _codex_auth_file(codex_executable)
    codex_isolation_image = os.getenv(
        "REWARD_FRAMEWORK_CODEX_ISOLATION_IMAGE", "gt-reward-controller:0.33"
    ).strip()
    backend = CodexBackend(
        model=os.getenv("REWARD_FRAMEWORK_MODEL", "gpt-5.5"),
        executable=codex_executable,
        timeout=int(os.getenv("REWARD_FRAMEWORK_MODEL_TIMEOUT", "600")),
        session_file=sessions / "reward_agent.session",
        sandbox="read-only",
        fresh_each_run=True,
        isolation_image=codex_isolation_image or None,
        isolation_auth_file=codex_auth_file,
    )
    spec_cache_value = os.getenv("REWARD_FRAMEWORK_SPEC_CACHE_ROOT", "").strip()
    spec_cache_root = (
        Path(spec_cache_value).resolve()
        if spec_cache_value else None
    )
    harness_version = int(os.getenv(
        "REWARD_FRAMEWORK_EPISODE_HARNESS_VERSION", "1"
    ))
    baseline_profile = os.getenv(
        "REWARD_FRAMEWORK_BASELINE_PROFILE", "openhands_0.33.0_pristine"
    )
    max_iterations = int(os.getenv("REWARD_FRAMEWORK_MAX_ITERATIONS", "100"))
    instrumentation = ArvoGDBInstrumentationBackend(
        image=_arvo_image(task_id), source_root=source_root,
        repo_root=repo_root,
        timeout=int(os.getenv("REWARD_FRAMEWORK_RUNTIME_TIMEOUT", "180")),
    )
    installed = []
    frameworks = []
    original_create_controller = oh_main.create_controller

    def create_controller(agent, runtime, app_config, replay_events=None):
        controller, initial_state = original_create_controller(
            agent, runtime, app_config, replay_events=replay_events
        )
        adapter = create_openhands_adapter(
            workspace_root=workspace, event_stream=runtime.event_stream,
            checkpoint_root=state_dir / "checkpoints",
        )
        if (state_dir / "task_context.json").is_file():
            framework = RewardFramework.resume(
                state_dir=state_dir, backend=backend,
                instrumentation=instrumentation, platform=adapter,
                baseline_profile=baseline_profile,
                harness_version=harness_version,
                max_iterations=max_iterations,
            )
        else:
            framework = RewardFramework.create(
                task_id=task_id,
                issue_description=issue_path.read_text(encoding="utf-8"),
                codebase_root=source_root, state_dir=state_dir,
                backend=backend, instrumentation=instrumentation,
                platform=adapter,
                baseline_profile=baseline_profile,
                harness_version=harness_version,
                max_iterations=max_iterations,
                spec_cache_root=spec_cache_root,
            )
        frameworks.append(framework)
        installed.append(install_openhands_reward_framework(
            agent=agent, event_stream=runtime.event_stream, framework=framework
        ))
        return controller, initial_state

    oh_main.create_controller = create_controller
    completed = False
    try:
        final_state = asyncio.run(oh_main.run_controller(
            config=config, initial_user_action=initial_user_action, sid=sid,
            fake_user_response_fn=(
                None if args.no_auto_continue else oh_main.auto_continue_response
            ),
        ))
        error_state = bool(
            final_state is not None
            and final_state.agent_state == AgentState.ERROR
        )
        iteration_limit = _is_iteration_limit_error(final_state)
        if error_state and not iteration_limit:
            for framework in frameworks:
                framework.record_event(
                    source="controller",
                    kind="episode_aborted",
                    payload={
                        "reason": "openhands_error",
                        "last_error": str(final_state.last_error or "")[:1000],
                    },
                )
                atomic_json(
                    framework.store.root / "episode_abort.json",
                    {
                        "reason": "openhands_error",
                        "last_error": str(final_state.last_error or "")[:1000],
                        "gt_used": False,
                    },
                )
        else:
            if iteration_limit:
                # OpenHands 0.33 represents ordinary iteration-budget
                # exhaustion as AgentState.ERROR. It is still a completed
                # reward episode.
                for framework in frameworks:
                    framework.reach_iteration_limit(
                        iteration=max_iterations, maximum=max_iterations
                    )
            completed = True
    finally:
        try:
            if completed:
                for framework in frameworks:
                    try:
                        framework.finalize_episode()
                    except Exception as exc:
                        atomic_json(
                            framework.store.root / "episode_summary_error.json",
                            {
                                "episode_harness_version": harness_version,
                                "summary_error": f"{type(exc).__name__}: {exc}",
                                "gt_used": False,
                            },
                        )
        finally:
            for transport in installed:
                transport.close()
            instrumentation.close()


if __name__ == "__main__":
    main()
