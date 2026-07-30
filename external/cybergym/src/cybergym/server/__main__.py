import argparse
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
import time
from typing import Annotated
from uuid import uuid4

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, Security, UploadFile, status
from fastapi.security import APIKeyHeader
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from cybergym.server.pocdb import (
    create_submission_attempt,
    get_poc_by_hash,
    init_engine,
    update_submission_attempt,
)
from cybergym.server.rate_limiter import RateLimiter
from cybergym.server.server_utils import _post_process_result, run_poc_id, submit_poc
from cybergym.server.types import Payload, PocQuery, VerifyPocs, server_conf
from cybergym.task.mask import load_mask_map
from cybergym.task.types import verify_task

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def make_log_config(log_file: str) -> dict:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": LOG_FORMAT,
                "datefmt": LOG_DATE_FORMAT,
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "default",
                "filename": log_file,
                "maxBytes": 10 * 1024 * 1024,  # 10 MB
                "backupCount": 5,
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            "cybergym.server": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        },
    }


logger = logging.getLogger("cybergym.server")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

engine: Engine = None
rate_limiter: RateLimiter = None


def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, rate_limiter
    logger.info("Starting server: db_path=%s, log_dir=%s", server_conf.db_path, server_conf.log_dir)
    engine = init_engine(server_conf.db_path)
    rate_limiter = RateLimiter(
        max_requests=server_conf.rate_limit_max_requests, window_seconds=server_conf.rate_limit_window_seconds
    )
    logger.info("Server ready")

    yield

    logger.info("Shutting down server")
    if engine:
        engine.dispose()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s -> %d (%.1fms)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


api_key_header = APIKeyHeader(name=server_conf.api_key_name, auto_error=False)


def get_api_key(api_key: str = Security(api_key_header)):
    if api_key == server_conf.api_key:
        return api_key
    else:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def try_read_file(file: UploadFile, max_size_mb: int) -> bytes:
    """Helper function to check file size."""
    max_size_bytes = max_size_mb * 1024 * 1024
    content = file.file.read(max_size_bytes + 1)
    if len(content) > max_size_bytes:
        raise HTTPException(status_code=413, detail=f"File too large. Maximum size allowed: {max_size_mb}MB")
    return content


def validate_candidate_trace(content: bytes | None) -> str | None:
    if content is None:
        return "candidate trace file is required"
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "candidate trace must be UTF-8 bare JSON"
    required = ("step", "file", "function", "line", "var", "code", "note")
    if not isinstance(value, list) or not value:
        return "candidate trace must be a non-empty JSON array"
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict):
            return f"candidate trace step {index} must be an object"
        missing = [field for field in required if field not in item]
        if missing:
            return f"candidate trace step {index} missing: {', '.join(missing)}"
        if item["step"] != index:
            return f"candidate trace step {index} must have step={index}"
        if "depends_on" in item:
            return (
                f"candidate trace step {index} must not contain depends_on; "
                "represent propagation with consecutive trace order"
            )
        for field in ("file", "function", "var", "code", "note"):
            if not str(item[field] or "").strip():
                return f"candidate trace step {index} has empty {field}"
        if item["line"] is not None and not isinstance(item["line"], int):
            return f"candidate trace step {index} line must be integer or null"
    return None


public_router = APIRouter()
private_router = APIRouter(dependencies=[Depends(get_api_key)])


