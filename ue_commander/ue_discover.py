"""
Discover all UE engine installations and projects across the machine.

Search strategy:
  1. Fast path  — Everything (es.exe) for millisecond full-disk search
  2. Fallback   — Registry + LauncherInstalled.dat + os.walk (slower but zero-dependency)

Project filtering:
  .uproject files appear in many places (Intermediate/, Saved/, engine samples).
  We classify each hit and only surface real project roots.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Directories that contain .uproject *copies*, not real project roots
_SKIP_DIRS = frozenset({
    "intermediate", "saved", "deriveddatacache", "binaries",
    ".git", "node_modules", "__pycache__", ".vs", ".vscode",
})

# Directories inside engine installs that contain template/sample .uproject files
_ENGINE_INTERNAL_DIRS = frozenset({
    "templates", "samples", "featurepacks",
})

# Top-level Windows directories to skip during os.walk fallback
_SKIP_ROOTS = frozenset({
    "windows", "$recycle.bin", "system volume information",
    "programdata", "recovery", "msocache",
})

# Max depth for os.walk fallback (avoids crawling forever)
_MAX_WALK_DEPTH = 6


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class EngineInfo:
    path: str
    version: str         # e.g. "5.7", "5.5.1", or "unknown"
    engine_type: str     # "source" | "installed" | "unknown"
    association: str     # GUID or version string from registry


@dataclass
class ProjectInfo:
    name: str
    path: str                  # Full path to .uproject file
    engine_association: str    # Raw EngineAssociation from JSON
    engine_path: str | None    # Resolved engine path (if possible)
    has_source: bool           # Has Source/ directory
    has_content: bool          # Has Content/ directory
    has_plugins: bool          # Has Plugins/ directory
    is_engine_sample: bool     # Inside an engine install directory
    module_count: int          # Number of modules in .uproject


@dataclass
class DiscoverResult:
    engines: list[EngineInfo] = field(default_factory=list)
    projects: list[ProjectInfo] = field(default_factory=list)
    skipped_count: int = 0
    search_method: str = "unknown"
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Everything integration
# ---------------------------------------------------------------------------

def _find_es_exe() -> str | None:
    """
    Locate Everything CLI (es.exe).
    Checks: PATH, common install locations, and alongside Everything.exe.
    Returns full path or None.
    """
    # 1. Check PATH
    try:
        r = subprocess.run(
            ["es.exe", "-get-everything-version"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            return "es.exe"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 2. Check common locations
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Everything" / "es.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Everything" / "es.exe",
        Path.home() / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "es.exe",
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Everything 1.5a" / "es.exe",
    ]
    for c in candidates:
        if c.exists():
            try:
                r = subprocess.run(
                    [str(c), "-get-everything-version"],
                    capture_output=True, text=True, timeout=3,
                )
                if r.returncode == 0:
                    return str(c)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

    return None


# Cached es.exe path (None = not checked yet, "" = not found)
_es_exe_path: str | None = None


def _has_everything() -> bool:
    """Check if Everything CLI (es.exe) is reachable."""
    global _es_exe_path
    if _es_exe_path is None:
        _es_exe_path = _find_es_exe() or ""
    return _es_exe_path != ""


def _search_everything(pattern: str) -> list[str]:
    """
    Use Everything CLI to search. Returns list of absolute paths.
    Raises RuntimeError if es.exe fails.
    """
    global _es_exe_path
    if not _es_exe_path:
        raise RuntimeError("es.exe not found")
    r = subprocess.run(
        [_es_exe_path, "-n", "0", pattern],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"es.exe failed (rc={r.returncode}): {r.stderr.strip()}")
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# os.walk fallback
# ---------------------------------------------------------------------------

def _get_drive_letters() -> list[str]:
    """Return available drive letters on Windows."""
    if sys.platform != "win32":
        return ["/"]
    drives = []
    for c in range(ord("C"), ord("Z") + 1):
        d = f"{chr(c)}:\\"
        if os.path.isdir(d):
            drives.append(d)
    return drives


def _walk_for_uprojects(roots: list[str], max_depth: int = _MAX_WALK_DEPTH) -> list[str]:
    """
    Walk directories looking for .uproject files, with depth limit and skip list.
    Slower than Everything but works everywhere.
    """
    found: list[str] = []
    for root in roots:
        root_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            # Depth check
            current_depth = dirpath.count(os.sep) - root_depth
            if current_depth >= max_depth:
                dirnames.clear()
                continue
            # Prune known-useless directories (in-place modification of dirnames)
            dirnames[:] = [
                d for d in dirnames
                if d.lower() not in _SKIP_DIRS
                and not d.startswith(".")
                and d.lower() not in _SKIP_ROOTS
            ]
            for f in filenames:
                if f.lower().endswith(".uproject"):
                    found.append(os.path.join(dirpath, f))
    return found


# ---------------------------------------------------------------------------
# Engine discovery
# ---------------------------------------------------------------------------

def _read_version_file(engine_root: Path) -> str:
    """Read Engine/Build/Build.version to get version string."""
    ver_file = engine_root / "Engine" / "Build" / "Build.version"
    if not ver_file.exists():
        return "unknown"
    try:
        data = json.loads(ver_file.read_text(encoding="utf-8"))
        major = data.get("MajorVersion", "?")
        minor = data.get("MinorVersion", "?")
        patch = data.get("PatchVersion", "0")
        return f"{major}.{minor}.{patch}"
    except Exception:
        return "unknown"


def _is_engine_dir(path: Path) -> bool:
    """Check if a path looks like a UE engine root."""
    return (path / "Engine" / "Build" / "Build.version").exists()


def discover_engines() -> list[EngineInfo]:
    """
    Find all UE engine installations via:
      1. HKCU registry (source builds)
      2. HKLM registry (Launcher installs)
      3. LauncherInstalled.dat
    """
    engines: dict[str, EngineInfo] = {}  # keyed by normalized path

    def _add(path_str: str, assoc: str, etype: str):
        p = Path(path_str).resolve()
        key = str(p).lower()
        if key not in engines and p.exists() and _is_engine_dir(p):
            version = _read_version_file(p)
            engines[key] = EngineInfo(
                path=str(p),
                version=version,
                engine_type=etype,
                association=assoc,
            )

    if sys.platform == "win32":
        try:
            import winreg
            # Source builds: HKCU
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"SOFTWARE\Epic Games\Unreal Engine\Builds",
                ) as key:
                    i = 0
                    while True:
                        try:
                            name, val, _ = winreg.EnumValue(key, i)
                            _add(val, name, "source")
                            i += 1
                        except OSError:
                            break
            except FileNotFoundError:
                pass

            # Launcher installs: HKLM
            try:
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\EpicGames\Unreal Engine",
                ) as base:
                    i = 0
                    while True:
                        try:
                            sub_name = winreg.EnumKey(base, i)
                            with winreg.OpenKey(base, sub_name) as sub:
                                try:
                                    val, _ = winreg.QueryValueEx(sub, "InstalledDirectory")
                                    _add(val, sub_name, "installed")
                                except FileNotFoundError:
                                    pass
                            i += 1
                        except OSError:
                            break
            except FileNotFoundError:
                pass
        except ImportError:
            pass  # not on Windows

    # LauncherInstalled.dat
    dat_paths = [
        Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
        / "Epic/UnrealEngineLauncher/LauncherInstalled.dat",
        Path.home() / "AppData/Local/EpicGamesLauncher/Saved/Data/LauncherInstalled.dat",
    ]
    for dat in dat_paths:
        if not dat.exists():
            continue
        try:
            data = json.loads(dat.read_text(encoding="utf-8"))
            for item in data.get("InstallationList", []):
                loc = item.get("InstallLocation", "")
                app = item.get("AppName", "")
                if loc and app.startswith("UE_"):
                    _add(loc, app.removeprefix("UE_"), "installed")
        except Exception:
            pass

    return sorted(engines.values(), key=lambda e: e.version, reverse=True)


# ---------------------------------------------------------------------------
# Project filtering
# ---------------------------------------------------------------------------

def _is_real_project(uproject_path: str, engine_roots: set[str]) -> tuple[bool, ProjectInfo | None]:
    """
    Classify a .uproject file.
    Returns (is_real, ProjectInfo_or_None).
    """
    p = Path(uproject_path)

    # Filter: path contains known junk directories
    parts_lower = [part.lower() for part in p.parts]
    if any(skip in parts_lower for skip in _SKIP_DIRS):
        return False, None

    # Filter: must exist and be readable JSON with EngineAssociation
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False, None

    if "EngineAssociation" not in raw:
        return False, None

    parent = p.parent
    assoc = raw.get("EngineAssociation", "")

    # Check directory markers
    has_source = (parent / "Source").is_dir()
    has_content = (parent / "Content").is_dir()
    has_plugins = (parent / "Plugins").is_dir()

    # Is this inside an engine installation?
    parent_str = str(parent.resolve()).lower()
    is_engine_sample = any(parent_str.startswith(er) for er in engine_roots)

    # Also detect by path containing Templates/, Samples/, FeaturePacks/
    # (catches unregistered engine installs too)
    if not is_engine_sample:
        if any(d in parts_lower for d in _ENGINE_INTERNAL_DIRS):
            is_engine_sample = True

    # Resolve engine path
    engine_path = None
    for ei in _cached_engines:
        if ei.association == assoc:
            engine_path = ei.path
            break

    modules = raw.get("Modules", [])

    info = ProjectInfo(
        name=p.stem,
        path=str(p),
        engine_association=assoc,
        engine_path=engine_path,
        has_source=has_source,
        has_content=has_content,
        has_plugins=has_plugins,
        is_engine_sample=is_engine_sample,
        module_count=len(modules),
    )
    return True, info


# Module-level cache for engine list (used during filtering)
_cached_engines: list[EngineInfo] = []


def discover_projects(engines: list[EngineInfo] | None = None) -> tuple[list[ProjectInfo], int, str, list[EngineInfo]]:
    """
    Find all real UE projects on the machine.
    Returns (projects, skipped_count, search_method, all_engines).
    The engine list may grow as unregistered engines are found via template paths.
    """
    global _cached_engines
    if engines is None:
        engines = discover_engines()
    _cached_engines = engines

    engine_roots = {str(Path(e.path).resolve()).lower() for e in engines}

    # Decide search strategy
    use_everything = _has_everything()
    search_method = "everything" if use_everything else "os_walk"

    # Gather all .uproject paths
    raw_paths: list[str] = []
    if use_everything:
        try:
            raw_paths = _search_everything("*.uproject")
        except RuntimeError:
            # es.exe failed, fall back
            use_everything = False
            search_method = "os_walk (everything failed)"

    if not use_everything:
        drives = _get_drive_letters()
        raw_paths = _walk_for_uprojects(drives)

    # Discover unregistered engines from template paths found
    known_map = {str(Path(e.path).resolve()).lower(): e for e in engines}
    engines = _discover_engines_from_paths(raw_paths, known_map)
    _cached_engines = engines
    engine_roots = {str(Path(e.path).resolve()).lower() for e in engines}

    # Filter and classify
    projects: list[ProjectInfo] = []
    skipped = 0
    seen_roots: set[str] = set()

    for path_str in raw_paths:
        is_real, info = _is_real_project(path_str, engine_roots)
        if not is_real:
            skipped += 1
            continue
        # Deduplicate by parent directory (normalized)
        root_key = str(Path(path_str).parent.resolve()).lower()
        if root_key in seen_roots:
            skipped += 1
            continue
        seen_roots.add(root_key)
        projects.append(info)  # type: ignore[arg-type]

    # Sort: non-engine-samples first, then alphabetically
    projects.sort(key=lambda p: (p.is_engine_sample, p.name.lower()))

    all_engines = sorted(engines, key=lambda e: e.version, reverse=True)
    return projects, skipped, search_method, all_engines


# ---------------------------------------------------------------------------
# Combined discovery
# ---------------------------------------------------------------------------

def _discover_engines_from_paths(uproject_paths: list[str], known_engines: dict[str, EngineInfo]) -> list[EngineInfo]:
    """
    Detect unregistered engine installations by examining .uproject paths
    that contain Templates/, Samples/, etc.
    """
    for path_str in uproject_paths:
        p = Path(path_str)
        parts_lower = [part.lower() for part in p.parts]
        for marker in _ENGINE_INTERNAL_DIRS:
            if marker in parts_lower:
                idx = parts_lower.index(marker)
                # Engine root is the parent of Templates/Samples/etc.
                candidate = Path(*p.parts[:idx])
                if _is_engine_dir(candidate):
                    key = str(candidate.resolve()).lower()
                    if key not in known_engines:
                        version = _read_version_file(candidate)
                        known_engines[key] = EngineInfo(
                            path=str(candidate.resolve()),
                            version=version,
                            engine_type="unregistered",
                            association="",
                        )
                break
    return list(known_engines.values())


def discover_all() -> DiscoverResult:
    """One-shot: find all engines and all projects."""
    result = DiscoverResult()
    try:
        result.engines = discover_engines()
    except Exception as e:
        result.errors.append(f"Engine discovery error: {e}")

    try:
        projects, skipped, method, all_engines = discover_projects(result.engines)
        result.engines = all_engines  # may include newly discovered unregistered engines
        result.projects = projects
        result.skipped_count = skipped
        result.search_method = method
    except Exception as e:
        result.errors.append(f"Project discovery error: {e}")

    return result
