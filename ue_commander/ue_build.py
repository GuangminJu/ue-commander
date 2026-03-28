"""
UE build and compile management.
Wraps UnrealBuildTool (UBT) with correct arguments so AI never gets them wrong.
"""

import asyncio
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol


class ProgressReporter(Protocol):
    """Callback for reporting build progress (compatible with MCP Context)."""
    async def report_progress(self, progress: float, total: float | None = None, message: str | None = None) -> None: ...
    async def info(self, message: str) -> None: ...

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


async def compile(
    cfg: UEConfig,
    target: BuildTarget = "Editor",
    config: BuildConfig = "Development",
    platform: BuildPlatform = "Win64",
    timeout: int = 600,
    live_output_lines: int = 40,
    ctx: ProgressReporter | None = None,
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
    # Escaped quotes around project path (matches Rider's format)
    project_escaped = f'\\"{cfg.project_path}\\"'

    # Use Rider-style -Target= syntax (modern UBT format)
    # Primary target: the project editor/game
    primary_target = f'{target_name} {platform} {config} -Project={project_escaped}'
    # ShaderCompileWorker: always needed alongside editor builds
    scw_target = f'ShaderCompileWorker {platform} Development -Project={project_escaped} -Quiet'

    # Map platform to architecture flag
    arch_map = {"Win64": "x64", "Win32": "x86"}
    arch = arch_map.get(platform, "")

    # Build the full command line string with correct quoting for cmd.exe
    # -Target= values contain spaces and embedded quotes, so we must
    # construct the string exactly as Rider does on the command line.
    bat = str(cfg.build_bat)
    parts = [
        f'"{bat}"' if " " in bat else bat,
        f'-Target="{primary_target}"',
        f'-Target="{scw_target}"',
        "-WaitMutex",
        "-FromMsBuild",
    ]
    if arch:
        parts.append(f"-architecture={arch}")
    command_str = " ".join(parts)

    if ctx:
        await ctx.info(f"Starting compilation: {target_name} {config} {platform}")
        await ctx.report_progress(0, 100, "Compiling...")

    all_lines: list[str] = []

    try:
        # Use shell mode so cmd.exe parses the quoting correctly
        proc = await asyncio.create_subprocess_shell(
            command_str,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        assert proc.stdout is not None
        compile_re = re.compile(r"\[(\d+)/(\d+)\]")
        last_pct = -1

        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace")
            all_lines.append(line)

            # Report progress from UBT's [N/Total] markers
            if ctx:
                m = compile_re.search(line)
                if m:
                    current, total = int(m.group(1)), int(m.group(2))
                    pct = int(current * 100 / total) if total > 0 else 0
                    if pct > last_pct:
                        last_pct = pct
                        await ctx.report_progress(current, total, f"Compiling [{current}/{total}]")

        rc = await asyncio.wait_for(proc.wait(), timeout=timeout)

    except asyncio.TimeoutError:
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
    # Return full output so callers can see the complete build log (like Rider's Build Output)
    full_output = "".join(all_lines)

    if ctx:
        if rc == 0:
            await ctx.info(f"Compilation succeeded ({len(warnings)} warnings)")
        else:
            await ctx.info(f"Compilation FAILED ({len(errors)} errors)")

    return BuildResult(
        ok=(rc == 0),
        return_code=rc,
        errors=errors[:20],    # Cap to avoid flooding context
        warnings=warnings[:10],
        output_tail=full_output,
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
