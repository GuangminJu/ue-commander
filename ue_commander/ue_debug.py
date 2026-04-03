"""
Lightweight debugger via CDB (Console Debugger from Windows SDK).

AI-controllable debugging for UE editor processes:
  - Attach/detach without killing the process
  - Pause and inspect call stacks
  - Set/remove breakpoints by symbol or source location
  - Evaluate expressions and inspect variables
  - Resume execution

CDB is part of 'Debugging Tools for Windows' (Windows SDK component).
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

_PROMPT_RE = re.compile(r"\d+:\d+(?::\w+)?>\s*$")
_GO_COMMANDS = frozenset({
    "g", "go", "gc", "gu", "gh", "gn", "gN",
    "t", "p", "pa", "pc", "pt", "ta", "tc", "tt", "wt",
})
_DANGEROUS_COMMANDS = frozenset({
    "q", "qq", ".kill", ".restart", ".reboot", ".crash",
})


def find_cdb() -> Path | None:
    """Locate cdb.exe from Windows SDK."""
    if sys.platform != "win32":
        return None

    found = shutil.which("cdb.exe")
    if found:
        return Path(found)

    for kits_base in [
        Path("C:/Program Files (x86)/Windows Kits/10/Debuggers/x64"),
        Path("C:/Program Files/Windows Kits/10/Debuggers/x64"),
        Path("C:/Program Files (x86)/Windows Kits/8.1/Debuggers/x64"),
        Path("C:/Program Files/Windows Kits/8.1/Debuggers/x64"),
    ]:
        cdb = kits_base / "cdb.exe"
        if cdb.exists():
            return cdb

    sdk_dir = os.environ.get("WindowsSdkDir")
    if sdk_dir:
        cdb = Path(sdk_dir) / "Debuggers" / "x64" / "cdb.exe"
        if cdb.exists():
            return cdb

    return None


class DebugSession:
    """Persistent CDB session attached to a target process."""

    def __init__(self, target_pid: int, cdb_path: Path):
        self.target_pid = target_pid
        self.cdb_path = cdb_path
        self.proc: subprocess.Popen | None = None
        self.attached = False
        self._cmd_lock = threading.Lock()
        self._buf: list[str] = []
        self._buf_lock = threading.Lock()
        self._new_data = threading.Event()

    def attach(self, symbol_paths: list[str] | None = None) -> str:
        """Attach CDB to the target process. The target is paused on attach."""
        cmd = [str(self.cdb_path), "-p", str(self.target_pid), "-lines"]
        if symbol_paths:
            cmd.extend(["-y", ";".join(symbol_paths)])

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )

        threading.Thread(target=self._reader, daemon=True).start()

        try:
            output = self._wait_prompt(timeout=30)
            if self.proc.poll() is not None:
                raise RuntimeError(f"CDB exited unexpectedly:\n{output}")
        except Exception:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
            self.proc = None
            raise

        self.attached = True
        return output

    def command(self, cmd: str, timeout: float = 30) -> str:
        """Send a debugger command and return its output."""
        if not self.attached or not self.proc:
            return "Error: no active debug session."

        # Block dangerous commands that could kill the target
        base = cmd.strip().split()[0].lower() if cmd.strip() else ""
        if base in _DANGEROUS_COMMANDS:
            return f"Blocked: '{base}' can terminate the target. Use ue_debug_detach instead."

        # Reject newlines to prevent command injection
        if "\n" in cmd or "\r" in cmd:
            return "Error: command must not contain newlines."

        with self._cmd_lock:
            self._clear_buf()

            try:
                self.proc.stdin.write(f"{cmd}\n".encode())
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.attached = False
                return "Error: CDB process died. Debug session lost."

            # go/step commands resume execution — return immediately
            if base in _GO_COMMANDS:
                time.sleep(0.1)
                return self._drain_buf() or "Process resumed."

            return self._wait_prompt(timeout)

    def send_break(self) -> str:
        """Break into a running target via DebugBreakProcess."""
        if not self.attached or not self.proc:
            return "Error: no active debug session."

        with self._cmd_lock:
            self._clear_buf()

            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.DebugBreakProcess.argtypes = [wintypes.HANDLE]
            kernel32.DebugBreakProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            PROCESS_ALL_ACCESS = 0x1F0FFF
            handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, self.target_pid)
            if not handle:
                return "Failed to open target process for break."
            try:
                if not kernel32.DebugBreakProcess(handle):
                    return "DebugBreakProcess failed."
            finally:
                kernel32.CloseHandle(handle)

            return self._wait_prompt(timeout=10)

    def detach(self) -> str:
        """Detach from the target process (it continues running)."""
        if not self.attached:
            return "Not attached."

        # If the target is running (after 'g'), CDB can't process .detach.
        # Break first to bring CDB back to a prompt.
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1F0FFF, False, self.target_pid)
            if handle:
                kernel32.DebugBreakProcess(handle)
                kernel32.CloseHandle(handle)
                time.sleep(0.3)
        except Exception:
            pass

        try:
            self.proc.stdin.write(b".detach\nq\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=5)
        except Exception as e:
            msg = f"Clean detach failed ({e}), killing CDB."
            if self.proc:
                try:
                    self.proc.kill()
                except OSError:
                    pass
            self.attached = False
            self.proc = None
            return msg

        self.attached = False
        self.proc = None
        return "Detached. Target process continues running."

    # -- internal -------------------------------------------------------

    def _reader(self):
        """Background thread: read CDB stdout into buffer."""
        fd = self.proc.stdout.fileno()
        try:
            while True:
                data = os.read(fd, 4096)
                if not data:
                    break
                with self._buf_lock:
                    self._buf.append(data.decode("utf-8", errors="replace"))
                self._new_data.set()
        except OSError:
            pass
        finally:
            self.attached = False
            self._new_data.set()

    def _wait_prompt(self, timeout: float) -> str:
        """Block until CDB outputs a prompt, then return all collected text."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            self._new_data.wait(timeout=min(remaining, 0.5))
            self._new_data.clear()

            # Check and clear in one lock acquisition to avoid TOCTOU
            with self._buf_lock:
                text = "".join(self._buf)
                if _PROMPT_RE.search(text):
                    self._buf.clear()
                    return text

        return self._drain_buf() + "\n[TIMEOUT waiting for CDB prompt]"

    def _clear_buf(self):
        with self._buf_lock:
            self._buf.clear()
        self._new_data.clear()

    def _drain_buf(self) -> str:
        with self._buf_lock:
            text = "".join(self._buf)
            self._buf.clear()
        return text


