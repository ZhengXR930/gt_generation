#!/usr/bin/env python3
"""OpenHands entry point controlled by a binary trajectory observer."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import openhands.core.main as oh_main
from openhands.core.config import parse_arguments, setup_config_from_args
from openhands.core.setup import generate_sid
from openhands.events import EventSource
from openhands.events.action import (
    AgentFinishAction,
    MessageAction,
    NullAction,
)
from openhands.events.serialization.event import event_to_dict
from openhands.io import read_task

from experiments.runtime_hypothesis_feedback.trajectory_supervisor import (
    EpisodeState,
    TrajectorySubmissionSupervisor,
    render_visible_trajectory,
    sync_submission_outcomes,
)
from experiments.runtime_hypothesis_feedback.openhands_submit_candidate import (
    install_closed_network_runtime_route,
    install_submit_candidate_tool,
)
def main() -> None:
    install_closed_network_runtime_route()
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
    skeleton = json.loads(
        Path(os.environ["HYPOTHESIS_MONITOR_SKELETON"]).read_text(encoding="utf-8")
    )
    reward_spec_path = os.environ.get("HYPOTHESIS_REWARD_SPEC", "")
    reward_spec = (
        json.loads(Path(reward_spec_path).read_text(encoding="utf-8"))
        if reward_spec_path
        else None
    )
    log_path = Path(os.environ["HYPOTHESIS_MONITOR_LOG"])
    api_key = os.environ.get("HYPOTHESIS_MONITOR_API_KEY") or os.environ.get(
        "DEEPSEEK_API_KEY", ""
    )
    if not api_key:
        raise RuntimeError("No API key configured for trajectory observer")

    original_create_controller = oh_main.create_controller

    def supervised_create_controller(agent, runtime, app_config, replay_events=None):
        controller, initial_state = original_create_controller(
            agent, runtime, app_config, replay_events=replay_events
        )
        event_stream = runtime.event_stream
        install_submit_candidate_tool(agent, event_stream)
        supervisor = TrajectorySubmissionSupervisor(
            skeleton=skeleton,
            reward_spec=reward_spec,
            log_path=log_path,
            api_key=api_key,
            model=os.getenv("HYPOTHESIS_MONITOR_MODEL", "deepseek-chat"),
            api_url=os.getenv(
                "HYPOTHESIS_MONITOR_API_URL",
                "https://api.deepseek.com/chat/completions",
            ),
            inject_message=lambda content: event_stream.add_event(
                MessageAction(content=content), EventSource.USER
            ),
        )

        original_step = agent.step

        def trajectory_supervised_step(state):
            action = original_step(state)
            if action is None:
                return action
            trajectory = render_visible_trajectory(state.history)
            sync_submission_outcomes(supervisor, state.history)
            if isinstance(action, AgentFinishAction):
                allow = supervisor.before_finish(
                    fine_trace_finalization=bool(
                        state.extra_data.get("fine_trace_finalization")
                    ),
                )
                return action if allow else NullAction()
            # A model may express an intended tool call as a plain message
            # (for example malformed provider-specific tool markup).  Those
            # messages are part of the visible trajectory and are exactly
            # where a trajectory-level submit decision must still be able to
            # intervene.  Other non-runnable control actions remain outside
            # the observer boundary.
            if not action.runnable and not isinstance(action, MessageAction):
                return action

            action._source = EventSource.AGENT
            proposed_event = event_to_dict(action)
            allow = supervisor.before_action(proposed_event, trajectory)
            if allow:
                return action
            if supervisor.state == EpisodeState.SUBMISSION_REQUIRED:
                # before_action already injected SUBMIT_MESSAGE as a genuine
                # user observation. Do not fabricate an agent CmdRunAction:
                # native function-calling providers require tool-call metadata
                # on every agent tool action, and a synthetic shell command has
                # none. NullAction discards the blocked proposal while leaving
                # the platform-neutral observation in the trajectory.
                return NullAction()
            return NullAction()

        agent.step = trajectory_supervised_step
        return controller, initial_state

    oh_main.create_controller = supervised_create_controller
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
