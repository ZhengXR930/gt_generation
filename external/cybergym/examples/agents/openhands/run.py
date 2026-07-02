import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import docker
import docker.errors
import tomli_w
from simple_parsing import ArgumentGenerationMode, ArgumentParser, flag

from cybergym.task.gen_task import generate_task
from cybergym.task.types import TaskConfig, TaskDifficulty
from cybergym.utils import save_json

ENVS = [
    "PYTHONPATH",
    "DOCKER_HOST",
    "DOCKER_CONFIG",
    "OPENHANDS_RUNTIME_READY_TIMEOUT",
    "OPENHANDS_ENABLE_REASONING_RECORDER",
    "OPENHANDS_REASONING_RECORDER_POLICY",
    "OPENHANDS_REASONING_RECORDER_INTERVAL",
    "OPENHANDS_REASONING_RECORDER_REQUIRE_TRACE_COMPLETE",
    "OPENHANDS_REASONING_OBSERVER_MODEL",
    "OPENHANDS_REASONING_OBSERVER_CONFIG",
    "OPENHANDS_REASONING_OBSERVER_DECISIONS_PATH",
    "OPENHANDS_REASONING_OBSERVER_STATE_PATH",
    "OPENHANDS_REASONING_OBSERVER_MEMORY_PATH",
    "OPENHANDS_HARNESS_MODE",
    "OPENHANDS_HARNESS_FSM",
    "OPENHANDS_TASK_WORKSPACE",
    "OPENHANDS_ENHANCEMENT_STAGE_CONTROLLER",
    "RECORDER_EVENTS_PATH",
    "RECORDER_STATE_PATH",
    "OPENHANDS_ENFORCE_CONSTRUCTION_LOOP",
    "CYBERGYM_ENABLE_CANDIDATE_SYNTHESIS_MCP",
    "CANDIDATE_SYNTHESIS_WORKSPACE",
    "CANDIDATE_SYNTHESIS_REACHABILITY_DEBUG_COMMAND",
    "CANDIDATE_SYNTHESIS_REACHABILITY_COVERAGE_COMMAND",
    "CANDIDATE_SYNTHESIS_REACHABILITY_SANITIZER_COMMAND",
    "CANDIDATE_SYNTHESIS_SUBMIT_SERVER",
]
API_KEY_ENVS = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "LLM_API_KEY"]
OPENAI_PREFIXES = ["gpt-", "o3", "o4"]
ANTHROPIC_PREFIXES = ["claude-"]
DEEPSEEK_PREFIXES = ["deepseek", "deepseek/"]


SCRIPT_DIR = Path(__file__).parent.absolute()
PROJECT_ROOT = SCRIPT_DIR.parents[4]

# Setup logger
logger = logging.getLogger(__name__)


class OpenHandsError(Exception):
    """Base class for OpenHands errors"""

    pass


class OpenHandsTimeoutError(OpenHandsError):
    """Exception raised when OpenHands times out"""

    pass


class OpenHandsValidationError(OpenHandsError):
    """Exception raised when OpenHands validation fails"""

    pass


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass
class LLMArgs:
    model: str
    """Model to use for generation"""

    api_key: str | None = None
    """API key for the model. If None, get from environment."""

    base_url: str = ""
    """Base URL for the model. If None, use the default URL."""

    native_tool_calling: bool | None = None
    """If None, use the default value. If True, use native tool calling."""

    top_p: float = 1.0
    """Top-p sampling value. Default is 1.0."""

    temperature: float = 0.0
    """Temperature value for sampling. Default is 0.0."""

    max_output_tokens: int = 2048
    """Maximum number of output tokens. Default is 2048."""

    seed: int | None = None
    """Random seed for llm. If None, do not set the seed."""


@dataclass
class OpenhandsArgs:
    log_dir: Path
    """Directory to save the logs"""

    tmp_dir: Path
    """Directory to save the temporary files"""

    llm: LLMArgs
    """LLM arguments"""

    max_iter: int = 10
    """Maximum number of iterations to run the agent"""

    repo: Path = SCRIPT_DIR / "openhands-repo"
    """Path to the repo"""

    silent: bool = False
    """If true, suppresses the output of the OpenHands agent"""

    remove_tmp: bool = True
    """If true, remove the tmp directory after running the agent"""

    timeout: int = 1200
    """Timeout for the OpenHands agent in seconds. Default is 20 minutes."""

    debug: bool = flag(default=False)
    """If true, enable debug mode for the OpenHands agent"""


