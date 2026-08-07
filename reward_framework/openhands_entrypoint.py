"""Drop-in OpenHands main module for the unified reward framework.

The CyberGym launcher selects this module with
``OPENHANDS_REWARD_FRAMEWORK=1``.  Subject-model prompt and task inputs remain
unchanged except that ``submit_candidate`` is exposed as a first-class tool.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import openhands.core.main as oh_main
from openhands.core.config import parse_arguments, setup_config_from_args
from openhands.core.setup import generate_sid
from openhands.events.action import MessageAction
from openhands.io import read_task

from .adapters.openhands import (
    create_openhands_adapter,
    install_openhands_reward_framework,
)
from .backend import CodexBackend
from .instrumentation.arvo import ArvoGDBInstrumentationBackend
from .orchestrator import RewardFramework


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


def main() -> None:
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
    backend = CodexBackend(
        model=os.getenv("REWARD_FRAMEWORK_MODEL", "gpt-5.5"),
        timeout=int(os.getenv("REWARD_FRAMEWORK_MODEL_TIMEOUT", "1800")),
    )
    instrumentation = ArvoGDBInstrumentationBackend(
        image=_arvo_image(task_id), source_root=source_root,
        repo_root=repo_root,
        timeout=int(os.getenv("REWARD_FRAMEWORK_RUNTIME_TIMEOUT", "180")),
    )
    installed = []
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
            )
        else:
            framework = RewardFramework.create(
                task_id=task_id,
                issue_description=issue_path.read_text(encoding="utf-8"),
                codebase_root=source_root, state_dir=state_dir,
                backend=backend, instrumentation=instrumentation,
                platform=adapter,
            )
        installed.append(install_openhands_reward_framework(
            agent=agent, event_stream=runtime.event_stream, framework=framework
        ))
        return controller, initial_state

    oh_main.create_controller = create_controller
    try:
        asyncio.run(oh_main.run_controller(
            config=config, initial_user_action=initial_user_action, sid=sid,
            fake_user_response_fn=(
                None if args.no_auto_continue else oh_main.auto_continue_response
            ),
        ))
    finally:
        for transport in installed:
            transport.close()
        instrumentation.close()


if __name__ == "__main__":
    main()
