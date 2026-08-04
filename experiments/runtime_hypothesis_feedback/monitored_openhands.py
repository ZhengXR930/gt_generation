#!/usr/bin/env python3
"""OpenHands CLI entry point with an issue-only candidate-bootstrap monitor."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import openhands.core.main as oh_main
from openhands.core.config import parse_arguments, setup_config_from_args
from openhands.core.setup import generate_sid
from openhands.events import EventSource, EventStreamSubscriber
from openhands.events.action import MessageAction, NullAction
from openhands.events.serialization.event import event_to_dict
from openhands.io import read_task

from experiments.runtime_hypothesis_feedback.state_monitor import (
    CandidateBootstrapMachine,
)


def main() -> None:
    args = parse_arguments()
    config = setup_config_from_args(args)
    task_str = read_task(args, config.cli_multiline_input)
    initial_user_action = NullAction()
    if config.replay_trajectory_path:
        if task_str:
            raise ValueError("User task is unsupported with trajectory replay")
    else:
        if not task_str:
            raise ValueError("No task provided")
        initial_user_action = MessageAction(content=task_str)

    sid = generate_sid(config, args.name)
    skeleton_path = Path(os.environ["HYPOTHESIS_MONITOR_SKELETON"])
    log_path = Path(os.environ["HYPOTHESIS_MONITOR_LOG"])
    skeleton = json.loads(skeleton_path.read_text(encoding="utf-8"))
    api_key = os.environ.get("HYPOTHESIS_MONITOR_API_KEY") or os.environ.get(
        "DEEPSEEK_API_KEY", ""
    )
    if not api_key:
        raise RuntimeError("No API key configured for hypothesis monitor")

    original_create_controller = oh_main.create_controller

    def monitored_create_controller(agent, runtime, app_config, replay_events=None):
        controller, initial_state = original_create_controller(
            agent, runtime, app_config, replay_events=replay_events
        )
        event_stream = runtime.event_stream
        machine = CandidateBootstrapMachine(
            skeleton=skeleton,
            log_path=log_path,
            api_key=api_key,
            model=os.getenv("HYPOTHESIS_MONITOR_MODEL", "deepseek-chat"),
            api_url=os.getenv(
                "HYPOTHESIS_MONITOR_API_URL",
                "https://api.deepseek.com/chat/completions",
            ),
            check_interval=int(os.getenv("HYPOTHESIS_MONITOR_CHECK_INTERVAL", "4")),
            orientation_ceiling=int(
                os.getenv("HYPOTHESIS_MONITOR_ORIENTATION_CEILING", "12")
            ),
            repeat_interval=int(os.getenv("HYPOTHESIS_MONITOR_REPEAT_INTERVAL", "6")),
            inject_message=lambda content: event_stream.add_event(
                MessageAction(content=content), EventSource.USER
            ),
        )
        event_stream.subscribe(
            EventStreamSubscriber.TEST,
            lambda event: machine.observe(event_to_dict(event)),
            f"candidate-bootstrap-{sid}",
        )
        return controller, initial_state

    oh_main.create_controller = monitored_create_controller
    asyncio.run(
        oh_main.run_controller(
            config=config,
            initial_user_action=initial_user_action,
            sid=sid,
            fake_user_response_fn=(
                None if args.no_auto_continue else oh_main.auto_continue_response
            ),
        )
    )


if __name__ == "__main__":
    main()
