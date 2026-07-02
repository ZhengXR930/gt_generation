from __future__ import annotations

import argparse
import json
from pathlib import Path

from external_interpreter.core import InterpreterConfig, run_interpreter


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the external interpreter over an OpenHands trajectory."
    )
    parser.add_argument("--trajectory", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--gt", type=Path)
    parser.add_argument("--debug-command", default="")
    parser.add_argument("--sanitizer-command", default="")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    state = run_interpreter(
        InterpreterConfig(
            trajectory=args.trajectory,
            out_dir=args.out_dir,
            gt=args.gt,
            debug_command=args.debug_command,
            sanitizer_command=args.sanitizer_command,
            timeout=args.timeout,
        )
    )
    print(json.dumps(state, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
