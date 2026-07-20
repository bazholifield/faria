"""Phase 0/1.5 CLI: `faria chat`, `faria patterns`, `faria migrate`.

Deliberately dumb: parse args, run migrations, drive SessionRunner, print.
All intelligence lives behind session/lifecycle.py so the Phase 2 server can
reuse it unchanged.
"""

from __future__ import annotations

import argparse
import getpass
import sys

import anthropic

from faria.config import Settings, load_settings
from faria.db.connection import connect
from faria.db.migrate import apply_migrations
from faria.db.repository import Repository
from faria.domain.levels import band
from faria.session.lifecycle import SessionRunner
from faria.tutor.brain import TutorBrain
from faria.tutor.prompts import language_name

HELP_TEXT = """Commands:
  /end   finish the session (writes the summary) and exit
  /quit  exit without a summary
  /help  show this help"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faria", description="Personal AI language tutor")
    sub = parser.add_subparsers(dest="command", required=True)

    chat = sub.add_parser("chat", help="Start a tutoring conversation")
    chat.add_argument(
        "--language",
        default=None,
        help=(
            "Target language code (es, fr, de, ar, eu, ...) to start practicing "
            "directly. Omit for companion mode: starts in your native language "
            "as home base, switch languages just by asking."
        ),
    )
    chat.add_argument("--goal", default=None, help="Optional session goal, given to the tutor")
    chat.add_argument("--name", default=None, help="Learner name (defaults to your OS username)")

    patterns = sub.add_parser("patterns", help="Show recurring error patterns for a language")
    patterns.add_argument("--language", required=True)
    patterns.add_argument("--name", default=None)
    patterns.add_argument("--limit", type=int, default=5)

    sub.add_parser("migrate", help="Apply pending database migrations and exit")

    args = parser.parse_args(argv)
    settings = load_settings()

    conn = connect(settings.db_path)
    applied = apply_migrations(conn)
    if applied:
        print(f"[faria] applied migrations: {', '.join(applied)}")

    if args.command == "migrate":
        print(f"[faria] database ready at {settings.db_path}")
        return 0
    if args.command == "patterns":
        return _show_patterns(args, settings, conn)

    return _run_chat(args, settings, conn)


def _run_chat(args: argparse.Namespace, settings: Settings, conn) -> int:
    repo = Repository(conn)
    brain = TutorBrain(settings.models)
    runner = SessionRunner(repo, brain, settings)

    name = args.name or getpass.getuser()
    ctx = runner.start(name, language=args.language, goal=args.goal)

    if args.language:
        mode_desc = f"{language_name(ctx.profile.language)} session"
    else:
        mode_desc = f"companion session ({language_name(ctx.profile.language)} home base)"
    level_line = ""
    if ctx.profile.level_overall is not None:
        level_line = (
            f" — est. level {band(ctx.profile.level_overall)} "
            f"(confidence {ctx.profile.level_confidence:.2f})"
        )
    print(f"[faria] {mode_desc} started{level_line}. {HELP_TEXT}\n")

    try:
        while True:
            try:
                user_text = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                user_text = "/end"

            if not user_text:
                continue
            if user_text == "/help":
                print(HELP_TEXT)
                continue
            if user_text == "/quit":
                runner.abandon()
                print("[faria] session abandoned (no summary).")
                return 0
            if user_text == "/end":
                return _end_session(runner)

            language_before = runner.active_language
            print("faria> ", end="", flush=True)
            for delta in runner.turn(user_text):
                print(delta, end="", flush=True)
            print()
            if runner.active_language != language_before:
                print(f"[faria — now speaking {language_name(runner.active_language)}]")
            print()
    except anthropic.AuthenticationError:
        runner.abandon()
        print(
            "\n[faria] Anthropic API authentication failed. Put a valid "
            "ANTHROPIC_API_KEY in .env (see .env.example).",
            file=sys.stderr,
        )
        return 1
    except TypeError as exc:
        # The SDK raises TypeError when no credential resolves at all.
        if "authentication" not in str(exc).lower():
            raise
        runner.abandon()
        print(
            "\n[faria] no Anthropic API key found. Copy .env.example to .env "
            "and set ANTHROPIC_API_KEY.",
            file=sys.stderr,
        )
        return 1
    except anthropic.APIConnectionError:
        runner.abandon()
        print("\n[faria] could not reach the Anthropic API (network?).", file=sys.stderr)
        return 1


def _end_session(runner: SessionRunner) -> int:
    print("[faria] wrapping up...")
    recap = runner.recap_queue
    summary = runner.end()

    if recap:
        print("\n[faria] a few things worth a second look:")
        for e in recap:
            if e.correction:
                print(f'  - "{e.span_text}" -> {e.correction}')
            elif e.explanation:
                print(f"  - {e.explanation}")

    if summary is None:
        print("[faria] nothing was said; no summary written.")
        return 0
    print(f"\n[faria] session summary:\n  {summary.summary_text}")
    if summary.next_focus:
        print("[faria] next time: " + "; ".join(summary.next_focus))
    return 0


def _show_patterns(args: argparse.Namespace, settings: Settings, conn) -> int:
    repo = Repository(conn)
    name = args.name or getpass.getuser()
    learner = repo.get_or_create_learner(name)
    profile = repo.get_or_create_profile(
        learner.id, args.language, settings.correction.default_aggressiveness,
        settings.correction.default_style,
    )
    patterns = repo.list_patterns(profile.id, limit=args.limit)
    if not patterns:
        print(f"[faria] no recurring error patterns logged yet for {language_name(profile.language)}.")
        return 0

    print(f"[faria] top error patterns for {language_name(profile.language)}:")
    for i, p in enumerate(patterns, start=1):
        print(f"{i}. {p.feature_key} [{p.status}] — seen {p.occurrence_count}x")
        for span_text, correction in repo.pattern_examples(p.id):
            arrow = f' -> "{correction}"' if correction else ""
            print(f'     "{span_text}"{arrow}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
