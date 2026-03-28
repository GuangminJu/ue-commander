"""
UE process management: launch, close, status.

Key guarantees:
  1. Only ONE UE editor instance per project.
  2. Ownership tracking: AI-launched editors can be AI-closed;
     user-launched editors require explicit user override to close.
"""

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from .config import UEConfig


# ---------------------------------------------------------------------------
# Ownership lock file
# ---------------------------------------------------------------------------

def _lock_path(cfg: UEConfig) -> Path:
    """Lock file lives in project Saved/ — ignored by UE and git."""
    return cfg.project_path.parent / "Saved" / ".ue_commander.lock"


def _write_lock(cfg: UEConfig, pid: int) -> None:
    lock = _lock_path(cfg)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": pid, "launched_by": "ue-commander", "ts": time.time()}))


def _read_lock(cfg: UEConfig) -> dict | None:
    lock = _lock_path(cfg)
    if not lock.exists():
        return None
    try:
        return json.loads(lock.read_text(encoding="utf-8"))
    except Exception:
        return None


def _clear_lock(cfg: UEConfig) -> None:
    lock = _lock_path(cfg)
    if lock.exists():
        lock.unlink(missing_ok=True)


def _is_ai_launched(cfg: UEConfig, current_pid: int) -> bool:
    """Check if the currently running UE was launched by ue-commander."""
    lock = _read_lock(cfg)
    if lock is None:
        return False
    return lock.get("pid") == current_pid


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class UEProcessInfo:
    running: bool
    pid: int | None = None
    project: str | None = None
    uptime_seconds: float | None = None
    memory_mb: float | None = None
    launched_by: str | None = None   # "ue-commander" or "user"


# ---------------------------------------------------------------------------
# Process discovery
# ---------------------------------------------------------------------------

