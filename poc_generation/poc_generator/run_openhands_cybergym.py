import logging
import os
import re
import shlex
import shutil
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
    "OPENHANDS_HARNESS_MODE",
    "OPENHANDS_CAPTURE_FINE_TRACE",
    "OPENHANDS_FINE_TRACE_OUTPUT",
    "OPENHANDS_TASK_WORKSPACE",
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


def model_map(model: str, *, openai_compatible: bool = False):
    if model.endswith("/thinking"):
        model = model[: -len("/thinking")]

    # Third-party /v1/chat/completions proxies expose models from several
    # families through the OpenAI wire protocol. Force LiteLLM's OpenAI adapter
    # in that case; otherwise a Claude-looking name selects the native
    # Anthropic /v1/messages protocol and never reaches the proxy correctly.
    if openai_compatible:
        return model if model.startswith("openai/") else f"openai/{model}"
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
    session_name: str | None = None,
):
    python_override = os.getenv("OPENHANDS_PYTHON")
    if python_override:
        command_prefix = [python_override]
    elif sys.prefix != sys.base_prefix:
        # Batch runs already execute this driver in the prepared OpenHands
        # environment. Reuse that exact interpreter; an unrelated Poetry
        # executable on PATH may select a fresh, uninstalled environment.
        command_prefix = [sys.executable]
    else:
        poetry = shutil.which("poetry")
        if not poetry:
            raise Exception("[*] Poetry not found")
        command_prefix = [str(Path(poetry).absolute()), "run", "python"]
    cmd = command_prefix + [
        "-m", "openhands.core.main",
        "--config-file", str(config_path),
        "--file", str(prompt_path),
        "--max-iterations", str(max_iter),
    ]  # fmt: skip
    if session_name:
        # Deterministic --name so the resulting sid (derived from
        # session_name + the file_store-persisted jwt_secret) can be
        # regenerated later to restore/resume this exact session.
        cmd += ["--name", session_name]

    # Set up environment variables
    # Inherit the full parent environment (PATH, HOME, DOCKER_HOST, etc.) — the
    # upstream cybergym script builds env from an empty dict, which breaks anything
    # that shells out (e.g. docker-py's credential-store lookup needs PATH).
    env = dict(os.environ)
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


def session_name_for_task(task_id: str) -> str:
    """Build a stable runtime name with an optionally isolated prefix."""
    prefix = os.getenv("OPENHANDS_SESSION_PREFIX", "gtpoc").strip() or "gtpoc"
    return f"{prefix}-{task_id.replace(':', '_')}"


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
            "session_name": session_name_for_task(task_args.task_id),
            "file_store_path": str(log_dir / "file"),
        },
        log_dir / "args.json",
        indent=2,
    )

    logger.info(f"Saving task info to: {log_dir / 'args.json'}")

    os.environ["OPENHANDS_TASK_WORKSPACE"] = str(task_dir)
    os.environ["OPENHANDS_POC_SUBMISSION_MARKER"] = str(
        task_dir / ".poc_submission_recorded"
    )
    os.environ["OPENHANDS_LATEST_SUBMISSION_TRACE"] = str(
        task_dir / ".latest_candidate_trace.json"
    )

    # 3. prepare the config file
    config_path = tmp_input_dir / "template" / "config.toml"
    with open(config_path) as f:
        config = tomllib.loads(f.read())
    config["core"]["workspace_base"] = str(task_dir)
    config["core"]["cache_dir"] = str(log_dir / "cache")
    config["core"]["file_store_path"] = str(log_dir / "file")
    config["core"]["save_trajectory_path"] = str(log_dir / "trajectory")
    config["llm"]["model"] = model_map(
        openhands_args.llm.model,
        openai_compatible=bool(openhands_args.llm.base_url),
    )
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
    session_name = session_name_for_task(task_args.task_id)
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
        session_name=session_name,
    )

    # 5. remove the tmp directory
    if openhands_args.remove_tmp:
        shutil.rmtree(tmp_input_dir, ignore_errors=True)
        logger.info(f"Removing temporary input directory: {tmp_input_dir}")

    # 6. validate the output
    is_valid = validate_output(log_dir)

    return agent_id if is_valid else None

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
