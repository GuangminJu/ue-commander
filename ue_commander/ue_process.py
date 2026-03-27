"""
UE process management: launch, close, status.
Key guarantee: only ONE UE editor instance per project.
"""

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from .config import UEConfig


@dataclass
class UEProcessInfo:
    running: bool
    pid: int | None = None
    project: str | None = None
    uptime_seconds: float | None = None
    memory_mb: float | None = None


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
            return UEProcessInfo(
                running=True,
                pid=proj_proc.pid,
                project=cfg.project_name,
                uptime_seconds=round(uptime),
                memory_mb=round(mem, 1),
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


def launch(cfg: UEConfig, extra_args: list[str] | None = None) -> dict:
    """
    Launch UE editor for this project.
    Returns error if an instance is already running (prevents duplicates).
    """
    existing = find_project_ue_process(cfg)
    if existing:
        return {
            "ok": False,
            "error": f"UE is already running for project '{cfg.project_name}' (PID {existing.pid}). "
                     "Use ue_close first if you need to restart.",
            "pid": existing.pid,
        }

    other_ue = find_ue_processes(cfg)
    warning = None
    if other_ue:
        pids = [p.pid for p in other_ue]
        warning = f"Other UE instances are running (PIDs: {pids}). Launching for this project anyway."

    cmd = [str(cfg.editor_exe), str(cfg.project_path)]
    if extra_args:
        cmd.extend(extra_args)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS if hasattr(subprocess, "DETACHED_PROCESS") else 0,
    )

    return {
        "ok": True,
        "pid": proc.pid,
        "message": f"Launched UnrealEditor for '{cfg.project_name}' (PID {proc.pid}).",
        "warning": warning,
        "command": " ".join(str(c) for c in cmd),
    }


def close(cfg: UEConfig, force: bool = False, timeout: int = 30) -> dict:
    """
    Close UE editor for this project.
    Tries graceful close first; falls back to terminate if force=True.
    """
    proc = find_project_ue_process(cfg)
    if not proc:
        # Check for any UE processes
        all_procs = find_ue_processes(cfg)
        if all_procs:
            pids = [p.pid for p in all_procs]
            return {
                "ok": False,
                "error": f"No UE instance found for project '{cfg.project_name}'. "
                         f"Other UE instances exist (PIDs: {pids}) — close them manually if needed.",
            }
        return {"ok": True, "message": "No UE process was running."}

    pid = proc.pid
    try:
        if force:
            proc.kill()
            return {"ok": True, "message": f"Force-killed UE process (PID {pid})."}

        # Graceful: send CTRL_CLOSE_EVENT on Windows, SIGTERM elsewhere
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
            return {"ok": True, "message": f"Closed UE gracefully (PID {pid})."}
        except psutil.TimeoutExpired:
            return {
                "ok": False,
                "error": f"UE (PID {pid}) did not close within {timeout}s. "
                         "Call ue_close with force=true to force-kill it.",
            }
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        return {"ok": False, "error": str(e)}


def close_all_ue(force: bool = False) -> dict:
    """Close ALL running UE editor instances."""
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
