"""`python -m deskpet` entry point."""

from __future__ import annotations

import argparse
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mochi desktop pet")
    parser.add_argument("--data-dir", type=Path, help="directory for the desktop pet's local data")
    parser.add_argument("--legacy", action="store_true", help="launch the original LLM pet")
    parser.add_argument("--config", help="configuration file for the original LLM pet")
    parser.add_argument("--configure-system2", action="store_true", help="open the Bailian API key settings")
    parser.add_argument("--request-screen-recording", action="store_true",
                        help="request macOS Screen Recording access once while launching Mochi")
    args = parser.parse_args(argv)
    if args.request_screen_recording and (args.legacy or args.config is not None or args.configure_system2):
        parser.error("--request-screen-recording requires a normal Mochi desktop launch")
    if args.legacy or args.config is not None:
        if args.data_dir is not None or args.configure_system2:
            parser.error("--data-dir and --configure-system2 are only available for the Mochi desktop pet")
        from .app import Application

        return Application(config_path=args.config).main()

    if args.configure_system2:
        from .c_ui.refine import configure_system2

        return configure_system2(args.data_dir)

    from .c_ui.refine import run_desktop

    return run_desktop(args.data_dir, request_screen_recording=args.request_screen_recording)


if __name__ == "__main__":
    raise SystemExit(main())
