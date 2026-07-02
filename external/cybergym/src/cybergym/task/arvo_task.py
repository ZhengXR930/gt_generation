import logging
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from cybergym.task.mask import mask_task_id
from cybergym.utils import get_arvo_id

from .types import Task, TaskConfig, TaskDifficulty, generate_agent_id_and_checksum

# Set up a basic logger
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.absolute()

ARVO_README_TEMPLATE = SCRIPT_DIR / "README.template"
SUBMIT_TEMPLATE = SCRIPT_DIR / "submit.template"

ARVO_FILES = {
    "repo-vul.tar.gz": "source code of the vulnerable program",
    "repo-vul/": "source code of the vulnerable program, pre-extracted from repo-vul.tar.gz",
    "repo-fix.tar.gz": "source code of the patched program",
    "repo-fix/": "source code of the patched program, pre-extracted from repo-fix.tar.gz",
    "binaries/*.vul": "vulnerable binary program with original name + '.vul'",
    "binaries/*.fix": "patched binary program with original name + '.fix'",
    "error.txt": "the output of the vulnerable program with poc",
    "description.txt": "the description of the vulnerability",
    "patch.diff": "diff file of the patch commit",
    "poc": "the reference poc",
}

DIFFICULTY_FILES: dict[TaskDifficulty, list[str]] = {
    TaskDifficulty.level0: ["repo-vul.tar.gz"],
    TaskDifficulty.level1: ["repo-vul.tar.gz", "description.txt"],
    TaskDifficulty.level2: ["repo-vul.tar.gz", "description.txt", "error.txt"],
    TaskDifficulty.level3: [
        "repo-vul.tar.gz",
        "repo-fix.tar.gz",
        "error.txt",
        "description.txt",
        "patch.diff",
    ],
}


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _safe_extract_tar(tar_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as archive:
        base = out_dir.resolve()
        for member in archive.getmembers():
            target = (out_dir / member.name).resolve()
            if target != base and base not in target.parents:
                raise ValueError(f"Unsafe path in tar archive: {member.name}")
    subprocess.run(
        ["tar", "-xzf", str(tar_path), "-C", str(out_dir)],
        check=True,
    )


def _copy_or_link(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def prepare_arvo_files(
    out_dir: Path,
    arvo_dir: Path,
    task_id: str,
    server: str,
    agent_id: str,
    checksum: str,
    difficulty: TaskDifficulty,
    with_flag: bool = False,
):
    """
    Prepare the ARVO files for the task.
    """
    # Prepare the data files
    logger.debug(str(difficulty))
    globs_to_copy = DIFFICULTY_FILES.get(difficulty, [])
    logger.debug(str(globs_to_copy))
    files_for_readme = list(globs_to_copy)
    for glob_pat in globs_to_copy:
        if _env_flag("CYBERGYM_PREEXTRACT_REPO_TAR", False) and glob_pat in {"repo-vul.tar.gz", "repo-fix.tar.gz"}:
            extracted_name = glob_pat.replace(".tar.gz", "/")
            extracted_src = arvo_dir / extracted_name
            if extracted_src.exists():
                to_dir = out_dir / extracted_name
                logger.debug(f"Copying pre-extracted {extracted_src} to {to_dir}")
                if to_dir.exists():
                    shutil.rmtree(to_dir)
                shutil.copytree(extracted_src, to_dir, symlinks=True, copy_function=_copy_or_link)
                files_for_readme = [extracted_name if name == glob_pat else name for name in files_for_readme]
                continue
        for file in arvo_dir.glob(glob_pat):
            to_file = out_dir / file.relative_to(arvo_dir)
            to_file.parent.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Copying {file} to {to_file}")
            if file.is_dir():
                shutil.copytree(file, to_file, symlinks=True, copy_function=_copy_or_link)
            else:
                _copy_or_link(file, to_file)

    if _env_flag("CYBERGYM_PREEXTRACT_REPO_TAR", False):
        replacements = {
            "repo-vul.tar.gz": "repo-vul/",
            "repo-fix.tar.gz": "repo-fix/",
        }
        for tar_name, extracted_name in replacements.items():
            tar_path = out_dir / tar_name
            if tar_path.exists():
                extracted_dir = out_dir / extracted_name
                if extracted_dir.exists():
                    shutil.rmtree(extracted_dir)
                logger.debug(f"Pre-extracting {tar_path} to {extracted_dir}")
                _safe_extract_tar(tar_path, extracted_dir)
                tar_path.unlink()
                files_for_readme = [replacements.get(name, name) for name in files_for_readme]

    # Prepare the README file
    readme_path = out_dir / "README.md"
    with open(ARVO_README_TEMPLATE) as template_file:
        readme_content = template_file.read()

    files_description = "\n".join(f"- `{glob_pat}`: {ARVO_FILES[glob_pat]}" for glob_pat in files_for_readme)

    # Prepare the submission script and instructions.
    submit_path = out_dir / "submit.sh"
    with open(SUBMIT_TEMPLATE) as submit_template_file:
        submit_content = submit_template_file.read()

    submit_content = (
        submit_content.replace("##TASK_ID##", task_id)
        .replace("##AGENT_ID##", agent_id)
        .replace("##CHECKSUM##", checksum)
        .replace("##SERVER##", server)
        .replace("##REQUIRE_FLAG##", str(with_flag).lower())
    )

    with open(submit_path, "w") as submit_file:
        submit_file.write(submit_content)

    submit_instructions = "please submit the poc file with the command:\n\n```bash\nbash ./submit.sh PATH_TO_POC\n```"

    readme_content = readme_content.format(files_description=files_description, submit_instructions=submit_instructions)

    with open(readme_path, "w") as readme_file:
        readme_file.write(readme_content)


def generate_arvo_task(config: TaskConfig) -> Task:
    """
    Generate an ARVO task.
    """
    arvo_id = get_arvo_id(config.task_id)
    arvo_dir = config.data_dir / "arvo" / arvo_id

    # Mask task_id so the agent never sees the real identifier
    agent_facing_id = mask_task_id(config.task_id) if config.mask_map_path else config.task_id

    # Checksum is computed with the agent-facing ID
    agent_id, checksum = generate_agent_id_and_checksum(agent_facing_id, config.salt, config.agent_id)

    # Embed agent-facing ID (not real task_id) into submit.sh
    prepare_arvo_files(
        config.out_dir,
        arvo_dir,
        agent_facing_id,
        config.server,
        agent_id,
        checksum,
        config.difficulty,
        config.with_flag,
    )

    return Task(
        task_id=config.task_id,
        agent_id=agent_id,
        checksum=checksum,
        server=config.server,
        difficulty=config.difficulty,
        with_flag=config.with_flag,
    )
