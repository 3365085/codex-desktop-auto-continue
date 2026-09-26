#!/usr/bin/env python3
"""Continue transiently failed Codex Desktop turns on the same thread."""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import select
import signal
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence, TextIO


VERSION = "0.3.3"
DEFAULT_DESKTOP_UNAVAILABLE_RETRY_MS = 30_000
DEFAULT_DESKTOP_UI_RETRY_MS = 5_000

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
    goal_status: str | None = None
    goal_seen: bool = False
    seen_events: set[str] = field(default_factory=set)


@dataclass
class PendingContinuation:
    event_key: str
    thread_id: str
    reason: str
    goal_active: bool | None = None
    attempts: int = 0
    next_attempt_at: float = 0.0
    desktop_unavailable_logged: bool = False
    desktop_ui_wait_logged: bool = False


class WebSocketError(RuntimeError):
    """The local Node inspector could not be used."""


class InspectorWebSocket:
    """Small dependency-free WebSocket client for the local Node inspector."""

    def __init__(self, url: str, timeout: float) -> None:
        match = re.match(r"^ws://([^/:]+):(\d+)(/.*)$", url)
        if not match:
            raise WebSocketError(f"unsupported inspector URL: {url}")
        self.host = match.group(1)
        self.port = int(match.group(2))
        self.path = match.group(3)
        self.timeout = timeout
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.sock.sendall(request)
        header = self._read_until(b"\r\n\r\n")
        if not header.startswith(b"HTTP/1.1 101"):
            self.close()
            raise WebSocketError("Node inspector WebSocket handshake failed")

    def _read_until(self, marker: bytes) -> bytes:
        data = bytearray()
        while marker not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("inspector closed during handshake")
            data.extend(chunk)
        return bytes(data)

    def send_json(self, payload: dict[str, object]) -> None:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
        length = len(data)
        if length < 126:
            header = bytes((0x81, 0x80 | length))
        elif length < 65536:
            header = bytes((0x81, 0x80 | 126)) + struct.pack("!H", length)
        else:
            header = bytes((0x81, 0x80 | 127)) + struct.pack("!Q", length)
        self.sock.sendall(header + mask + masked)

    def recv_json(self) -> dict[str, object]:
        first = self._recv_exact(2)
        opcode = first[0] & 0x0F
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if first[1] & 0x80 else b""
        data = self._recv_exact(length)
        if mask:
            data = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
        if opcode == 0x9:  # ping
            self._send_control(0xA, data)
            return self.recv_json()
        if opcode == 0x8:
            raise WebSocketError("inspector WebSocket closed")
        if opcode != 0x1:
            return self.recv_json()
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebSocketError("invalid inspector response") from exc
        if not isinstance(value, dict):
            raise WebSocketError("invalid inspector response object")
        return value

    def _recv_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise WebSocketError("inspector WebSocket closed")
            data.extend(chunk)
        return bytes(data)

    def _send_control(self, opcode: int, data: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
        self.sock.sendall(bytes((0x80 | opcode, 0x80 | len(data))) + mask + masked)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def default_lock_file() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime_dir) if runtime_dir else Path("/tmp") / f"codex-auto-continue-{os.getuid()}"
    return base / "watcher.lock"


def inspector_target(port: int, timeout_ms: int) -> str | None:
    """Return the Node inspector WebSocket URL, if the local port is open."""

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=timeout_ms / 1000.0
        ) as response:
            targets = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    if not isinstance(targets, list):
        return None
    for target in targets:
        if not isinstance(target, dict):
            continue
        url = target.get("webSocketDebuggerUrl")
        if isinstance(url, str) and url.startswith("ws://"):
            return url
    return None


def is_chatgpt_main_command(executable: str, raw_cmdline: bytes) -> bool:
    """Match both NUL-separated and single-string `/proc/cmdline` forms."""

    if executable != "ChatGPT":
        return False
    command_line = raw_cmdline.decode("utf-8", errors="replace").replace("\0", " ")
    return not re.search(r"(?:^|\s)--type=", command_line)


def chatgpt_main_pid() -> int | None:
    """Find the Electron main process without matching renderer children."""

    for entry in Path("/proc").glob("[0-9]*"):
        try:
            raw = (entry / "cmdline").read_bytes()
            executable = Path(os.readlink(entry / "exe")).name
        except OSError:
            continue
        if not is_chatgpt_main_command(executable, raw):
            continue
        # Most Linux processes expose NUL-separated argv here, but the ChatGPT
        # launcher used on this machine can expose one complete command string.
        # Inspect the executable symlink and the raw command text instead of
        # assuming a particular /proc cmdline representation.
        try:
            return int(entry.name)
        except ValueError:
            continue
    return None