@dataclass
class TaskArgs:
    task_id: str
    """ID of the task to generate"""

    data_dir: Path
    """Directory containing the data files"""

    server: str
    """Server address for the task"""

    difficulty: TaskDifficulty = TaskDifficulty.level1
    """Difficulty level of the task"""


def validate_output(log_dir: Path):
    traj_json = log_dir / "trajectory"
    if not traj_json.exists():
        logger.warning(f"Trajectory file not found: {traj_json}")
        return False
    return True


def model_map(model: str):
    if model.endswith("/thinking"):
        model = model[: -len("/thinking")]

    if model.startswith("claude-"):
        return model
    elif len(model.split("/")) >= 2:
        return model
    return f"openai/{model}"


def get_api_key(model: str):
    if any(model.startswith(prefix) for prefix in OPENAI_PREFIXES):
        env_var = "OPENAI_API_KEY"
    elif any(model.startswith(prefix) for prefix in ANTHROPIC_PREFIXES):
        env_var = "ANTHROPIC_API_KEY"
    elif any(model.startswith(prefix) for prefix in DEEPSEEK_PREFIXES):
        env_var = "DEEPSEEK_API_KEY"
    else:
        env_var = "LLM_API_KEY"
    api_key = os.getenv(env_var) or os.getenv("LLM_API_KEY")

    if api_key is None:
        api_key = "EMPTY"
    return api_key


def get_prompt_file(model: str):
    # if "o4-mini" in model or "o3-" in model:
    #     return "prompt.o4-mini.txt"
    return "prompt.txt"


def support_native_tool_calling(model: str):
    if "o4-mini" in model:
        return False
    return None


def _cleanup_docker_container(log_dir: Path):
    if os.getenv("OPENHANDS_KEEP_RUNTIME_CONTAINER", "").lower() in {"1", "true", "yes", "on"}:
        logger.info("Keeping OpenHands runtime container for debugging.")
        return

    # try to read the container name from the log dir
    log_files = list(log_dir.glob("*.log"))
    if not log_files:
        logger.warning(f"Log files not found in: {log_dir / 'logs'}")
        return
    # "runtime d1a7102c-cf4e-46df-9483-dbbeb753585d-588e94af345e82b0"
    pat = re.compile(r"runtime ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-[0-9a-f]{16})")
    with open(log_files[0]) as f:
        for line in f:
            match = pat.search(line)
            if match:
                container_id = match.group(1)
                break
        else:
            logger.warning(f"Container ID not found in: {log_files[0]}")
            return
    # remove the container
    client = docker.from_env()
    try:
        container = client.containers.get(f"openhands-runtime-{container_id}")
        container.remove(force=True)
        logger.info(f"Removed container {container_id}")
    except docker.errors.APIError as e:
        logger.warning(f"Container {container_id}, error: {e}")


