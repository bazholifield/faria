"""Phase 0 CLI: `faria chat --language es`, `faria migrate`.

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
    chat.add_argument("--language", required=True, help="Target language code (es, fr, de, ar, eu, ...)")
    chat.add_argument("--goal", default=None, help="Optional session goal, given to the tutor")
    chat.add_argument("--name", default=None, help="Learner name (defaults to your OS username)")

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

    return _run_chat(args, settings, conn)


def _run_chat(args: argparse.Namespace, settings: Settings, conn) -> int:
    repo = Repository(conn)
    brain = TutorBrain(settings.models)
    runner = SessionRunner(repo, brain, settings)

    name = args.name or getpass.getuser()
    ctx = runner.start(args.language, learner_name=name, goal=args.goal)

    print(f"[faria] {language_name(ctx.profile.language)} session started. {HELP_TEXT}\n")

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

            print("faria> ", end="", flush=True)
            for delta in runner.turn(user_text):
                print(delta, end="", flush=True)
            print("\n")
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
    summary = runner.end()
    if summary is None:
        print("[faria] nothing was said; no summary written.")
        return 0
    print(f"\n[faria] session summary:\n  {summary.summary_text}")
    if summary.next_focus:
        print("[faria] next time: " + "; ".join(summary.next_focus))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