def ensure_node_inspector(port: int, timeout_ms: int) -> tuple[str | None, bool, str]:
    """Open the loopback-only Electron inspector on demand when needed."""

    existing = inspector_target(port, timeout_ms)
    if existing:
        return existing, False, ""
    pid = chatgpt_main_pid()
    if pid is None:
        return None, False, "Codex Desktop main process was not found"
    try:
        os.kill(pid, signal.SIGUSR1)
    except OSError as exc:
        return None, False, f"could not enable Desktop inspector: {exc}"
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        target = inspector_target(port, min(timeout_ms, 250))
        if target:
            return target, True, ""
        time.sleep(0.05)
    return None, True, f"Desktop inspector did not open on 127.0.0.1:{port}"


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


def thread_name(codex_bin: str, thread_id: str, timeout_ms: int) -> tuple[bool, str, str]:
    """Read the Desktop display name without creating a turn."""

    command = [codex_bin, "app-server", "--listen", "stdio://"]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        return False, "", str(exc)

    assert process.stdin is not None
    assert process.stdout is not None
    deadline = time.monotonic() + timeout_ms / 1000.0
    try:
        initialize = {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "codex-desktop-auto-continue",
                    "title": "Codex Desktop Auto Continue",
                    "version": VERSION,
                },
                "capabilities": {"experimentalApi": True},
            },
        }
        if not _write_json_rpc(process.stdin, initialize):
            return False, "", "could not write initialize request"
        ok, _, detail = _read_json_rpc_response(process.stdout, 1, deadline)
        if not ok:
            return False, "", detail
        if not _write_json_rpc(process.stdin, {"method": "initialized", "params": {}}):
            return False, "", "could not write initialized notification"
        if not _write_json_rpc(
            process.stdin,
            {
                "id": 2,
                "method": "thread/read",
                "params": {"threadId": thread_id, "includeTurns": False},
            },
        ):
            return False, "", "could not write thread/read request"
        ok, result, detail = _read_json_rpc_response(process.stdout, 2, deadline)
        if not ok:
            return False, "", detail
        thread = result.get("thread") if isinstance(result, dict) else None
        name = thread.get("name") if isinstance(thread, dict) else None
        if not isinstance(name, str) or not name.strip():
            return False, "", "thread/read returned no Desktop thread name"
        return True, name, ""
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()


def _desktop_renderer_script(thread_title: str, prefer_goal: bool, wait_ms: int) -> str:
    title_literal = json.dumps(thread_title, ensure_ascii=False)
    goal_literal = "true" if prefer_goal else "false"
    return f"""
(async () => {{
  const title = {title_literal};
  const preferGoal = {goal_literal};
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (element) => !!element && !!element.offsetParent;
  const aria = (value) => [...document.querySelectorAll('[aria-label]')]
    .find((element) => visible(element) && element.getAttribute('aria-label') === value);
  const thread = aria(title);
  if (thread) {{
    thread.click();
    await wait({wait_ms});
  }} else if (!document.body.innerText.includes(title)) {{
    return {{ok: false, detail: 'Desktop thread is not visible in the sidebar: ' + title}};
  }}

  const restore = aria('恢复目标');
  if (restore) {{
    restore.click();
    return {{ok: true, action: 'resume-goal'}};
  }}
  const pause = aria('暂停目标');
  if (pause) {{
    return {{ok: true, action: 'goal-already-active'}};
  }}
  if (preferGoal) {{
    return {{ok: false, detail: 'Goal resume button is not visible'}};
  }}

  const retry = [...document.querySelectorAll('button')]
    .find((element) => visible(element) && ['重试', 'Retry', 'retry'].includes(element.innerText.trim()));
  if (retry) {{
    retry.click();
    return {{ok: true, action: 'retry-failed-turn'}};
  }}
  return {{ok: false, detail: 'Desktop retry button is not visible'}};
}})()
"""


