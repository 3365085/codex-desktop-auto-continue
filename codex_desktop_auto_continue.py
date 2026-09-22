#!/usr/bin/env python3
"""Continue transiently failed Codex Desktop turns on the same thread."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence, TextIO


VERSION = "0.1.0"

TRANSIENT_CODES = frozenset(
    {
        "server_overloaded",
        "http_connection_failed",
        "response_stream_connection_failed",
        "response_stream_disconnected",
        "response_too_many_failed_attempts",
    }
)

TRANSIENT_TEXT = (
    "selected model is at capacity",
    "server overloaded",
    "server busy",
    "正在重新连接",
    "reconnecting",
    "response stream connection failed",
    "response stream disconnected",
)

THREAD_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)


@dataclass
class FileState:
    offset: int
    partial: bytes = b""
    thread_id: str | None = None
    originator: str | None = None
    seen_events: set[str] = field(default_factory=set)


@dataclass
class PendingContinuation:
    event_key: str
    thread_id: str
    reason: str
    attempts: int = 0
    next_attempt_at: float = 0.0


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def default_lock_file() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime_dir) if runtime_dir else Path("/tmp") / f"codex-auto-continue-{os.getuid()}"
    return base / "watcher.lock"


def clean_text(value: object) -> str:
    return str(value).casefold()


def transient_reason(error: object) -> str | None:
    if isinstance(error, dict):
        code = error.get("codex_error_info") or error.get("codexErrorInfo")
        if code and str(code).casefold() in TRANSIENT_CODES:
            return str(code)
        message = error.get("message", "")
        details = error.get("additional_details") or error.get("additionalDetails") or ""
        text = f"{message} {details}"
    else:
        text = str(error)

    folded = clean_text(text)
    for marker in TRANSIENT_TEXT:
        if marker.casefold() in folded:
            return marker
    return None


def thread_id_from_filename(path: Path) -> str | None:
    match = THREAD_ID_RE.search(path.name)
    return match.group(1) if match else None


def read_session_header(path: Path) -> tuple[str | None, str | None]:
    """Read the metadata line without scanning historical turn events."""

    try:
        with path.open("rb") as stream:
            line = stream.readline()
    except OSError:
        return None, None

    try:
        record = json.loads(line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return thread_id_from_filename(path), None

    payload = record.get("payload") if isinstance(record, dict) else None
    if isinstance(payload, dict):
        value = payload.get("id") or payload.get("thread_id") or payload.get("session_id")
        originator = payload.get("originator")
        return (
            str(value) if value else thread_id_from_filename(path),
            str(originator) if originator else None,
        )
    return thread_id_from_filename(path), None


def new_records(path: Path, state: FileState) -> Iterator[dict[str, object]]:
    try:
        size = path.stat().st_size
    except OSError:
        return

    if size < state.offset:
        state.offset = 0
        state.partial = b""

    try:
        with path.open("rb") as stream:
            stream.seek(state.offset)
            chunk = stream.read()
            state.offset = stream.tell()
    except OSError:
        return

    if not chunk:
        return

    data = state.partial + chunk
    lines = data.split(b"\n")
    state.partial = lines.pop()
    for raw in lines:
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def queue_continue(codex_bin: str, thread_id: str, message: str) -> tuple[bool, str]:
    command = [codex_bin, "queue", "--thread", thread_id, "--message", message]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return False, str(exc)

    detail = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0, detail


def acquire_lock(path: Path) -> TextIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(f"another watcher already holds {path}") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def event_action(
    record: dict[str, object],
    state: FileState,
    *,
    all_clients: bool,
    path: Path,
) -> PendingContinuation | None:
    if record.get("type") == "session_meta":
        payload = record.get("payload")
        if isinstance(payload, dict):
            candidate = payload.get("id") or payload.get("thread_id") or payload.get("session_id")
            if candidate:
                state.thread_id = str(candidate)
            originator = payload.get("originator")
            if originator:
                state.originator = str(originator)
        return None

    if record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "task_complete":
        return None

    reason = transient_reason(payload.get("error"))
    if reason is None or not state.thread_id:
        return None
    if not all_clients and state.originator != "Codex Desktop":
        return None

    event_key = f"{path}:{record.get('ordinal', state.offset)}"
    if event_key in state.seen_events:
        return None
    state.seen_events.add(event_key)
    return PendingContinuation(event_key, state.thread_id, reason)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Queue 'continue' on the same Codex Desktop thread after transient "
            "overload or response-stream failures."
        )
    )
    parser.add_argument(
        "--session-root",
        type=Path,
        default=default_codex_home() / "sessions",
        help="Codex JSONL session root (default: CODEX_HOME/sessions).",
    )
    parser.add_argument(
        "--codex-bin",
        default=shutil.which("codex") or "/usr/lib/chatgpt/resources/codex",
        help="Codex executable used for 'codex queue'.",
    )
    parser.add_argument("--message", default="continue", help="Message queued on the thread.")
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=10_000,
        help="Maximum successful continuations per thread (default: 10000).",
    )
    parser.add_argument(
        "--max-queue-attempts",
        type=int,
        default=10_000,
        help="Maximum local queue attempts for one failure event (default: 10000).",
    )
    parser.add_argument(
        "--poll-ms", type=int, default=100, help="Session polling interval (default: 100 ms)."
    )
    parser.add_argument(
        "--queue-retry-ms",
        type=int,
        default=250,
        help="Delay after a local queue failure (default: 250 ms).",
    )
    parser.add_argument(
        "--scan-existing",
        action="store_true",
        help="Process historical failures at startup; use with care.",
    )
    parser.add_argument(
        "--all-clients",
        action="store_true",
        help="Include sessions not created by Codex Desktop.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Log actions without queueing.")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit.")
    parser.add_argument("--lock-file", type=Path, default=default_lock_file())
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args(argv)
    for name in ("max_attempts", "max_queue_attempts"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")
    for name in ("poll_ms", "queue_retry_ms"):
        if getattr(args, name) < 10:
            parser.error(f"--{name.replace('_', '-')} must be at least 10")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = args.session_root.expanduser()
    states: dict[Path, FileState] = {}
    continuation_counts: dict[str, int] = {}
    pending: dict[str, PendingContinuation] = {}

    try:
        lock_handle = acquire_lock(args.lock_file.expanduser())
    except RuntimeError as exc:
        print(f"[codex-auto-continue] {exc}", file=sys.stderr, flush=True)
        return 1

    print(
        f"[codex-auto-continue] watching {root}; max_attempts={args.max_attempts}; "
        f"message={args.message!r}; dry_run={args.dry_run}",
        file=sys.stderr,
        flush=True,
    )

    try:
        while True:
            try:
                paths = sorted(root.rglob("rollout-*.jsonl"))
            except OSError:
                paths = []

            for path in paths:
                try:
                    size = path.stat().st_size
                except OSError:
                    continue

                if path not in states:
                    initial_offset = 0 if args.scan_existing else size
                    thread_id, originator = read_session_header(path)
                    states[path] = FileState(initial_offset, thread_id=thread_id, originator=originator)

                state = states[path]
                if state.thread_id is None:
                    state.thread_id, state.originator = read_session_header(path)

                for record in new_records(path, state):
                    action = event_action(
                        record,
                        state,
                        all_clients=args.all_clients,
                        path=path,
                    )
                    if action is not None:
                        pending[action.event_key] = action

            now = time.monotonic()
            for event_key, action in list(pending.items()):
                if action.next_attempt_at > now:
                    continue

                count = continuation_counts.get(action.thread_id, 0)
                if count >= args.max_attempts:
                    print(
                        f"[codex-auto-continue] exhausted {args.max_attempts} continuations "
                        f"for thread {action.thread_id}",
                        file=sys.stderr,
                        flush=True,
                    )
                    del pending[event_key]
                    continue

                if args.dry_run:
                    print(
                        f"[codex-auto-continue] dry-run: would queue {args.message!r} on "
                        f"{action.thread_id} after {action.reason}",
                        file=sys.stderr,
                        flush=True,
                    )
                    del pending[event_key]
                    continue

                action.attempts += 1
                ok, detail = queue_continue(args.codex_bin, action.thread_id, args.message)
                if ok:
                    continuation_counts[action.thread_id] = count + 1
                    print(
                        f"[codex-auto-continue] queued {args.message!r} on "
                        f"{action.thread_id} after {action.reason} "
                        f"({count + 1}/{args.max_attempts})",
                        file=sys.stderr,
                        flush=True,
                    )
                    del pending[event_key]
                elif action.attempts >= args.max_queue_attempts:
                    print(
                        f"[codex-auto-continue] queue failed {action.attempts} times for "
                        f"{action.thread_id}; giving up: {detail}",
                        file=sys.stderr,
                        flush=True,
                    )
                    del pending[event_key]
                else:
                    action.next_attempt_at = now + args.queue_retry_ms / 1000.0
                    if action.attempts == 1 or action.attempts % 100 == 0:
                        print(
                            f"[codex-auto-continue] queue attempt {action.attempts} failed for "
                            f"{action.thread_id}: {detail}",
                            file=sys.stderr,
                            flush=True,
                        )

            if args.once:
                return 0
            time.sleep(args.poll_ms / 1000.0)
    except KeyboardInterrupt:
        print("[codex-auto-continue] stopped", file=sys.stderr, flush=True)
        return 0
    finally:
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