def run_openhands(
    config_path: Path,
    prompt_path: Path,
    log_dir: Path,
    max_iter: int,
    timeout: int,
    model: str,
    llm_api_key: str | None = None,
    repo: Path = SCRIPT_DIR / "openhands-repo",
    silent: bool = False,
    debug: bool = False,
    enable_thinking: bool = False,
):
    poetry_path = Path(shutil.which("poetry")).absolute()
    if not poetry_path.exists():
        raise Exception(f"[*] Poetry not found at {poetry_path}")
    cmd = [
        str(poetry_path), "run", "python",
        "-m", "openhands.core.main",
        "--config-file", str(config_path),
        "--file", str(prompt_path),
        "--max-iterations", str(max_iter),
    ]  # fmt: skip

    # Set up environment variables
    env = {}
    for env_var in ENVS:
        if os.getenv(env_var) is not None:
            env[env_var] = os.getenv(env_var)

    env["LLM_API_KEY"] = llm_api_key or get_api_key(model)
    env["LOG_TO_FILE"] = "1"
    env["LOG_DIR"] = str(log_dir)
    if debug:
        env["DEBUG"] = "1"
    env["LOG_ALL_EVENTS"] = "1"
    env["DEBUG_RUNTIME"] = "1"
    if enable_thinking:
        logger.info(f"enable thinking for the model {model}")
        env["CYBERGYM_ENABLE_THINKING"] = "1"
    if model.startswith("vertex_ai/"):
        env["GOOGLE_APPLICATION_CREDENTIALS"] = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        env["VERTEXAI_LOCATION"] = os.getenv("VERTEXAI_LOCATION")

    # Run the command and stream the output
    logger.info(f"Running OpenHands with command: {shlex.join(cmd)}")
    try:
        subprocess.run(  # noqa: S603
            cmd,
            cwd=repo,
            env=env,
            stdout=subprocess.DEVNULL if silent else None,
            stderr=subprocess.DEVNULL if silent else None,
            timeout=timeout,  # Timeout set to 300 seconds (5 minutes)
        )
    except subprocess.TimeoutExpired:
        # TODO: should we retry on timeout?
        logger.error("OpenHands process timed out.")
        raise OpenHandsTimeoutError("OpenHands process timed out.") from None
    except Exception as e:
        logger.error(f"Error running OpenHands: {e}")
    finally:
        _cleanup_docker_container(log_dir)


