"""`python -m deskpet` entry point."""

from __future__ import annotations

import argparse
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mochi desktop pet")
    parser.add_argument("--data-dir", type=Path, help="directory for the desktop pet's local data")
    parser.add_argument("--legacy", action="store_true", help="launch the original LLM pet")
    parser.add_argument("--config", help="configuration file for the original LLM pet")
    setup = parser.add_mutually_exclusive_group()
    setup.add_argument("--configure-system2", action="store_true", help="open the System 2 model settings")
    setup.add_argument("--configure-models", action="store_true", help="configure System 2 and Observation models")
    parser.add_argument("--request-screen-recording", action="store_true",
                        help="request macOS Screen Recording access once while launching Mochi")
    terminal = parser.add_argument_group("terminal model setup")
    terminal.add_argument("--terminal", action="store_true", help="configure models in the terminal with hidden API key prompts")
    selection = terminal.add_mutually_exclusive_group()
    selection.add_argument("--role", choices=("system2", "observation", "both"), help="choose which models to configure")
    selection.add_argument("--skip-system2", dest="role", action="store_const", const="observation",
                           help="configure only Observation")
    selection.add_argument("--skip-observation-vision", dest="role", action="store_const", const="system2",
                           help="configure only System 2")
    terminal.add_argument("--system2-base-url", help="System 2 API base URL")
    terminal.add_argument("--system2-model", help="System 2 model identifier")
    terminal.add_argument("--observation-base-url", help="Observation API base URL")
    terminal.add_argument("--observation-model", help="Observation model identifier")
    terminal.add_argument("--share-activity", action=argparse.BooleanOptionalAction,
                          help="allow System 2 to receive published activity summaries")
    terminal.add_argument("--share-images", action=argparse.BooleanOptionalAction,
                          help="allow Observation vision to receive approved screenshots")
    args = parser.parse_args(argv)
    if args.terminal and not args.configure_models:
        parser.error("--terminal requires --configure-models")
    system2_options = (args.system2_base_url, args.system2_model, args.share_activity)
    observation_options = (args.observation_base_url, args.observation_model, args.share_images)
    has_system2_options = any(value is not None for value in system2_options)
    has_observation_options = any(value is not None for value in observation_options)
    if (args.role or has_system2_options or has_observation_options) and not args.terminal:
        parser.error("terminal model options require --configure-models --terminal")
    if args.role == "observation" and has_system2_options:
        parser.error("System 2 options require configuring System 2")
    if args.role == "system2" and has_observation_options:
        parser.error("Observation options require configuring Observation")
    configuring = args.configure_system2 or args.configure_models
    if args.request_screen_recording and (args.legacy or args.config is not None or configuring):
        parser.error("--request-screen-recording requires a normal Mochi desktop launch")
    if args.legacy or args.config is not None:
        if args.data_dir is not None or configuring:
            parser.error("--data-dir and model setup are only available for the Mochi desktop pet")
        from .app import Application

        return Application(config_path=args.config).main()

    if args.configure_models:
        if args.terminal:
            from .model_setup_cli import run_terminal_setup
            from .observation_v2.routes import SYSTEM2, VISION

            return run_terminal_setup(
                args.data_dir, role=args.role,
                overrides={SYSTEM2: system2_options, VISION: observation_options},
            )
        from .c_ui.refine import configure_models

        return configure_models(args.data_dir)

    if args.configure_system2:
        from .c_ui.refine import configure_system2

        return configure_system2(args.data_dir)

    from .c_ui.refine import run_desktop

    return run_desktop(args.data_dir, request_screen_recording=args.request_screen_recording)


if __name__ == "__main__":
    raise SystemExit(main())