@public_router.post("/submit-vul")
def submit_vul(
    db: SessionDep,
    metadata: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    trace: Annotated[UploadFile | None, File()] = None,
):
    # Read and validate file size
    try:
        file_content = try_read_file(file, server_conf.max_file_size_mb)
    except HTTPException:
        raise
    except Exception:
        logger.warning("Failed to read uploaded file")
        raise HTTPException(status_code=400, detail="Error reading file") from None

    try:
        payload = Payload.model_validate_json(metadata)
    except Exception:
        logger.warning("Invalid metadata in submit-vul request")
        raise HTTPException(status_code=400, detail="Invalid metadata format") from None

    logger.info("submit-vul: agent=%s task=%s file_size=%d", payload.agent_id, payload.task_id, len(file_content))

    if not verify_task(payload.task_id, payload.agent_id, payload.checksum, salt=server_conf.salt):
        raise HTTPException(status_code=400, detail="Invalid checksum")

    rate_limiter.check(payload.agent_id)

    trace_content = try_read_file(trace, 2) if trace is not None else None
    trace_error = validate_candidate_trace(trace_content)
    attempt_id = uuid4().hex
    attempt_dir = server_conf.log_dir / "submissions" / payload.agent_id / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=False)
    (attempt_dir / "poc.bin").write_bytes(file_content)
    if trace_content is not None:
        (attempt_dir / "candidate_trace.response.txt").write_bytes(trace_content)
        if trace_error is None:
            parsed_trace = json.loads(trace_content.decode("utf-8"))
            (attempt_dir / "candidate_trace.json").write_text(
                json.dumps(parsed_trace, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
    request_record = {
        "attempt_id": attempt_id,
        "agent_id": payload.agent_id,
        "task_id": payload.task_id,
        "poc_hash": hashlib.sha256(file_content).hexdigest(),
        "poc_length": len(file_content),
        "trace_valid": trace_error is None,
        "trace_error": trace_error,
    }
    (attempt_dir / "request.json").write_text(
        json.dumps(request_record, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    attempt = create_submission_attempt(db, **request_record)

    payload.data = file_content
    binary_only_mode = bool(server_conf.binary_dir)
    try:
        res = submit_poc(
            db,
            payload,
            mode="vul",
            log_dir=server_conf.log_dir,
            salt=server_conf.salt,
            binary_only_mode=binary_only_mode,
        )
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        error_record = {
            **request_record,
            "exit_code": None,
            "error": error_text,
        }
        (attempt_dir / "runtime_output.txt").write_text(
            error_text + "\n", encoding="utf-8"
        )
        (attempt_dir / "result.json").write_text(
            json.dumps(error_record, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        raise
    target_exit_code = res.get("exit_code")
    runtime_output = str(res.get("output") or "")
    (attempt_dir / "runtime_output.txt").write_text(
        runtime_output, encoding="utf-8"
    )
    result_record = {
        **request_record,
        "poc_id": res.get("poc_id"),
        "exit_code": target_exit_code,
    }
    (attempt_dir / "result.json").write_text(
        json.dumps(result_record, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    update_submission_attempt(
        db,
        attempt,
        poc_id=res.get("poc_id"),
        vul_exit_code=target_exit_code,
    )
    res = _post_process_result(res, payload.require_flag)
    res["attempt_id"] = attempt_id
    res["trace_valid"] = trace_error is None
    res["trace_error"] = trace_error
    logger.info(
        "submit-vul done: agent=%s task=%s exit_code=%s",
        payload.agent_id,
        payload.task_id,
        target_exit_code,
    )
    return res


@private_router.post("/submit-fix")
def submit_fix(db: SessionDep, metadata: Annotated[str, Form()], file: Annotated[UploadFile, File()]):
    # Read and validate file size
    try:
        file_content = try_read_file(file, server_conf.max_file_size_mb)
    except HTTPException:
        raise
    except Exception:
        logger.warning("Failed to read uploaded file")
        raise HTTPException(status_code=400, detail="Error reading file") from None

    try:
        payload = Payload.model_validate_json(metadata)
    except Exception:
        logger.warning("Invalid metadata in submit-fix request")
        raise HTTPException(status_code=400, detail="Invalid metadata format") from None

    logger.info("submit-fix: agent=%s task=%s file_size=%d", payload.agent_id, payload.task_id, len(file_content))

    payload.data = file_content
    binary_only_mode = bool(server_conf.binary_dir)
    res = submit_poc(
        db, payload, mode="fix", log_dir=server_conf.log_dir, salt=server_conf.salt, binary_only_mode=binary_only_mode
    )
    res = _post_process_result(res, payload.require_flag)
    logger.info("submit-fix done: agent=%s task=%s exit_code=%s", payload.agent_id, payload.task_id, res["exit_code"])
    return res


@private_router.post("/query-poc")
def query_db(db: SessionDep, query: PocQuery):
    logger.info("query-poc: agent=%s task=%s", query.agent_id, query.task_id)
    records = get_poc_by_hash(db, query.agent_id, query.task_id)
    if not records:
        raise HTTPException(status_code=404, detail="Record not found")
    return [record.to_dict() for record in records]


@private_router.post("/verify-agent-pocs")
def verify_all_pocs_for_agent_id(db: SessionDep, query: VerifyPocs):
    """
    Verify all PoCs for a given agent_id.
    """
    logger.info("verify-agent-pocs: agent=%s", query.agent_id)
    records = get_poc_by_hash(db, query.agent_id)
    if not records:
        raise HTTPException(status_code=404, detail="No records found for this agent_id")

    for record in records:
        if record.vul_exit_code in [0, 300]:
            continue  # Skip PoCs that did not trigger a crash
        logger.info("Re-verifying poc_id=%s task=%s", record.poc_id, record.task_id)
        run_poc_id(db, server_conf.log_dir, record.poc_id, binary_only_mode=bool(server_conf.binary_dir))
        time.sleep(0.5)  # Small delay to avoid overwhelming the docker

    logger.info("verify-agent-pocs done: agent=%s count=%d", query.agent_id, len(records))
    return {
        "message": f"All {len(records)} PoCs for this agent_id have been verified",
        "poc_ids": [record.poc_id for record in records],
    }


app.include_router(public_router)
app.include_router(private_router)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CyberGym Server")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to run the server on")
    parser.add_argument("--port", type=int, default=8666, help="Port to run the server on")
    parser.add_argument("--salt", type=str, default=server_conf.salt, help="Salt for checksum")
    parser.add_argument(
        "--mask_map_path", type=Path, default=server_conf.mask_map_path, help="Path to task ID mask mapping JSON file"
    )
    parser.add_argument("--log_dir", type=Path, default=server_conf.log_dir, help="Directory to store logs")
    parser.add_argument("--db_path", type=Path, default=server_conf.db_path, help="Path to SQLite DB")
    parser.add_argument(
        "--binary_dir", type=Path, default=server_conf.binary_dir, help="Directory to store target binaries"
    )
    parser.add_argument(
        "--max_file_size_mb", type=int, default=server_conf.max_file_size_mb, help="Maximum file size for uploads in MB"
    )
    parser.add_argument(
        "--rate_limit_max_requests",
        type=int,
        default=server_conf.rate_limit_max_requests,
        help="Max requests per agent per window",
    )
    parser.add_argument(
        "--rate_limit_window_seconds",
        type=int,
        default=server_conf.rate_limit_window_seconds,
        help="Rate limit window in seconds",
    )

    args = parser.parse_args()

    server_conf.salt = args.salt
    server_conf.mask_map_path = args.mask_map_path
    server_conf.log_dir = args.log_dir
    server_conf.log_dir.mkdir(parents=True, exist_ok=True)
    server_conf.db_path = Path(args.db_path)
    server_conf.binary_dir = args.binary_dir
    server_conf.max_file_size_mb = args.max_file_size_mb
    server_conf.rate_limit_max_requests = args.rate_limit_max_requests
    server_conf.rate_limit_window_seconds = args.rate_limit_window_seconds

    if server_conf.mask_map_path:
        load_mask_map(server_conf.mask_map_path)

    uvicorn.run(
        app, host=args.host, port=args.port, log_config=make_log_config(str(server_conf.log_dir / "server.log"))
    )
