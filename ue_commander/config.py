"""
UE installation and project config detection.

Engine resolution priority:
  1. UE_ENGINE_PATH environment variable
  2. Windows Registry HKCU\\SOFTWARE\\Epic Games\\Unreal Engine\\Builds  (source builds / custom)
  3. Windows Registry HKLM\\SOFTWARE\\EpicGames\\Unreal Engine\\{version} (Launcher installs)
  4. Epic Launcher's LauncherInstalled.dat
  5. Common install paths scan

IDE build config detection:
  Reads Rider/CLion workspace.xml to find the active run configuration's
  CONFIGURATION and PLATFORM, so we never needlessly recompile.
"""

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

BuildConfig = Literal["Debug", "DebugGame", "Development", "Shipping", "Test"]
BuildPlatform = Literal["Win64", "Win32", "Mac", "Linux"]
BuildTarget = Literal["Editor", "Game", "Client", "Server"]

# Rider CONFIGURATION → (UBT config, UBT target)
_RIDER_CONFIG_MAP: dict[str, tuple[BuildConfig, BuildTarget]] = {
    "debug_editor":      ("Debug",        "Editor"),
    "debuggame_editor":  ("DebugGame",    "Editor"),
    "development_editor":("Development",  "Editor"),
    "test_editor":       ("Test",         "Editor"),
    "debug":             ("Debug",        "Game"),
    "debuggame":         ("DebugGame",    "Game"),
    "development":       ("Development",  "Game"),
    "shipping":          ("Shipping",     "Game"),
    "test":              ("Test",         "Game"),
}

# Rider PLATFORM → UBT platform
_RIDER_PLATFORM_MAP: dict[str, BuildPlatform] = {
    "x64":   "Win64",
    "x86":   "Win32",
    "win64":  "Win64",
    "win32":  "Win32",
    "mac":    "Mac",
    "linux":  "Linux",
}


@dataclass
class IDEBuildConfig:
    """Active build configuration detected from IDE workspace."""
    config: BuildConfig
    target: BuildTarget
    platform: BuildPlatform
    source: str  # e.g. "Rider workspace.xml" or "default"


@dataclass
class UEConfig:
    engine_path: Path
    project_path: Path
    project_name: str
    editor_exe: Path
    build_bat: Path
    ide_build: IDEBuildConfig | None = None
    plugin_names: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Engine path resolution
# ---------------------------------------------------------------------------

def _registry_source_build(guid: str) -> Path | None:
    """
    Source-built UE registers under:
    HKCU\\SOFTWARE\\Epic Games\\Unreal Engine\\Builds
    Each value name is the GUID, value data is the engine root path.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
        key_path = r"SOFTWARE\Epic Games\Unreal Engine\Builds"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            try:
                path, _ = winreg.QueryValueEx(key, guid)
                candidate = Path(path)
                if (candidate / "Engine/Binaries/Win64/UnrealEditor.exe").exists():
                    return candidate
            except FileNotFoundError:
                pass
    except Exception:
        pass
    return None


def _registry_launcher_install(association: str) -> Path | None:
    """
    Launcher-installed UE registers under:
    HKLM\\SOFTWARE\\EpicGames\\Unreal Engine\\{version}
    with value InstalledDirectory.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
        base = r"SOFTWARE\EpicGames\Unreal Engine"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            try:
                with winreg.OpenKey(root, association) as key:
                    path, _ = winreg.QueryValueEx(key, "InstalledDirectory")
                    return Path(path)
            except FileNotFoundError:
                pass
    except Exception:
        pass
    return None


def _launcher_installed_dat(association: str) -> Path | None:
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
                if not loc:
                    continue
                p = Path(loc)
                if app == f"UE_{association}":
                    return p
        except Exception:
            continue
    return None


def _scan_common_paths() -> list[Path]:
    roots = [
        Path("C:/Program Files/Epic Games"),
        Path("D:/Epic Games"),
        Path("E:/Epic Games"),
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Epic Games",
    ]
    found = []
    for root in roots:
        if not root.exists():
            continue
        for child in root.iterdir():
            if child.name.startswith("UE_") and (child / "Engine/Binaries/Win64/UnrealEditor.exe").exists():
                found.append(child)
    return sorted(found, key=lambda p: p.name)


def _resolve_engine(association: str) -> Path | None:
    # Env override always wins
    env = os.environ.get("UE_ENGINE_PATH")
    if env:
        return Path(env)

    is_guid = association.startswith("{") and association.endswith("}")

    if is_guid:
        p = _registry_source_build(association)
        if p:
            return p
    else:
        p = _registry_launcher_install(association)
        if p:
            return p
        p = _launcher_installed_dat(association)
        if p:
            return p

    # Fallback scan
    candidates = _scan_common_paths()
    if candidates:
        return candidates[-1]

    return None


# ---------------------------------------------------------------------------
# IDE build config detection
# ---------------------------------------------------------------------------