def run_with_configs(openhands_args: OpenhandsArgs, task_args: TaskArgs):
    openhands_args.tmp_dir.mkdir(parents=True, exist_ok=True)
    openhands_args.log_dir.mkdir(parents=True, exist_ok=True)
    openhands_args.tmp_dir = openhands_args.tmp_dir.absolute()
    openhands_args.log_dir = openhands_args.log_dir.absolute()

    enable_thinking = openhands_args.llm.model.endswith("/thinking")

    agent_id = uuid4().hex
    sub_dir = task_args.task_id.replace(":", "_") + "-" + agent_id
    tmp_input_dir = openhands_args.tmp_dir / sub_dir
    tmp_input_dir.mkdir()
    logger.info(f"Creating temporary input directory: {tmp_input_dir}")

    # 1. prepare the challenge inputs

    # 1.1. copy the challenge template to the input directory
    shutil.copytree(
        SCRIPT_DIR / "template",
        tmp_input_dir / "template",
    )

    # 1.2. generate the task
    task_dir = tmp_input_dir / "workspace"
    task_dir.mkdir()

    task_config = TaskConfig(
        task_id=task_args.task_id,
        out_dir=task_dir,
        data_dir=task_args.data_dir,
        server=task_args.server,
        difficulty=task_args.difficulty,
        agent_id=agent_id,
    )

    task = generate_task(task_config)

    # 2. prepare the log directory
    log_dir = openhands_args.log_dir / sub_dir
    log_dir.mkdir()
    logger.info(f"Creating log directory: {log_dir}")

    # 2.1. save the task info to the log
    save_json(
        {
            "agent": f"openhands:{openhands_args.llm.model}",
            "task": task,
            "agent_args": openhands_args,
            "task_args": task_args,
        },
        log_dir / "args.json",
        indent=2,
    )

    logger.info(f"Saving task info to: {log_dir / 'args.json'}")

    managed_mcp_processes: list[subprocess.Popen] = []
    managed_mcp_servers: list[str] = []
    os.environ["OPENHANDS_TASK_WORKSPACE"] = str(task_dir)
    if _env_flag("OPENHANDS_ENABLE_REASONING_RECORDER", False):
        os.environ["RECORDER_EVENTS_PATH"] = str(log_dir / "reasoning_events.jsonl")
        os.environ["RECORDER_STATE_PATH"] = str(log_dir / "reasoning_state.json")
        if os.getenv("OPENHANDS_REASONING_RECORDER_POLICY", "").lower() == "observer":
            os.environ["OPENHANDS_REASONING_OBSERVER_DECISIONS_PATH"] = str(
                log_dir / "observer_decisions.jsonl"
            )
            os.environ["OPENHANDS_REASONING_OBSERVER_STATE_PATH"] = str(
                log_dir / "observer_state.json"
            )
            os.environ["OPENHANDS_REASONING_OBSERVER_MEMORY_PATH"] = str(
                log_dir / "observer_memory.jsonl"
            )
        proc, server_url = _start_reasoning_recorder_mcp(
            log_dir=log_dir,
            openhands_repo=openhands_args.repo,
        )
        managed_mcp_processes.append(proc)
        managed_mcp_servers.append(server_url)
        logger.info("Started reasoning recorder MCP server: %s", server_url)
    if _env_flag("CYBERGYM_ENABLE_CANDIDATE_SYNTHESIS_MCP", False):
        proc, server_url = _start_candidate_synthesis_mcp(
            workspace=task_dir,
            log_dir=log_dir,
            openhands_repo=openhands_args.repo,
        )
        managed_mcp_processes.append(proc)
        managed_mcp_servers.append(server_url)
        logger.info("Started candidate synthesis MCP server: %s", server_url)

    # 3. prepare the config file
    config_path = tmp_input_dir / "template" / "config.toml"
    with open(config_path) as f:
        config = tomllib.loads(f.read())
    config["core"]["workspace_base"] = str(task_dir)
    config["core"]["cache_dir"] = str(log_dir / "cache")
    config["core"]["file_store_path"] = str(log_dir / "file")
    config["core"]["save_trajectory_path"] = str(log_dir / "trajectory")
    config["llm"]["model"] = model_map(openhands_args.llm.model)
    config["llm"]["top_p"] = openhands_args.llm.top_p
    config["llm"]["temperature"] = openhands_args.llm.temperature
    config["llm"]["base_url"] = openhands_args.llm.base_url
    config["llm"]["max_output_tokens"] = openhands_args.llm.max_output_tokens

    native_tool_calling = openhands_args.llm.native_tool_calling
    if native_tool_calling is not None:
        config["llm"]["native_tool_calling"] = native_tool_calling
    
    if openhands_args.llm.seed is not None:
        config["llm"]["seed"] = openhands_args.llm.seed

    auto_remove_override = os.getenv("OPENHANDS_RUNTIME_AUTO_REMOVE")
    if auto_remove_override is not None:
        config.setdefault("sandbox", {}).setdefault("docker_runtime_kwargs", {})[
            "auto_remove"
        ] = auto_remove_override.lower() in {"1", "true", "yes", "on"}

    runtime_binding_override = os.getenv("OPENHANDS_RUNTIME_BINDING_ADDRESS")
    if runtime_binding_override:
        config.setdefault("sandbox", {})["runtime_binding_address"] = runtime_binding_override

    runtime_image_override = os.getenv("OPENHANDS_RUNTIME_CONTAINER_IMAGE")
    if runtime_image_override:
        config.setdefault("sandbox", {})["runtime_container_image"] = runtime_image_override

    mcp_servers_override = os.getenv("OPENHANDS_MCP_SERVERS")
    mcp_servers = list(managed_mcp_servers)
    if mcp_servers_override:
        mcp_servers.extend([
            item.strip()
            for item in mcp_servers_override.split(",")
            if item.strip()
        ])
    if mcp_servers:
        config.setdefault("mcp", {})["mcp_servers"] = mcp_servers

    with open(config_path, "w") as f:
        f.write(tomli_w.dumps(config))

    # 4. run the openhands agent
    prompt_file = get_prompt_file(openhands_args.llm.model)
    prompt_override = os.getenv("CYBERGYM_OPENHANDS_PROMPT_FILE")
    if prompt_override:
        prompt_override_path = Path(prompt_override).expanduser().resolve()
        if not prompt_override_path.exists():
            raise FileNotFoundError(f"CYBERGYM_OPENHANDS_PROMPT_FILE does not exist: {prompt_override_path}")
        shutil.copy2(prompt_override_path, tmp_input_dir / "template" / prompt_file)
    try:
        run_openhands(
            config_path=config_path,
            prompt_path=tmp_input_dir / "template" / prompt_file,
            log_dir=log_dir / "logs",
            timeout=openhands_args.timeout,
            repo=openhands_args.repo,
            silent=openhands_args.silent,
            max_iter=openhands_args.max_iter,
            model=openhands_args.llm.model,
            llm_api_key=openhands_args.llm.api_key,
            debug=openhands_args.debug,
            enable_thinking=enable_thinking,
        )
    finally:
        for proc in managed_mcp_processes:
            _terminate_process(proc)

    _export_candidate_synthesis_artifacts(task_dir=task_dir, log_dir=log_dir)

    # 5. remove the tmp directory
    if openhands_args.remove_tmp:
        shutil.rmtree(tmp_input_dir, ignore_errors=True)
        logger.info(f"Removing temporary input directory: {tmp_input_dir}")

    # 6. validate the output
    is_valid = validate_output(log_dir)

    return agent_id if is_valid else None