# ---------------------------------------------------------------------------
# Global session management
# ---------------------------------------------------------------------------

_session: DebugSession | None = None
_session_lock = threading.Lock()


def get_session() -> DebugSession | None:
    """Get the active debug session, or None."""
    global _session
    with _session_lock:
        if _session is not None:
            if not _session.attached or (_session.proc and _session.proc.poll() is not None):
                _session = None
        return _session


def create_session(target_pid: int) -> DebugSession:
    """Create a new debug session, closing any existing one."""
    global _session
    with _session_lock:
        if _session is not None and _session.attached:
            _session.detach()

        cdb = find_cdb()
        if cdb is None:
            raise RuntimeError(
                "cdb.exe not found. Install 'Debugging Tools for Windows' from Windows SDK:\n"
                "  winget install Microsoft.WindowsSDK\n"
                "  (select 'Debugging Tools for Windows' during install)"
            )

        session = DebugSession(target_pid=target_pid, cdb_path=cdb)
        try:
            # Don't set _session until attach succeeds
            _session = session
            return session
        except Exception:
            _session = None
            raise


def close_session() -> str:
    """Close the active debug session."""
    global _session
    with _session_lock:
        if _session is not None and _session.attached:
            result = _session.detach()
            _session = None
            return result
        _session = None
        return "No active debug session."
