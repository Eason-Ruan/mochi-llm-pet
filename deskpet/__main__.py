"""`python -m deskpet` entry point."""

from __future__ import annotations

import argparse
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mochi desktop pet")
    parser.add_argument("--data-dir", type=Path, help="directory for the desktop pet's local data")
    parser.add_argument("--legacy", action="store_true", help="launch the original LLM pet")
    parser.add_argument("--config", help="configuration file for the original LLM pet")
    args = parser.parse_args(argv)
    if args.legacy or args.config is not None:
        if args.data_dir is not None:
            parser.error("--data-dir is only available for the Mochi desktop pet")
        from .app import Application

        return Application(config_path=args.config).main()

    from .c_ui.refine import run_desktop

    return run_desktop(args.data_dir)


if __name__ == "__main__":
    raise SystemExit(main())
