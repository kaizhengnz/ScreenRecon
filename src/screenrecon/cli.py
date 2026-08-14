"""Argument parsing and subcommand routing (design doc 5.8)."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from . import __version__, config, ui

PROG = "screenrecon"

EPILOG = """\
examples:
  screenrecon                     watch the configured region
  screenrecon --configure         interactive setup with a drag-to-select picker
  screenrecon --mode log          watch using the 'log' prompt preset
  screenrecon ask "what is this"  capture once and ask a single question
  screenrecon ask                 capture once and start an interactive Q&A
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Watch a screen region; when the mouse dwells inside it, capture the region, "
            "send it to the AI, then print, archive and push the result to Telegram."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument(
        "--configure", action="store_true", help="run the interactive setup wizard"
    )
    parser.add_argument(
        "--screen",
        action="store_true",
        help="re-pick just the watched region (all other config fields are left alone)",
    )
    parser.add_argument(
        "--mode",
        metavar="NAME",
        help="use the named prompt preset from the 'prompts' config section",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        dest="config_path",
        # A literal path, not the expanded one: --help output is routinely pasted
        # into bug reports and should not disclose the user's home directory.
        help="use an alternative config file (default: ~/.config/screenrecon/config.json)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show a persistent red outline around the watched region (watch mode only)",
    )

    subparsers = parser.add_subparsers(dest="command")
    ask_parser = subparsers.add_parser(
        "ask",
        help="capture the region once and ask questions about it",
        description=(
            "Capture the configured region once, then answer questions about that "
            "screenshot. With no question argument, an interactive session starts."
        ),
    )
    ask_parser.add_argument(
        "question",
        nargs="*",
        help="the question to ask; omit it to start an interactive session",
    )
    return parser


def _configure_stdio() -> None:
    """Make stdout/stderr tolerant of terminals that cannot encode every character."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def _warn_about_redirected_endpoint() -> None:
    """Make a non-default API endpoint visible — screenshots and the key go there."""
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    if base_url:
        ui.warn(
            f"ANTHROPIC_BASE_URL is set: captures and your API key go to {base_url}, "
            "not to api.anthropic.com."
        )


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "ask" and args.configure:
        parser.error("the 'ask' subcommand cannot be combined with --configure")
    if args.mode and args.configure:
        parser.error("--mode only applies when watching or when using 'ask'")
    if args.screen and (args.configure or args.command == "ask" or args.mode or args.debug):
        parser.error("--screen sets only the watched region; use it on its own")

    # Imported lazily so that --help and --version never load mss/anthropic.
    from . import platform as cursor_platform
    from . import watcher

    cfg: dict[str, object] | None = None
    try:
        # Establish the coordinate space before mss is imported, so every entry
        # point (including 'ask', which never reads the cursor) agrees on what a
        # pixel is. Inside the try: it must not produce a raw traceback.
        cursor_platform.ensure_dpi_awareness()

        if args.configure:
            return config.run_wizard(args.config_path)
        if args.screen:
            return config.run_set_region(args.config_path)

        cfg = config.load(args.config_path)
        config.require_credentials(cfg)
        # Resolved for every path that reaches the API, so an unknown --mode is
        # always an error rather than a silently ignored flag.
        prompt = config.resolve_prompt(cfg, args.mode)
        _warn_about_redirected_endpoint()

        if args.command == "ask":
            # With --mode but no question, the preset is the question.
            question = " ".join(args.question).strip() or (prompt if args.mode else None)
            return watcher.run_ask(cfg, question)

        return watcher.run(cfg, prompt, debug=args.debug)

    except config.ConfigError as exc:
        ui.error(str(exc))
        return 1
    except KeyboardInterrupt:
        print()
        return 130
    except Exception as exc:
        # Last line of defence: a traceback here could be pasted into a public bug
        # report, and the frame locals of the call stack hold the credentials.
        secrets = (
            [str(cfg.get(field) or "") for field in config.CREDENTIAL_FIELDS] if cfg else []
        )
        ui.error(ui.scrub(f"Unexpected error: {type(exc).__name__}: {exc}", secrets))
        ui.error("This is a bug. The message above is safe to report; it contains no credentials.")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
