"""Optional execution of model-generated Python reference solutions against their tests.

DISABLED BY DEFAULT (CODE_VERIFICATION_ENABLED=false). Even when enabled this is
only a best-effort isolation: a separate interpreter in isolated mode (-I),
an empty temporary working directory, a stripped environment, a wall-clock
timeout and an output cap. It is not a security sandbox; for production use run
the worker in a locked-down container (no network, read-only FS, low limits).
When disabled, coding questions are flagged for teacher review instead.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT = 4000


@dataclass
class CodeRunResult:
    passed: bool
    output: str
    timed_out: bool = False


async def run_python_tests(solution: str, tests: str, timeout: float) -> CodeRunResult:
    program = f"{solution}\n\n# --- tests ---\n{tests}\nprint('ALL_TESTS_PASSED')\n"
    with tempfile.TemporaryDirectory(prefix="teachlab-code-") as tmp:
        path = Path(tmp) / "main.py"
        await asyncio.to_thread(path.write_text, program, encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8", "SYSTEMROOT": os.environ.get(
            "SYSTEMROOT", "")}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-S", str(path), cwd=tmp, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return CodeRunResult(False, "Timed out", timed_out=True)
    text = out.decode("utf-8", errors="replace")[-MAX_OUTPUT:]
    return CodeRunResult(proc.returncode == 0 and "ALL_TESTS_PASSED" in text, text)
