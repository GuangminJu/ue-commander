"""
UE build and compile management.
Wraps UnrealBuildTool (UBT) with correct arguments so AI never gets them wrong.
"""

import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .config import UEConfig
from .ue_process import find_project_ue_process

# Valid values — exposed to AI so it can never guess wrong values
BuildConfig = Literal["Debug", "DebugGame", "Development", "Shipping", "Test"]
BuildPlatform = Literal["Win64", "Win32", "Mac", "Linux", "Android", "IOS"]
BuildTarget = Literal["Editor", "Game", "Client", "Server"]


@dataclass
class BuildResult:
    ok: bool
    return_code: int | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    output_tail: str = ""       # Last N lines of output for context
    command: str = ""


_ERROR_RE = re.compile(r"error\s+[A-Z]\d+:", re.IGNORECASE)
_WARNING_RE = re.compile(r"warning\s+[A-Z]\d+:", re.IGNORECASE)
_COMPILE_ERROR_RE = re.compile(r"\((\d+)\)\s*:\s*(error|fatal error)\s+", re.IGNORECASE)


def _parse_output(lines: list[str]) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    for line in lines:
        if _COMPILE_ERROR_RE.search(line) or _ERROR_RE.search(line):
            errors.append(line.strip())
        elif _WARNING_RE.search(line):
            warnings.append(line.strip())
    return errors, warnings


def compile(
    cfg: UEConfig,
    target: BuildTarget = "Editor",
    config: BuildConfig = "Development",
    platform: BuildPlatform = "Win64",
    timeout: int = 600,
    live_output_lines: int = 40,
) -> BuildResult:
    """
    Compile the project's C++ code using UnrealBuildTool.

    The target name is constructed as:  {ProjectName}{target}
    e.g. "OhMyUEEditor" for target="Editor"

    This is the ONLY correct way to compile — never call Build.bat manually.
    """
    if not cfg.build_bat.exists():
        return BuildResult(
            ok=False,
            return_code=None,
            errors=[f"Build.bat not found: {cfg.build_bat}"],
            command="",
        )

    target_name = f"{cfg.project_name}{target}" if target != "Game" else cfg.project_name

    cmd = [
        str(cfg.build_bat),
        target_name,
        platform,
        config,
        str(cfg.project_path),
        "-WaitMutex",       # Wait if another UBT is running (no crash)
        "-FromMsBuild",     # Structured output
    ]
    command_str = " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd)

    all_lines: list[str] = []
    lock = threading.Lock()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        assert proc.stdout is not None
        for line in proc.stdout:
            with lock:
                all_lines.append(line)

        proc.wait(timeout=timeout)
        rc = proc.returncode

    except subprocess.TimeoutExpired:
        proc.kill()
        return BuildResult(
            ok=False,
            return_code=-1,
            errors=[f"Build timed out after {timeout}s"],
            output_tail="\n".join(all_lines[-live_output_lines:]),
            command=command_str,
        )
    except Exception as e:
        return BuildResult(
            ok=False,
            return_code=None,
            errors=[f"Failed to start build process: {e}"],
            command=command_str,
        )

    errors, warnings = _parse_output(all_lines)
    tail = "\n".join(all_lines[-live_output_lines:])

    return BuildResult(
        ok=(rc == 0),
        return_code=rc,
        errors=errors[:20],    # Cap to avoid flooding context
        warnings=warnings[:10],
        output_tail=tail,
        command=command_str,
    )


def get_recent_log(cfg: UEConfig, lines: int = 50) -> dict:
    """
    Read the most recent UE editor log file.
    Useful for diagnosing crashes or errors after a session.
    """
    log_dir = cfg.project_path.parent / "Saved" / "Logs"
    if not log_dir.exists():
        return {"ok": False, "error": "Log directory not found. Has the project been run?"}

    log_files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not log_files:
        return {"ok": False, "error": "No log files found."}

    latest = log_files[0]
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")
        tail_lines = text.splitlines()[-lines:]
        return {
            "ok": True,
            "log_file": str(latest.name),
            "lines": "\n".join(tail_lines),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_compile_errors(cfg: UEConfig) -> dict:
    """
    Parse the most recent log for compile errors specifically.
    Returns structured error list, not raw log.
    """
    result = get_recent_log(cfg, lines=500)
    if not result["ok"]:
        return result

    errors = []
    warnings = []
    for line in result["lines"].splitlines():
        if _COMPILE_ERROR_RE.search(line) or ("error" in line.lower() and ".cpp" in line.lower()):
            errors.append(line.strip())
        elif "warning" in line.lower() and ".cpp" in line.lower():
            warnings.append(line.strip())

    return {
        "ok": True,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors[:30],
        "warnings": warnings[:10],
    }