def find_ue_processes(cfg: UEConfig) -> list[psutil.Process]:
    """Return all running UnrealEditor processes, regardless of project."""
    result = []
    for proc in psutil.process_iter(["name", "exe", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            exe = proc.info["exe"] or ""
            if "unrealedit" in name or "unrealedit" in exe.lower():
                result.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return result


def find_project_ue_process(cfg: UEConfig) -> psutil.Process | None:
    """Find the UE process specifically for our project (by cmdline)."""
    uproject_str = str(cfg.project_path).lower().replace("\\", "/")
    for proc in find_ue_processes(cfg):
        try:
            cmdline = " ".join(proc.cmdline()).lower().replace("\\", "/")
            if uproject_str in cmdline:
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None


def get_status(cfg: UEConfig) -> UEProcessInfo:
    """Get status of the UE editor for this project."""
    all_procs = find_ue_processes(cfg)
    proj_proc = find_project_ue_process(cfg)

    if proj_proc:
        try:
            mem = proj_proc.memory_info().rss / (1024 * 1024)
            uptime = time.time() - proj_proc.create_time()
            ai = _is_ai_launched(cfg, proj_proc.pid)
            return UEProcessInfo(
                running=True,
                pid=proj_proc.pid,
                project=cfg.project_name,
                uptime_seconds=round(uptime),
                memory_mb=round(mem, 1),
                launched_by="ue-commander" if ai else "user",
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # UE is running but not for our project
    if all_procs:
        return UEProcessInfo(
            running=False,
            pid=None,
            project=None,
        )

    return UEProcessInfo(running=False)


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

# Default args applied to every editor launch.
# -auto: auto-accept "modules out of date, rebuild?" dialog (no popup)
# -skipcompile: don't try editor-internal live compile on startup
_DEFAULT_LAUNCH_ARGS = ["-auto", "-skipcompile"]


def launch(
    cfg: UEConfig,
    extra_args: list[str] | None = None,
    compile_first: bool = True,
) -> dict:
    """
    Launch UE editor for this project.

    By default, compiles C++ modules BEFORE launching so the editor never
    shows the "modules are missing / out of date" rebuild dialog.

    Writes a lock file so ue-commander knows it owns this process.
    Returns error if an instance is already running (prevents duplicates).
    """
    existing = find_project_ue_process(cfg)
    if existing:
        ai = _is_ai_launched(cfg, existing.pid)
        return {
            "ok": False,
            "error": f"UE is already running for project '{cfg.project_name}' (PID {existing.pid}). "
                     f"Launched by: {'ue-commander' if ai else 'user'}. "
                     "Use ue_close first if you need to restart.",
            "pid": existing.pid,
            "launched_by": "ue-commander" if ai else "user",
        }

    # Pre-compile if requested (default: yes)
    if compile_first:
        from .ue_build import compile as ue_compile
        compile_result = ue_compile(cfg)
        if not compile_result.ok:
            return {
                "ok": False,
                "error": "Pre-launch compile failed. Fix errors before launching the editor.",
                "compile_errors": compile_result.errors[:10],
                "output_tail": compile_result.output_tail,
            }

    other_ue = find_ue_processes(cfg)
    warning = None
    if other_ue:
        pids = [p.pid for p in other_ue]
        warning = f"Other UE instances are running (PIDs: {pids}). Launching for this project anyway."

    cmd = [str(cfg.editor_exe), str(cfg.project_path)]
    cmd.extend(_DEFAULT_LAUNCH_ARGS)
    if extra_args:
        cmd.extend(extra_args)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS if hasattr(subprocess, "DETACHED_PROCESS") else 0,
    )

    # Record ownership
    _write_lock(cfg, proc.pid)

    result = {
        "ok": True,
        "pid": proc.pid,
        "message": f"Launched UnrealEditor for '{cfg.project_name}' (PID {proc.pid}).",
        "launched_by": "ue-commander",
        "warning": warning,
        "command": " ".join(str(c) for c in cmd),
    }
    if compile_first:
        result["pre_compiled"] = True
    return result


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------

def close(cfg: UEConfig, force: bool = False, timeout: int = 30, user_override: bool = False) -> dict:
    """
    Close UE editor for this project.

    Ownership rules:
      - AI-launched (lock file matches PID): close is allowed.
      - User-launched (no lock or PID mismatch): REFUSED unless user_override=True.

    This prevents AI from accidentally closing an editor the user opened.
    """
    proc = find_project_ue_process(cfg)
    if not proc:
        all_procs = find_ue_processes(cfg)
        if all_procs:
            pids = [p.pid for p in all_procs]
            return {
                "ok": False,
                "error": f"No UE instance found for project '{cfg.project_name}'. "
                         f"Other UE instances exist (PIDs: {pids}) — close them manually if needed.",
            }
        _clear_lock(cfg)
        return {"ok": True, "message": "No UE process was running."}

    pid = proc.pid
    ai_launched = _is_ai_launched(cfg, pid)

    # Ownership check
    if not ai_launched and not user_override:
        return {
            "ok": False,
            "error": (
                f"UE (PID {pid}) was launched by the USER, not by ue-commander. "
                "AI cannot close a user-launched editor without explicit permission. "
                "Ask the user to close it, or call ue_close with user_override=true "
                "if the user has granted permission."
            ),
            "launched_by": "user",
            "pid": pid,
        }

    try:
        if force:
            proc.kill()
            _clear_lock(cfg)
            return {"ok": True, "message": f"Force-killed UE process (PID {pid}).", "launched_by": "ue-commander" if ai_launched else "user"}

        # Try graceful shutdown via plugin HTTP API first:
        # SaveAll to avoid save dialog, then RequestExit
        closed_via_plugin = False
        try:
            from . import ue_editor
            if ue_editor.is_plugin_available():
                ue_editor.call_plugin("RequestExit", timeout=10)
                # Wait briefly for editor to start closing
                try:
                    proc.wait(timeout=5)
                    closed_via_plugin = True
                except psutil.TimeoutExpired:
                    pass
        except Exception:
            pass

        if closed_via_plugin:
            _clear_lock(cfg)
            return {"ok": True, "message": f"Closed UE gracefully via plugin (PID {pid}).", "launched_by": "ue-commander" if ai_launched else "user"}

        # Fallback: OS-level terminate
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
            _clear_lock(cfg)
            return {"ok": True, "message": f"Closed UE via terminate (PID {pid}).", "launched_by": "ue-commander" if ai_launched else "user"}
        except psutil.TimeoutExpired:
            # Last resort: force kill
            proc.kill()
            proc.wait(timeout=5)
            _clear_lock(cfg)
            return {"ok": True, "message": f"Force-killed UE after timeout (PID {pid}).", "launched_by": "ue-commander" if ai_launched else "user"}
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        _clear_lock(cfg)
        return {"ok": False, "error": str(e)}


def close_all_ue(force: bool = False) -> dict:
    """Close ALL running UE editor instances. Use with caution."""
    killed = []
    errors = []
    for proc in find_ue_processes(None):  # type: ignore[arg-type]
        try:
            pid = proc.pid
            if force:
                proc.kill()
            else:
                proc.terminate()
            killed.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            errors.append(str(e))
    return {
        "ok": len(errors) == 0,
        "killed_pids": killed,
        "errors": errors,
        "message": f"Closed {len(killed)} UE instance(s).",
    }
