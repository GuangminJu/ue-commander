"""
CLI compile wrapper — single-line output for Claude Code.

Usage:
    python -m ue_commander.cli_compile [--config X] [--target X] [--platform X]
"""

import asyncio
import re
import sys
import time

G = "\033[92m"  # green
R = "\033[91m"  # red
Y = "\033[93m"  # yellow
C = "\033[96m"  # cyan
B = "\033[1m"   # bold
D = "\033[2m"   # dim
X = "\033[0m"   # reset

def fg(n): return f"\033[38;5;{n}m"
GRAD = [fg(27), fg(33), fg(39), fg(45), fg(51), fg(50), fg(49), fg(48), fg(47), fg(46)]


def _bar(pct):
    w = 25
    filled = int(w * pct / 100)
    s = ""
    for i in range(w):
        c = GRAD[min(int(i / w * len(GRAD)), len(GRAD) - 1)]
        s += f"{c}{'█' if i < filled else f'{D}░{X}'}"
    return s + X


def _time(s):
    if s < 60: return f"{s:.0f}s"
    m, sec = divmod(int(s), 60)
    return f"{m}m{sec:02d}s"


async def main():
    from .config import find_uproject, detect_config

    cfg = detect_config(find_uproject())
    ide = cfg.ide_build

    args = sys.argv[1:]
    config = ide.config if ide else "Development"
    target = ide.target if ide else "Editor"
    platform = ide.platform if ide else "Win64"

    i = 0
    while i < len(args):
        if args[i] == "--config" and i + 1 < len(args):
            config = args[i + 1]; i += 2
        elif args[i] == "--target" and i + 1 < len(args):
            target = args[i + 1]; i += 2
        elif args[i] == "--platform" and i + 1 < len(args):
            platform = args[i + 1]; i += 2
        else:
            i += 1

    tgt = f"{cfg.project_name}{target}" if target != "Game" else cfg.project_name
    pe = f'\\"{cfg.project_path}\\"'
    cmd = " ".join([
        str(cfg.build_bat),
        f'-Target="{tgt} {platform} {config} -Project={pe}"',
        f'-Target="ShaderCompileWorker {platform} Development -Project={pe} -Quiet"',
        "-WaitMutex", "-FromMsBuild",
    ] + ([f"-architecture={'x64' if platform == 'Win64' else 'x86'}"] if platform in ("Win64", "Win32") else []))

    start = time.time()
    cre = re.compile(r"\[(\d+)/(\d+)\]")
    ere = re.compile(r"\(\d+\)\s*:\s*(error|fatal error)\s+", re.IGNORECASE)
    ere2 = re.compile(r"error\s+[A-Z]\d+:", re.IGNORECASE)

    lines, errors, warnings = [], [], []
    last_file = ""
    last_pct = 0
    total_files = 0

    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )

    assert proc.stdout
    async for raw in proc.stdout:
        ln = raw.decode("utf-8", errors="replace").rstrip()
        lines.append(ln)
        if ere.search(ln) or ere2.search(ln): errors.append(ln)
        elif re.search(r"warning\s+[A-Z]\d+:", ln, re.IGNORECASE): warnings.append(ln)
        m = cre.search(ln)
        if m:
            cur, tot = int(m.group(1)), int(m.group(2))
            last_pct = int(cur * 100 / tot) if tot else 0
            total_files = tot
            f = ln.split("]")[-1].strip().replace("\\", "/").split("/")[-1] if "]" in ln else ""
            last_file = f
        elif "Targets are up to date" in ln:
            last_pct = 100
            last_file = "up to date"

    rc = await proc.wait()
    elapsed = time.time() - start

    # === Single output line ===
    if rc == 0:
        w = f" {Y}⚠{len(warnings)}{X}" if warnings else ""
        files_info = f" {D}({total_files} files){X}" if total_files else ""
        print(f"{G}✅ {B}{tgt}{X} {_bar(100)} {G}{B}OK{X} {D}{_time(elapsed)}{X}{files_info}{w}")
    else:
        print(f"{R}❌ {B}{tgt}{X} {_bar(last_pct)} {R}{B}FAILED{X} {D}{_time(elapsed)}{X}")
        for e in errors[:6]:
            print(f"  {R}●{X} {e[:140]}")
        if not errors:
            for ln in lines[-4:]:
                print(f"  {D}{ln[:140]}{X}")

    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
