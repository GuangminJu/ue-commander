"""
UE process management: launch, close, status, monitoring.

Acts as a lightweight IDE for AI — captures logs, detects crashes,
monitors process lifecycle. Generic for any UE project.

Key guarantees:
  1. Only ONE UE editor instance per project.
  2. Ownership tracking: AI-launched editors can be AI-closed;
     user-launched editors require explicit user override to close.
  3. Background log monitoring with crash detection.
"""

import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
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
# UEMonitor — background process & log monitor (like an IDE output panel)
# ---------------------------------------------------------------------------

_CRASH_MARKERS = ["Fatal error!", "Critical error:", "Assertion failed:"]
_LOG_BUFFER_SIZE = 100  # lines to keep in memory


@dataclass
class UEMonitor:
    """Monitors a launched UE process: tails log file, detects crashes."""
    pid: int
    log_path: Path
    proc: subprocess.Popen

    alive: bool = True
    crashed: bool = False
    crash_reason: str = ""
    exit_code: int | None = None
    recent_log: list[str] = field(default_factory=list)
    _stop: bool = False

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def stop(self):
        self._stop = True

    def get_state(self) -> dict:
        return {
            "alive": self.alive,
            "crashed": self.crashed,
            "crash_reason": self.crash_reason,
            "exit_code": self.exit_code,
            "recent_log": self.recent_log[-20:],
        }

    def _run(self):
        # Wait for log file to appear (UE creates it during startup)
        deadline = time.time() + 60
        while not self.log_path.exists() and time.time() < deadline and not self._stop:
            # Also check if process died before log appeared
            if self.proc.poll() is not None:
                self.alive = False
                self.exit_code = self.proc.returncode
                self.crashed = self.exit_code != 0
                self.crash_reason = f"Process exited with code {self.exit_code} before log file appeared"
                return
            time.sleep(1)

        if not self.log_path.exists():
            return

        # Tail the log file
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                # Skip to end — only process lines written AFTER launch
                f.seek(0, 2)

                while not self._stop:
                    # Check process
                    ret = self.proc.poll()
                    if ret is not None:
                        self.alive = False
                        self.exit_code = ret
                        # Read remaining lines
                        for line in f:
                            self._process_line(line.rstrip("\n\r"))
                        if ret != 0 and not self.crashed:
                            self.crashed = True
                            self.crash_reason = self.crash_reason or f"Process exited with code {ret}"
                        return

                    # Read new lines
                    line = f.readline()
                    if line:
                        self._process_line(line.rstrip("\n\r"))
                    else:
                        time.sleep(0.5)
        except Exception:
            pass

    def _process_line(self, line: str):
        if not line:
            return
        self.recent_log.append(line)
        if len(self.recent_log) > _LOG_BUFFER_SIZE:
            self.recent_log = self.recent_log[-_LOG_BUFFER_SIZE:]
        # Crash detection
        for marker in _CRASH_MARKERS:
            if marker in line:
                self.crashed = True
                self.crash_reason = line.strip()
                break


# Global monitor instance (one per server session)
_active_monitor: UEMonitor | None = None


def get_monitor() -> UEMonitor | None:
    """Get the active UE monitor, or None if no monitored process."""
    global _active_monitor
    if _active_monitor is not None and not _active_monitor.alive and _active_monitor._stop:
        _active_monitor = None
    return _active_monitor


def _normalize_close_save_mode(save_mode: str) -> str | None:
    normalized = (save_mode or "auto_save").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"auto_save", "prompt", "discard", "force"}:
        return normalized
    return None


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


def _detect_project_from_proc(proc: psutil.Process) -> str | None:
    """Try to detect the project name from the process command line."""
    try:
        for arg in proc.cmdline():
            if arg.endswith(".uproject"):
                return Path(arg).stem
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return None