def _detect_rider_config(project_root: Path, project_name: str) -> IDEBuildConfig | None:
    """
    Parse Rider workspace.xml for the active run configuration.
    Rider stores UE run configs at:
      .idea/.idea.{ProjectName}/.idea/workspace.xml
    The selected config is in RunManager[@selected].
    """
    ws_path = project_root / ".idea" / f".idea.{project_name}" / ".idea" / "workspace.xml"
    if not ws_path.exists():
        return None

    try:
        tree = ET.parse(ws_path)
        root = tree.getroot()

        # Find RunManager component
        run_manager = None
        for comp in root.findall("component"):
            if comp.get("name") == "RunManager":
                run_manager = comp
                break
        if run_manager is None:
            return None

        # selected attribute: e.g. "C/C++ Project.OhMyUE"
        selected = run_manager.get("selected", "")
        selected_name = selected.split(".")[-1].strip() if "." in selected else selected

        # Find that configuration element
        active_cfg = None
        for cfg in run_manager.findall("configuration"):
            if cfg.get("name") == selected_name:
                active_cfg = cfg
                break
        if active_cfg is None:
            # Fall back to first CppProject config
            for cfg in run_manager.findall("configuration"):
                if cfg.get("type") == "CppProject":
                    active_cfg = cfg
                    break
        if active_cfg is None:
            return None

        # Extract CONFIGURATION and PLATFORM from the highest-numbered
        # configuration_N block — Rider uses the last one as the active config.
        raw_config = "Development_Editor"
        raw_platform = "x64"
        last_cfg_block = None
        for child in active_cfg:
            tag = child.tag or ""
            if tag.startswith("configuration_"):
                last_cfg_block = child
        if last_cfg_block is not None:
            for opt in last_cfg_block.findall("option"):
                name = opt.get("name", "")
                val = opt.get("value", "")
                if name == "CONFIGURATION":
                    raw_config = val
                elif name == "PLATFORM":
                    raw_platform = val

        ubt_config, ubt_target = _RIDER_CONFIG_MAP.get(
            raw_config.lower(), ("Development", "Editor")
        )
        ubt_platform = _RIDER_PLATFORM_MAP.get(raw_platform.lower(), "Win64")

        return IDEBuildConfig(
            config=ubt_config,
            target=ubt_target,
            platform=ubt_platform,
            source=f"Rider workspace.xml (config={raw_config}, platform={raw_platform})",
        )

    except Exception:
        return None


def _detect_vscode_config(project_root: Path) -> IDEBuildConfig | None:
    """
    Parse VS Code .vscode/tasks.json or c_cpp_properties.json for build config.
    Less reliable than Rider — treated as lower-priority fallback.
    """
    tasks_path = project_root / ".vscode" / "tasks.json"
    if not tasks_path.exists():
        return None
    try:
        data = json.loads(tasks_path.read_text(encoding="utf-8"))
        for task in data.get("tasks", []):
            args = task.get("args", [])
            # Look for a Build task with UBT arguments
            args_str = " ".join(args)
            config_match = re.search(
                r"\b(Debug|DebugGame|Development|Shipping|Test)(?:_?Editor)?\b", args_str
            )
            if config_match:
                raw = config_match.group(0)
                ubt_config, ubt_target = _RIDER_CONFIG_MAP.get(
                    raw.lower(), ("Development", "Editor")
                )
                return IDEBuildConfig(
                    config=ubt_config,
                    target=ubt_target,
                    platform="Win64",
                    source=f"VS Code tasks.json ({raw})",
                )
    except Exception:
        pass
    return None


def detect_ide_build_config(project_root: Path, project_name: str) -> IDEBuildConfig:
    """Detect active IDE build config, with sensible fallback."""
    cfg = _detect_rider_config(project_root, project_name)
    if cfg:
        return cfg
    cfg = _detect_vscode_config(project_root)
    if cfg:
        return cfg
    return IDEBuildConfig(
        config="Development",
        target="Editor",
        platform="Win64",
        source="default (no IDE config found)",
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def find_uproject(start: Path | None = None) -> Path:
    search = start or Path.cwd()
    for directory in [search, *search.parents]:
        matches = list(directory.glob("*.uproject"))
        if matches:
            return matches[0]
    env = os.environ.get("UE_PROJECT_PATH")
    if env:
        p = Path(env)
        if p.suffix == ".uproject" and p.exists():
            return p
        matches = list(p.glob("*.uproject"))
        if matches:
            return matches[0]
    raise RuntimeError(
        "No .uproject file found. Run from your project directory "
        "or set UE_PROJECT_PATH environment variable."
    )


def detect_config(uproject_path: Path) -> UEConfig:
    """
    Build full UEConfig from a .uproject file.
    Raises RuntimeError if engine cannot be located.
    """
    raw = json.loads(uproject_path.read_text(encoding="utf-8"))
    association: str = raw.get("EngineAssociation", "")
    plugin_names = [p["Name"] for p in raw.get("Plugins", [])]
    project_name = uproject_path.stem
    project_root = uproject_path.parent

    engine_path = _resolve_engine(association)
    if not engine_path:
        raise RuntimeError(
            f"Cannot locate Unreal Engine for association '{association}'.\n"
            "Options:\n"
            "  1. Set UE_ENGINE_PATH=<engine root> environment variable\n"
            "  2. Run UnrealVersionSelector.exe /register from your engine directory\n"
            "  3. Install UE via Epic Games Launcher"
        )

    build_bat = engine_path / "Engine/Build/BatchFiles/Build.bat"
    ide_build = detect_ide_build_config(project_root, project_name)

    # Pick the correct editor exe based on build config.
    # Development → UnrealEditor.exe
    # Other configs → UnrealEditor-{Platform}-{Config}.exe
    bin_dir = engine_path / "Engine/Binaries/Win64"
    if ide_build and ide_build.config != "Development":
        editor_exe = bin_dir / f"UnrealEditor-{ide_build.platform}-{ide_build.config}.exe"
        if not editor_exe.exists():
            # Fallback to default
            editor_exe = bin_dir / "UnrealEditor.exe"
    else:
        editor_exe = bin_dir / "UnrealEditor.exe"

    if not editor_exe.exists():
        raise RuntimeError(f"Editor executable not found: {editor_exe}")

    return UEConfig(
        engine_path=engine_path,
        project_path=uproject_path,
        project_name=project_name,
        editor_exe=editor_exe,
        build_bat=build_bat,
        ide_build=ide_build,
        plugin_names=plugin_names,
    )
