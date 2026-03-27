"""Entry point: runs the MCP server over stdio."""
import sys


def main():
    # Quick sanity check before loading MCP machinery
    try:
        from .config import find_uproject, detect_config
        uproject = find_uproject()
        cfg = detect_config(uproject)
        print(
            f"[ue-commander] Project: {cfg.project_name}  "
            f"Engine: {cfg.engine_path}  "
            f"IDE config: {cfg.ide_build.config if cfg.ide_build else 'unknown'}",
            file=sys.stderr,
        )
    except RuntimeError as e:
        print(f"[ue-commander] WARNING: {e}", file=sys.stderr)
        print("[ue-commander] Server will start but tools may fail until config is resolved.", file=sys.stderr)

    from .server import mcp
    mcp.run()


if __name__ == "__main__":
    main()
