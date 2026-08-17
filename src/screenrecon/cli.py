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
  screenrecon --configure         first-time interactive setup (drag-to-select picker)
  screenrecon --show              print the current config (credentials masked)
  screenrecon --screen            re-pick just the watched region
  screenrecon --key               change just the API key (for the current provider)
  screenrecon --model             change just the AI model
  screenrecon --prompt            change just the default prompt (mini-wizard)
  screenrecon --prompt "..."      use "..." as the prompt for this run only
  screenrecon --dwell             change just the dwell seconds
  screenrecon --save-dir          change just the save directory
  screenrecon --telegram          change just the Telegram bot token + chat ID
  screenrecon --debug             watch and overlay a red outline on the region
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
        "--key",
        action="store_true",
        help="prompt for a new API key for the current provider (all other fields are left alone)",
    )
    parser.add_argument(
        "--model",
        action="store_true",
        help="pick a new AI model only (all other config fields are left alone)",
    )
    parser.add_argument(
        "--prompt",
        nargs="?",
        default=None,
        const=True,
        metavar="TEXT",
        help=(
            "with TEXT: use it as the system prompt for this run only "
            "(config is untouched); without TEXT: pick a new default prompt via the mini-wizard"
        ),
    )
    parser.add_argument(
        "--dwell",
        action="store_true",
        help="set dwell seconds only (all other config fields are left alone)",
    )
    parser.add_argument(
        "--save-dir",
        dest="save_dir",
        action="store_true",
        help="set the save directory only (all other config fields are left alone)",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="prompt for Telegram bot token and chat ID only (all other fields are left alone)",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="print the current config (credentials masked) and exit",
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


def _warn_about_redirected_endpoint(cfg: dict[str, object]) -> None:
    """Warn when the Anthropic SDK's env-var endpoint override is set.

    Only meaningful when the selected provider is Anthropic — the SDK reads
    ``ANTHROPIC_BASE_URL`` regardless of what we do, and captures + key end
    up somewhere other than ``api.anthropic.com``. Other providers have
    their own env-var conventions (or, for the openai_compatible path, the
    explicit ``base_url`` config field is already visible in ``--show``).
    """
    from . import vision

    if vision.get_provider(cfg).name != "anthropic":
        return
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

    # --prompt has three forms after nargs="?": None (not passed),
    # True (--prompt with no argument → mini-wizard), str (inline session prompt).
    inline_prompt = args.prompt if isinstance(args.prompt, str) else None
    prompt_is_setter = args.prompt is True

    if args.command == "ask" and args.configure:
        parser.error("the 'ask' subcommand cannot be combined with --configure")

    setter_flags = [
        name
        for name in ("screen", "key", "model", "dwell", "save_dir", "telegram")
        if getattr(args, name)
    ]
    if prompt_is_setter:
        setter_flags.append("prompt")

    if len(setter_flags) > 1:
        parser.error("the single-field setters are mutually exclusive; use them one at a time")
    if setter_flags and (
        args.configure or args.command == "ask" or inline_prompt is not None or args.debug
    ):
        parser.error(
            f"--{setter_flags[0].replace('_', '-')} sets one config field; use it on its own"
        )
    if args.show and (
        setter_flags
        or args.configure
        or args.command == "ask"
        or inline_prompt is not None
        or args.debug
    ):
        parser.error("--show only prints the current config; use it on its own")

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
        if args.key:
            return config.run_set_key(args.config_path)
        if args.model:
            return config.run_set_model(args.config_path)
        if prompt_is_setter:
            return config.run_set_prompt(args.config_path)
        if args.dwell:
            return config.run_set_dwell(args.config_path)
        if args.save_dir:
            return config.run_set_save_dir(args.config_path)
        if args.telegram:
            return config.run_set_telegram(args.config_path)
        if args.show:
            return config.run_show(args.config_path)

        cfg = config.load(args.config_path)
        config.require_credentials(cfg)
        prompt = inline_prompt or str(cfg["prompt"])
        _warn_about_redirected_endpoint(cfg)

        if args.command == "ask":
            # An inline --prompt with no positional question doubles as the question.
            question = " ".join(args.question).strip() or inline_prompt
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