def get_status(cfg: UEConfig) -> UEProcessInfo:
    """Get status of the UE editor — works with any project, not just cfg.project_name."""
    all_procs = find_ue_processes(cfg)
    proj_proc = find_project_ue_process(cfg)

    # Prefer the project-specific process if found
    active_proc = proj_proc or (all_procs[0] if all_procs else None)

    if active_proc:
        try:
            mem = active_proc.memory_info().rss / (1024 * 1024)
            uptime = time.time() - active_proc.create_time()
            ai = _is_ai_launched(cfg, active_proc.pid)
            detected_project = _detect_project_from_proc(active_proc) or cfg.project_name
            return UEProcessInfo(
                running=True,
                pid=active_proc.pid,
                project=detected_project,
                uptime_seconds=round(uptime),
                memory_mb=round(mem, 1),
                launched_by="ue-commander" if ai else "user",
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return UEProcessInfo(running=False)


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

# Default args applied to every editor launch.
_DEFAULT_LAUNCH_ARGS = ["-skipcompile"]


def launch(
    cfg: UEConfig,
    extra_args: list[str] | None = None,
) -> dict:
    """
    Launch UE editor for this project. Returns IMMEDIATELY.

    Starts a background monitor that tails the UE log and detects crashes.
    Call ue_status to poll for readiness and see live state.

    Writes a lock file so ue-commander knows it owns this process.
    Returns error if an instance is already running (prevents duplicates).
    """
    global _active_monitor

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

    other_ue = find_ue_processes(cfg)
    warning = None
    if other_ue:
        pids = [p.pid for p in other_ue]
        warning = f"Other UE instances are running (PIDs: {pids}). Launching for this project anyway."

    cmd = [str(cfg.editor_exe), str(cfg.project_path)]
    cmd.extend(_DEFAULT_LAUNCH_ARGS)
    if extra_args:
        cmd.extend(extra_args)

    # Launch without DETACHED_PROCESS or DEVNULL — these cause UE to stall
    # during Python/shader init on Windows. Use CREATE_NEW_PROCESS_GROUP
    # so the child doesn't share our console signal handlers, but still
    # inherits a normal console environment.
    proc = subprocess.Popen(
        cmd,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP") else 0,
    )

    # Record ownership
    _write_lock(cfg, proc.pid)

    # Start background monitor
    log_path = cfg.project_path.parent / "Saved" / "Logs" / f"{cfg.project_name}.log"
    if _active_monitor is not None:
        _active_monitor.stop()
    _active_monitor = UEMonitor(pid=proc.pid, log_path=log_path, proc=proc)
    _active_monitor.start()

    return {
        "ok": True,
        "status": "started",
        "pid": proc.pid,
        "message": f"Editor launched (PID {proc.pid}). Monitoring log. Call ue_status to check.",
        "launched_by": "ue-commander",
        "warning": warning,
        "command": " ".join(str(c) for c in cmd),
    }


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------

def close(
    cfg: UEConfig,
    force: bool = False,
    timeout: int = 30,
    user_override: bool = False,
    save_mode: str = "auto_save",
) -> dict:
    """
    Close UE editor for this project.

    Ownership rules:
      - AI-launched (lock file matches PID): close is allowed.
      - User-launched (no lock or PID mismatch): REFUSED unless user_override=True.

    This prevents AI from accidentally closing an editor the user opened.
    """
    normalized_save_mode = _normalize_close_save_mode(save_mode)
    if normalized_save_mode is None:
        return {
            "ok": False,
            "error": f"Unsupported save_mode '{save_mode}'. Use auto_save, prompt, discard, or force.",
        }

    global _active_monitor

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
        if _active_monitor:
            _active_monitor.stop()
            _active_monitor = None
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
        if force or normalized_save_mode == "force":
            proc.kill()
            _clear_lock(cfg)
            if _active_monitor:
                _active_monitor.stop()
                _active_monitor = None
            return {
                "ok": True,
                "message": f"Force-killed UE process (PID {pid}).",
                "launched_by": "ue-commander" if ai_launched else "user",
                "save_mode": "force",
            }

        # Try graceful shutdown via plugin HTTP API first so save mode can be honored.
        closed_via_plugin = False
        plugin_attempted = False
        try:
            from . import ue_editor
            if ue_editor.is_plugin_available():
                plugin_attempted = True
                ue_editor.call_plugin("RequestExit", timeout=max(timeout, 10), SaveMode=normalized_save_mode)
                try:
                    proc.wait(timeout=timeout)
                    closed_via_plugin = True
                except psutil.TimeoutExpired:
                    pass
        except Exception:
            pass

        if closed_via_plugin:
            _clear_lock(cfg)
            if _active_monitor:
                _active_monitor.stop()
                _active_monitor = None
            return {
                "ok": True,
                "message": f"Closed UE gracefully via plugin (PID {pid}).",
                "launched_by": "ue-commander" if ai_launched else "user",
                "save_mode": normalized_save_mode,
            }

        if plugin_attempted and normalized_save_mode == "prompt":
            return {
                "ok": False,
                "error": (
                    f"Timed out waiting for prompt-based close on UE process (PID {pid}). "
                    "Editor is still running; user may still be deciding in the save dialog."
                ),
                "launched_by": "ue-commander" if ai_launched else "user",
                "pid": pid,
                "save_mode": normalized_save_mode,
                "prompt_pending": True,
            }

        if normalized_save_mode == "auto_save":
            return {
                "ok": False,
                "error": (
                    "Cannot guarantee auto-save close because the plugin is unavailable or did not complete in time. "
                    "Retry when the plugin is reachable, or use save_mode='discard' / 'force' if you explicitly want an unsafe shutdown."
                ),
                "launched_by": "ue-commander" if ai_launched else "user",
                "pid": pid,
                "save_mode": normalized_save_mode,
            }

        # Fallback: OS-level terminate can only honor discard semantics.
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
            _clear_lock(cfg)
            if _active_monitor:
                _active_monitor.stop()
                _active_monitor = None
            return {
                "ok": True,
                "message": f"Closed UE via terminate (PID {pid}).",
                "launched_by": "ue-commander" if ai_launched else "user",
                "save_mode": normalized_save_mode,
            }
        except psutil.TimeoutExpired:
            # Last resort: force kill
            proc.kill()
            proc.wait(timeout=5)
            _clear_lock(cfg)
            if _active_monitor:
                _active_monitor.stop()
                _active_monitor = None
            return {
                "ok": True,
                "message": f"Force-killed UE after timeout (PID {pid}).",
                "launched_by": "ue-commander" if ai_launched else "user",
                "save_mode": normalized_save_mode,
            }
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        _clear_lock(cfg)
        if _active_monitor:
            _active_monitor.stop()
            _active_monitor = None
        return {"ok": False, "error": str(e)}


def close_all_ue(force: bool = False, timeout: int = 30, save_mode: str = "discard") -> dict:
    """Close ALL running UE editor instances. Use with caution."""
    global _active_monitor
    normalized_save_mode = _normalize_close_save_mode(save_mode)
    if normalized_save_mode is None:
        return {
            "ok": False,
            "error": f"Unsupported save_mode '{save_mode}'. Use auto_save, prompt, discard, or force.",
        }

    processes = list(find_ue_processes(None))  # type: ignore[arg-type]
    if not processes:
        if _active_monitor:
            _active_monitor.stop()
            _active_monitor = None
        return {
            "ok": True,
            "closed_pids": [],
            "force_killed_pids": [],
            "errors": [],
            "save_mode": normalized_save_mode,
            "message": "No UE processes were running.",
        }

    if force or normalized_save_mode == "force":
        killed = []
        errors = []
        for proc in processes:
            try:
                proc.kill()
                killed.append(proc.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                errors.append(str(e))
        if _active_monitor:
            _active_monitor.stop()
            _active_monitor = None
        return {
            "ok": len(errors) == 0,
            "closed_pids": killed,
            "force_killed_pids": killed,
            "errors": errors,
            "save_mode": "force",
            "message": f"Force-closed {len(killed)} UE instance(s).",
        }

    if normalized_save_mode in {"auto_save", "prompt"}:
        if len(processes) != 1:
            return {
                "ok": False,
                "error": (
                    f"Cannot use save_mode='{normalized_save_mode}' with {len(processes)} running UE instances. "
                    "Close instances individually with ue_close, or use save_mode='discard' / 'force' for bulk shutdown."
                ),
                "pid_count": len(processes),
                "save_mode": normalized_save_mode,
            }

        proc = processes[0]
        plugin_attempted = False
        errors = []
        try:
            from . import ue_editor
            if ue_editor.is_plugin_available():
                plugin_attempted = True
                ue_editor.call_plugin("RequestExit", timeout=max(timeout, 10), SaveMode=normalized_save_mode)
                try:
                    proc.wait(timeout=timeout)
                    if _active_monitor:
                        _active_monitor.stop()
                        _active_monitor = None
                    return {
                        "ok": True,
                        "closed_pids": [proc.pid],
                        "force_killed_pids": [],
                        "errors": [],
                        "save_mode": normalized_save_mode,
                        "message": f"Closed 1 UE instance via plugin with save_mode='{normalized_save_mode}'.",
                    }
                except psutil.TimeoutExpired:
                    pass
        except Exception as e:
            errors.append(str(e))

        if plugin_attempted and normalized_save_mode == "prompt":
            return {
                "ok": False,
                "error": (
                    f"Timed out waiting for prompt-based close on UE process (PID {proc.pid}). "
                    "Editor is still running; user may still be deciding in the save dialog."
                ),
                "closed_pids": [],
                "force_killed_pids": [],
                "errors": errors,
                "save_mode": normalized_save_mode,
                "prompt_pending": True,
            }

        return {
            "ok": False,
            "error": (
                f"Cannot guarantee {normalized_save_mode} close because the plugin is unavailable or did not complete in time. "
                "Retry when the plugin is reachable, or use save_mode='discard' / 'force' for bulk shutdown."
            ),
            "closed_pids": [],
            "force_killed_pids": [],
            "errors": errors,
            "save_mode": normalized_save_mode,
        }

    closed = []
    force_killed = []
    errors = []
    for proc in processes:
        try:
            pid = proc.pid
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
                closed.append(pid)
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                closed.append(pid)
                force_killed.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            errors.append(str(e))
    if _active_monitor:
        _active_monitor.stop()
        _active_monitor = None
    return {
        "ok": len(errors) == 0,
        "closed_pids": closed,
        "force_killed_pids": force_killed,
        "errors": errors,
        "save_mode": normalized_save_mode,
        "message": f"Closed {len(closed)} UE instance(s).",
    }
