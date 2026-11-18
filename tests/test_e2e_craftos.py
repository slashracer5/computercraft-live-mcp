import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("selftest_cc.py")


def run_script(timeout):
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def failure_output(output):
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    lines = (output or "").splitlines()
    failures = [line for line in lines if line.startswith("FAIL ")]
    return "\n".join([*failures, "Output tail:", *lines[-80:]])


def test_missing_emulator_skips_without_temp_files(monkeypatch, tmp_path):
    missing = tmp_path / "missing-craftos"
    monkeypatch.setenv("CRAFTOS_PC", str(missing))
    for variable in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(variable, str(tmp_path))
    result = run_script(timeout=30)
    assert result.returncode == 77, result.stdout + result.stderr
    assert len(result.stdout.splitlines()) == 1
    assert result.stdout.startswith("SKIP: ")
    assert "CRAFTOS_PC" in result.stdout
    assert "missing-craftos" in result.stdout
    assert not result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.e2e
def test_craftos_end_to_end():
    try:
        result = run_script(timeout=900)
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"CraftOS-PC test exceeded 900 seconds\n{failure_output(error.stdout)}", pytrace=False)
    output = result.stdout + "\n" + result.stderr
    if result.returncode == 77:
        reason = next((line for line in output.splitlines() if line.startswith("SKIP: ")), None)
        assert reason is not None, output
        pytest.skip(reason)
    if result.returncode != 0:
        pytest.fail(f"CraftOS-PC test exited {result.returncode}\n{failure_output(output)}", pytrace=False)