def _export_candidate_synthesis_artifacts(*, task_dir: Path, log_dir: Path) -> None:
    """Preserve minimal candidate/PoC artifacts before CyberGym tmp cleanup."""
    export_dir = log_dir / "candidate_synthesis"
    names = [
        "construction_support_requests.jsonl",
        "candidate_plans.jsonl",
        "candidates.jsonl",
        "candidate_runs.jsonl",
        "candidate_submissions.jsonl",
        "candidates",
        "runs",
        "submissions",
    ]
    copied = False
    for name in names:
        src = task_dir / name
        if not src.exists():
            continue
        dst = export_dir / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        copied = True
    if copied:
        logger.info("Exported candidate synthesis artifacts to: %s", export_dir)


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_candidate_synthesis_mcp(
    *,
    workspace: Path,
    log_dir: Path,
    openhands_repo: Path,
) -> tuple[subprocess.Popen, str]:
    port = int(os.getenv("CANDIDATE_SYNTHESIS_MCP_PORT") or _free_tcp_port())
    python_bin = openhands_repo / ".venv" / "bin" / "python"
    if not python_bin.exists():
        python_bin = Path(sys.executable)
    mcp_log = open(log_dir / "candidate_synthesis_mcp.log", "w")
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{PROJECT_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    cmd = [
        str(python_bin),
        "-m",
        "enhance_mcp_servers.candidate_synthesis_server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--workspace",
        str(workspace),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=PROJECT_ROOT,
        env=env,
        stdout=mcp_log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # FastMCP starts quickly; fail early if imports or binding fail.
    import time

    time.sleep(1.0)
    if proc.poll() is not None:
        raise RuntimeError(
            f"candidate synthesis MCP server exited early; see {log_dir / 'candidate_synthesis_mcp.log'}"
        )
    return proc, f"http://127.0.0.1:{port}/sse"


def _start_reasoning_recorder_mcp(
    *,
    log_dir: Path,
    openhands_repo: Path,
) -> tuple[subprocess.Popen, str]:
    port = int(os.getenv("RECORDER_MCP_PORT") or _free_tcp_port())
    python_bin = openhands_repo / ".venv" / "bin" / "python"
    if not python_bin.exists():
        python_bin = Path(sys.executable)
    mcp_log = open(log_dir / "reasoning_recorder_mcp.log", "w")
    events_path = log_dir / "reasoning_events.jsonl"
    state_path = log_dir / "reasoning_state.json"
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{PROJECT_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    cmd = [
        str(python_bin),
        "-m",
        "mcp_servers.reasoning_recorder_server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--events-path",
        str(events_path),
        "--state-path",
        str(state_path),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=PROJECT_ROOT,
        env=env,
        stdout=mcp_log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    import time

    time.sleep(1.0)
    if proc.poll() is not None:
        raise RuntimeError(
            f"reasoning recorder MCP server exited early; see {log_dir / 'reasoning_recorder_mcp.log'}"
        )
    return proc, f"http://127.0.0.1:{port}/sse"


def _terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def main(raw_args=None):
    parser = ArgumentParser(argument_generation_mode=ArgumentGenerationMode.BOTH)
    parser.add_arguments(OpenhandsArgs, dest="openhands_args")
    parser.add_arguments(TaskArgs, dest="task_args")

    args = parser.parse_args(raw_args)

    run_with_configs(args.openhands_args, args.task_args)


if __name__ == "__main__":
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("[%(levelname)s] %(message)s")
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    main()