def desktop_resume(
    codex_bin: str,
    thread_id: str,
    *,
    prefer_goal: bool,
    inspector_port: int,
    timeout_ms: int,
) -> tuple[bool, str]:
    """Use the same Desktop button the user would click, in the live UI."""

    target, _, detail = ensure_node_inspector(inspector_port, timeout_ms)
    if not target:
        return False, f"Desktop unavailable: {detail}"
    ok, title, detail = thread_name(codex_bin, thread_id, timeout_ms)
    if not ok:
        return False, detail
    try:
        websocket = InspectorWebSocket(target, timeout_ms / 1000.0)
        try:
            expression = (
                "(async()=>{"
                "const e=process.getBuiltinModule('module').createRequire(process.execPath)('electron');"
                "const windows=e.BrowserWindow.getAllWindows();"
                "const w=windows.find((candidate)=>candidate.isFocused())||windows[0];"
                "if(!w)return {ok:false,detail:'no Desktop BrowserWindow'};"
                "try{return await w.webContents.executeJavaScript("
                + json.dumps(_desktop_renderer_script(title, prefer_goal, 700), ensure_ascii=False)
                + ",true)}catch(error){return {ok:false,detail:String(error)}}"
                "})()"
            )
            websocket.send_json(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                }
            )
            deadline = time.monotonic() + timeout_ms / 1000.0
            while time.monotonic() < deadline:
                response = websocket.recv_json()
                if response.get("id") != 1:
                    continue
                result = response.get("result")
                if not isinstance(result, dict):
                    return False, "invalid Desktop inspector response"
                exception = result.get("exceptionDetails")
                if exception:
                    return False, str(exception.get("text") or "Desktop script failed")
                value = result.get("result")
                value = value.get("value") if isinstance(value, dict) else None
                if not isinstance(value, dict):
                    return False, "Desktop inspector returned no action result"
                if value.get("ok") is True:
                    return True, str(value.get("action") or "Desktop action completed")
                return False, str(value.get("detail") or "Desktop action was not completed")
            return False, "timed out waiting for Desktop inspector"
        finally:
            websocket.close()
    except (OSError, WebSocketError, ValueError) as exc:
        return False, str(exc)


def desktop_unavailable(detail: str) -> bool:
    """Identify a missing/starting Desktop without treating it as a fast failure."""

    folded = detail.casefold()
    return (
        folded.startswith("desktop unavailable:")
        or "main process was not found" in folded
        or "inspector did not open" in folded
    )


def desktop_ui_unavailable(detail: str) -> bool:
    """Identify a live Desktop UI that is not currently addressable.

    This is different from a formal turn failure: the event must remain pending
    until the same Desktop thread and its official button can be found. In
    particular, Electron may have the thread open but not render it in the
    virtualized sidebar yet.
    """

    folded = detail.casefold()
    return (
        "thread is not visible in the sidebar" in folded
        or "goal resume button is not visible" in folded
        or "desktop retry button is not visible" in folded
    )


def _write_json_rpc(stream: TextIO, message: dict[str, object]) -> bool:
    try:
        stream.write(json.dumps(message, ensure_ascii=False) + "\n")
        stream.flush()
    except (BrokenPipeError, OSError):
        return False
    return True


def _read_json_rpc_response(
    stream: TextIO,
    expected_id: int,
    deadline: float,
) -> tuple[bool, object | None, str]:
    """Read JSON-RPC messages until the response for expected_id arrives."""

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, None, "timed out waiting for app-server response"
        try:
            ready, _, _ = select.select([stream], [], [], remaining)
        except (OSError, ValueError) as exc:
            return False, None, str(exc)
        if not ready:
            return False, None, "timed out waiting for app-server response"
        line = stream.readline()
        if not line:
            return False, None, "app-server closed its protocol stream"
        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(response, dict) or response.get("id") != expected_id:
            continue
        error = response.get("error")
        if error is not None:
            if isinstance(error, dict):
                message = error.get("message") or error.get("code") or str(error)
            else:
                message = str(error)
            return False, None, f"app-server error: {message}"
        return True, response.get("result"), ""


