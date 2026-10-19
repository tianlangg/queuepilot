"""Load explicit isolated-service config and require all integration tests."""

import os
import subprocess
from pathlib import Path

from dotenv import dotenv_values


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    for key, value in dotenv_values(root / ".env.test").items():
        if value is not None:
            env.setdefault(key, value)
    env["QUEUEPILOT_ALLOW_TEST_DATABASE"] = "1"
    result = subprocess.run(
        ["uv", "run", "--locked", "pytest", "--require-integration"],
        cwd=root,
        env=env,
        check=False,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
