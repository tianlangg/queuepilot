"""Exercise local setup without touching the checkout's actual credentials."""

import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/init_env.py"


def test_setup_preserves_existing_values_and_credentials(tmp_path: Path) -> None:
    script = tmp_path / "scripts/init_env.py"
    script.parent.mkdir()
    shutil.copy(SCRIPT, script)
    env_path = tmp_path / ".env"
    env_path.write_text("QUEUEPILOT_APP_NAME=My API\n", encoding="utf-8")
    first = subprocess.run(
        [sys.executable, str(script)], check=True, capture_output=True, text=True
    )
    files = [tmp_path / name for name in (".env", ".env.compose", ".env.test")]
    initial = [path.read_text() for path in files]
    subprocess.run([sys.executable, str(script)], check=True, capture_output=True)
    assert [path.read_text() for path in files] == initial
    assert dotenv_values(env_path)["QUEUEPILOT_APP_NAME"] == "My API"
    password = dotenv_values(tmp_path / ".env.compose")["POSTGRES_PASSWORD"]
    assert password
    assert password not in first.stdout
    for path in files:
        assert path.stat().st_mode & 0o777 == 0o600
    assert password in initial[0]
    assert password in initial[2]