def goal_continue(
    codex_bin: str,
    thread_id: str,
    *,
    goal_active: bool | None,
    timeout_ms: int,
) -> tuple[bool, str]:
    """Ask app-server to continue an existing active Goal without a user message.

    Setting an active or transiently blocked Goal to ``active`` invokes Codex's
    own Goal runtime continuation path. A transient turn failure can leave a
    Goal in ``blocked`` (shown as stalled in Desktop), and the Goal Continue
    control reactivates that state. When the log did not contain a recent Goal
    event, query the current status so paused, limited, and completed Goals are
    never resumed by accident.
    """

    command = [codex_bin, "app-server", "--listen", "stdio://"]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        return False, str(exc)

    assert process.stdin is not None
    assert process.stdout is not None
    deadline = time.monotonic() + timeout_ms / 1000.0
    try:
        initialize = {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "codex-desktop-auto-continue",
                    "title": "Codex Desktop Auto Continue",
                    "version": VERSION,
                },
                "capabilities": {"experimentalApi": True},
            },
        }
        if not _write_json_rpc(process.stdin, initialize):
            return False, "could not write initialize request"
        ok, _, detail = _read_json_rpc_response(process.stdout, 1, deadline)
        if not ok:
            return False, detail
        if not _write_json_rpc(process.stdin, {"method": "initialized", "params": {}}):
            return False, "could not write initialized notification"

        current_status: str | None = "active" if goal_active is True else None
        if current_status is None:
            if not _write_json_rpc(
                process.stdin,
                {
                    "id": 2,
                    "method": "thread/goal/get",
                    "params": {"threadId": thread_id},
                },
            ):
                return False, "could not write thread/goal/get request"
            ok, result, detail = _read_json_rpc_response(process.stdout, 2, deadline)
            if not ok:
                return False, detail
            goal = result.get("goal") if isinstance(result, dict) else None
            current_status = goal.get("status") if isinstance(goal, dict) else None
            if current_status not in {"active", "blocked"}:
                return False, f"Goal status is {current_status or 'none'}"

        request_id = 3 if goal_active is None else 2
        if not _write_json_rpc(
            process.stdin,
            {
                "id": request_id,
                "method": "thread/goal/set",
                "params": {"threadId": thread_id, "status": "active"},
            },
        ):
            return False, "could not write thread/goal/set request"
        ok, _, detail = _read_json_rpc_response(process.stdout, request_id, deadline)
        return (True, "") if ok else (False, detail)
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()


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
    if not isinstance(payload, dict):
        return None

    if payload.get("type") == "thread_goal_updated":
        goal = payload.get("goal")
        if isinstance(goal, dict):
            status = goal.get("status")
            state.goal_status = str(status) if status else None
            state.goal_seen = True
        return None

    if payload.get("type") != "task_complete":
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
    goal_active: bool | None
    if not state.goal_seen:
        goal_active = None
    else:
        goal_active = state.goal_status in {"active", "blocked"}
    return PendingContinuation(event_key, state.thread_id, reason, goal_active=goal_active)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Continue the same Codex Desktop thread after transient overload or "
            "response-stream failures by clicking the live Desktop retry/Goal button."
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
        help="Codex executable used for queue and Goal app-server calls.",
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
        "--app-server-timeout-ms",
        type=int,
        default=15_000,
        help="Timeout for Desktop metadata and inspector calls (default: 15000 ms).",
    )
    parser.add_argument(
        "--desktop-unavailable-retry-ms",
        type=int,
        default=DEFAULT_DESKTOP_UNAVAILABLE_RETRY_MS,
        help="Delay while Desktop is closed/unavailable (default: 30000 ms).",
    )
    parser.add_argument(
        "--desktop-ui-retry-ms",
        type=int,
        default=DEFAULT_DESKTOP_UI_RETRY_MS,
        help="Delay while the Desktop thread/button is not rendered (default: 5000 ms).",
    )
    parser.add_argument(
        "--inspector-port",
        type=int,
        default=9229,
        help="Loopback Node inspector port used for live Desktop buttons (default: 9229).",
    )
    parser.add_argument(
        "--no-desktop-ui",
        action="store_true",
        help="Disable live Desktop button control and use the legacy queue fallback.",
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
    for name in (
        "poll_ms",
        "queue_retry_ms",
        "app_server_timeout_ms",
        "desktop_unavailable_retry_ms",
        "desktop_ui_retry_ms",
    ):
        if getattr(args, name) < 10:
            parser.error(f"--{name.replace('_', '-')} must be at least 10")
    if args.inspector_port < 1 or args.inspector_port > 65535:
        parser.error("--inspector-port must be between 1 and 65535")
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
        f"message={args.message!r}; desktop_ui={not args.no_desktop_ui}; "
        f"dry_run={args.dry_run}",
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

                action.attempts += 1
                if action.goal_active is False:
                    print(
                        f"[codex-auto-continue] Goal is not active for {action.thread_id} "
                        f"after {action.reason}; leaving it paused/blocked",
                        file=sys.stderr,
                        flush=True,
                    )
                    del pending[event_key]
                    continue

                if args.dry_run:
                    if action.goal_active is True:
                        print(
                            f"[codex-auto-continue] dry-run: would click the Desktop Goal "
                            f"continuation button on {action.thread_id} after {action.reason}",
                            file=sys.stderr,
                            flush=True,
                        )
                    elif action.goal_active is None:
                        print(
                            f"[codex-auto-continue] dry-run: would inspect Goal status for "
                            f"{action.thread_id}; active => Goal continuation (click 恢复目标), "
                            f"no Goal => click 重试 (fallback: queue {args.message!r})",
                            file=sys.stderr,
                            flush=True,
                        )
                    del pending[event_key]
                    continue

                if not args.no_desktop_ui and action.goal_active is not False:
                    ok, detail = desktop_resume(
                        args.codex_bin,
                        action.thread_id,
                        prefer_goal=action.goal_active is True,
                        inspector_port=args.inspector_port,
                        timeout_ms=args.app_server_timeout_ms,
                    )
                    if ok:
                        continuation_counts[action.thread_id] = count + 1
                        print(
                            f"[codex-auto-continue] clicked Desktop {detail} on "
                            f"{action.thread_id} after {action.reason} "
                            f"({count + 1}/{args.max_attempts})",
                            file=sys.stderr,
                            flush=True,
                        )
                        del pending[event_key]
                        continue

                    if desktop_unavailable(detail):
                        action.attempts -= 1
                        action.next_attempt_at = (
                            now + args.desktop_unavailable_retry_ms / 1000.0
                        )
                        if not action.desktop_unavailable_logged:
                            print(
                                f"[codex-auto-continue] Desktop is unavailable for "
                                f"{action.thread_id}; pausing this event for "
                                f"{args.desktop_unavailable_retry_ms} ms: {detail}",
                                file=sys.stderr,
                                flush=True,
                            )
                            action.desktop_unavailable_logged = True
                        continue

                    if desktop_ui_unavailable(detail):
                        # This is a UI-readiness condition, not a retryable
                        # server failure. Keep the original event pending and
                        # never replace the official Desktop action with a new
                        # user message while the thread is not rendered.
                        action.attempts -= 1
                        action.next_attempt_at = now + args.desktop_ui_retry_ms / 1000.0
                        if not action.desktop_ui_wait_logged:
                            print(
                                f"[codex-auto-continue] Desktop thread/button is not "
                                f"currently visible for {action.thread_id}; pausing this "
                                f"event for {args.desktop_ui_retry_ms} ms: {detail}",
                                file=sys.stderr,
                                flush=True,
                            )
                            action.desktop_ui_wait_logged = True
                        continue

                    if action.goal_active is True:
                        action.next_attempt_at = now + args.desktop_ui_retry_ms / 1000.0
                        if not action.desktop_ui_wait_logged:
                            print(
                                f"[codex-auto-continue] Desktop Goal button was not usable for "
                                f"{action.thread_id}; pausing this event for "
                                f"{args.desktop_ui_retry_ms} ms without sending a new message: {detail}",
                                file=sys.stderr,
                                flush=True,
                            )
                            action.desktop_ui_wait_logged = True
                        continue

                    print(
                        f"[codex-auto-continue] Desktop retry button failed for "
                        f"{action.thread_id}; falling back to {args.message!r}: {detail}",
                        file=sys.stderr,
                        flush=True,
                    )

                elif action.goal_active is True and not args.no_desktop_ui:
                    action.next_attempt_at = now + args.queue_retry_ms / 1000.0
                    continue

                if action.goal_active is True and not args.no_desktop_ui:
                    # The live Goal control is the only safe way to resume a Goal.  A
                    # queue message would be a new user turn and could lose the Goal's
                    # stateful continuation context, so keep the event pending.
                    continue

                if action.goal_active is False:
                    del pending[event_key]
                    continue

                if action.goal_active is not False and args.no_desktop_ui:
                    operation = "Goal continuation"
                    ok, detail = goal_continue(
                        args.codex_bin,
                        action.thread_id,
                        goal_active=action.goal_active,
                        timeout_ms=args.app_server_timeout_ms,
                    )
                    if ok:
                        continuation_counts[action.thread_id] = count + 1
                        print(
                            f"[codex-auto-continue] requested legacy {operation} on "
                            f"{action.thread_id} after {action.reason} "
                            f"({count + 1}/{args.max_attempts})",
                            file=sys.stderr,
                            flush=True,
                        )
                        del pending[event_key]
                        continue

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
