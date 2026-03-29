"""
UE build and compile management.
Wraps UnrealBuildTool (UBT) with correct arguments so AI never gets them wrong.
"""

import asyncio
import re
import subprocess
import threading
import psutil
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


def _kill_conflicting_ubt() -> list[int]:
    """Kill UBT/Build.bat processes and delete the file lock so compilation can proceed."""
    import tempfile, os

    killed = []
    # Kill dotnet processes running UnrealBuildTool
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            cmdline = " ".join(proc.info["cmdline"] or [])
            if ("dotnet" in name and "UnrealBuildTool" in cmdline) or \
               ("cmd" in name and "Build.bat" in cmdline):
                proc.kill()
                killed.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Delete the Build.bat file lock
    # Lock file path: %TEMP%\<Build.bat path with \ -> - and : stripped>.lock
    build_bat = Path("E:/UnrealEngine/Engine/Build/BatchFiles/Build.bat")
    lock_name = str(build_bat).replace("\\", "-").replace("/", "-").replace(":", "") + ".lock"
    lock_path = Path(tempfile.gettempdir()) / lock_name
    if lock_path.exists():
        try:
            lock_path.unlink()
        except OSError:
            pass

    return killed


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
    _retry: bool = False,
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

    # Map platform to architecture flag
    arch_map = {"Win64": "x64", "Win32": "x86"}
    arch = arch_map.get(platform, "")

    # Prefer calling UBT dll directly (bypasses Build.bat file-lock entirely).
    # Fall back to Build.bat only if the dll is not found.
    ubt_dll = cfg.build_bat.parent.parent.parent / "Binaries" / "DotNET" / "UnrealBuildTool" / "UnrealBuildTool.dll"
    if ubt_dll.exists():
        # Direct dotnet invocation — pass args directly, no shell quoting needed
        proj = str(cfg.project_path)
        primary_target = f'{target_name} {platform} {config} -Project="{proj}"'
        scw_target = f'ShaderCompileWorker {platform} Development -Project="{proj}" -Quiet'
        exec_args = [
            "dotnet",
            str(ubt_dll),
            f"-Target={primary_target}",
            f"-Target={scw_target}",
            "-FromMsBuild",
        ]
        if arch:
            exec_args.append(f"-architecture={arch}")
        command_str = " ".join(exec_args)
        use_shell = False
    else:
        # Legacy: call Build.bat via cmd shell (uses file lock)
        project_escaped = f'\\"{cfg.project_path}\\"'
        primary_target = f'{target_name} {platform} {config} -Project={project_escaped}'
        scw_target = f'ShaderCompileWorker {platform} Development -Project={project_escaped} -Quiet'
        bat = str(cfg.build_bat)
        bat_parts = [
            f'"{bat}"' if " " in bat else bat,
            f'-Target="{primary_target}"',
            f'-Target="{scw_target}"',
            "-WaitMutex",
            "-FromMsBuild",
        ]
        if arch:
            bat_parts.append(f"-architecture={arch}")
        command_str = " ".join(bat_parts)
        exec_args = []
        use_shell = True

    if ctx:
        await ctx.info(f"Starting compilation: {target_name} {config} {platform}")
        await ctx.report_progress(0, 100, "Compiling...")

    all_lines: list[str] = []

    try:
        if use_shell:
            proc = await asyncio.create_subprocess_shell(
                command_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *exec_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

        assert proc.stdout is not None
        compile_re = re.compile(r"\[(\d+)/(\d+)\]")
        current, total = 0, 100
        last_pct = -1
        import time
        last_info_time = time.time()
        INFO_INTERVAL = 10  # seconds between heartbeat info messages

        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace")
            all_lines.append(line)

            # Detect mutex deadlock — kill conflicting processes, delete lock, restart
            if "Build.bat is already running" in line or "waiting for existing script to terminate" in line.lower():
                proc.kill()
                killed = _kill_conflicting_ubt()
                if _retry:
                    # Already retried once — give up
                    msg = f"UBT mutex conflict persists after retry (killed {killed}). Check for stuck Build.bat processes."
                    if ctx:
                        await ctx.info(f"✗ {msg}")
                    return BuildResult(ok=False, return_code=-1, errors=[msg],
                                       output_tail="\n".join(all_lines), command=command_str)
                if ctx:
                    await ctx.info(f"⚠ UBT mutex conflict — killed {killed}, cleared lock. Restarting build...")
                await asyncio.sleep(2)
                return await compile(cfg, target=target, config=config, platform=platform,
                                     timeout=timeout, live_output_lines=live_output_lines, ctx=ctx,
                                     _retry=True)

            if ctx and line.strip():
                m = compile_re.search(line)
                if m:
                    current, total = int(m.group(1)), int(m.group(2))
                    log_part = line.split("]", 1)[-1].strip() if "]" in line else line.strip()
                    await ctx.info(f"[{current}/{total}] {log_part}")
                else:
                    await ctx.info(line.rstrip())

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
