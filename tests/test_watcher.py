import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "codex_desktop_auto_continue.py"
SPEC = importlib.util.spec_from_file_location("codex_desktop_auto_continue", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ClassificationTests(unittest.TestCase):
    def test_recognizes_structured_transient_codes(self):
        for code in MODULE.TRANSIENT_CODES:
            with self.subTest(code=code):
                error = {"message": "temporary failure", "codex_error_info": code}
                self.assertEqual(MODULE.transient_reason(error), code)

    def test_recognizes_capacity_message(self):
        error = {"message": "Selected model is at capacity. Please try a different model."}
        self.assertEqual(MODULE.transient_reason(error), "selected model is at capacity")

    def test_rejects_non_transient_error(self):
        error = {"message": "Invalid request", "codex_error_info": "bad_request"}
        self.assertIsNone(MODULE.transient_reason(error))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.thread_id = "11111111-2222-3333-4444-555555555555"
        self.path = self.root / f"rollout-2026-09-22T10-00-00-{self.thread_id}.jsonl"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_records(self, records):
        self.path.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )

    def test_reads_desktop_thread_header(self):
        self.write_records(
            [
                {
                    "ordinal": 0,
                    "type": "session_meta",
                    "payload": {
                        "id": self.thread_id,
                        "session_id": self.thread_id,
                        "originator": "Codex Desktop",
                    },
                }
            ]
        )
        self.assertEqual(
            MODULE.read_session_header(self.path),
            (self.thread_id, "Codex Desktop"),
        )

    def test_creates_same_thread_action_for_desktop_failure(self):
        state = MODULE.FileState(
            offset=0,
            thread_id=self.thread_id,
            originator="Codex Desktop",
        )
        record = {
            "ordinal": 7,
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "error": {
                    "message": "Selected model is at capacity.",
                    "codex_error_info": "server_overloaded",
                },
            },
        }
        action = MODULE.event_action(record, state, all_clients=False, path=self.path)
        self.assertIsNotNone(action)
        self.assertEqual(action.thread_id, self.thread_id)
        self.assertEqual(action.reason, "server_overloaded")
        self.assertIsNone(action.goal_active)

    def test_tracks_active_goal_and_marks_failure_for_goal_continuation(self):
        state = MODULE.FileState(
            offset=0,
            thread_id=self.thread_id,
            originator="Codex Desktop",
        )
        goal_event = {
            "ordinal": 6,
            "type": "event_msg",
            "payload": {
                "type": "thread_goal_updated",
                "goal": {"threadId": self.thread_id, "status": "active"},
            },
        }
        self.assertIsNone(
            MODULE.event_action(goal_event, state, all_clients=False, path=self.path)
        )
        failure = {
            "ordinal": 7,
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "error": {"codex_error_info": "server_overloaded"},
            },
        }
        action = MODULE.event_action(failure, state, all_clients=False, path=self.path)
        self.assertIsNotNone(action)
        self.assertTrue(action.goal_active)

    def test_does_not_continue_paused_goal(self):
        state = MODULE.FileState(
            offset=0,
            thread_id=self.thread_id,
            originator="Codex Desktop",
        )
        MODULE.event_action(
            {
                "ordinal": 6,
                "type": "event_msg",
                "payload": {
                    "type": "thread_goal_updated",
                    "goal": {"threadId": self.thread_id, "status": "paused"},
                },
            },
            state,
            all_clients=False,
            path=self.path,
        )
        action = MODULE.event_action(
            {
                "ordinal": 7,
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "error": {"codex_error_info": "server_overloaded"},
                },
            },
            state,
            all_clients=False,
            path=self.path,
        )
        self.assertIsNotNone(action)
        self.assertFalse(action.goal_active)

    def test_reactivates_blocked_goal_after_transient_failure(self):
        state = MODULE.FileState(
            offset=0,
            thread_id=self.thread_id,
            originator="Codex Desktop",
        )
        MODULE.event_action(
            {
                "ordinal": 10,
                "type": "event_msg",
                "payload": {
                    "type": "thread_goal_updated",
                    "goal": {"threadId": self.thread_id, "status": "blocked"},
                },
            },
            state,
            all_clients=False,
            path=self.path,
        )
        action = MODULE.event_action(
            {
                "ordinal": 11,
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "error": {"codex_error_info": "server_overloaded"},
                },
            },
            state,
            all_clients=False,
            path=self.path,
        )
        self.assertIsNotNone(action)
        self.assertTrue(action.goal_active)

    def test_ignores_cli_session_by_default(self):
        state = MODULE.FileState(offset=0, thread_id=self.thread_id, originator="codex_cli")
        record = {
            "ordinal": 8,
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "error": {"codex_error_info": "response_stream_disconnected"},
            },
        }
        self.assertIsNone(
            MODULE.event_action(record, state, all_clients=False, path=self.path)
        )

    def test_deduplicates_one_failure_event(self):
        state = MODULE.FileState(
            offset=0,
            thread_id=self.thread_id,
            originator="Codex Desktop",
        )
        record = {
            "ordinal": 9,
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "error": {"codex_error_info": "server_overloaded"},
            },
        }
        first = MODULE.event_action(record, state, all_clients=False, path=self.path)
        second = MODULE.event_action(record, state, all_clients=False, path=self.path)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_dry_run_processes_existing_failure_on_same_thread(self):
        self.write_records(
            [
                {
                    "ordinal": 0,
                    "type": "session_meta",
                    "payload": {
                        "id": self.thread_id,
                        "session_id": self.thread_id,
                        "originator": "Codex Desktop",
                    },
                },
                {
                    "ordinal": 1,
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "error": {
                            "message": "Selected model is at capacity.",
                            "codex_error_info": "server_overloaded",
                        },
                    },
                },
            ]
        )
        output = io.StringIO()
        lock_file = self.root / "watcher.lock"
        with redirect_stderr(output):
            status = MODULE.main(
                [
                    "--session-root",
                    str(self.root),
                    "--scan-existing",
                    "--dry-run",
                    "--once",
                    "--lock-file",
                    str(lock_file),
                ]
            )
        self.assertEqual(status, 0)
        self.assertIn(
            f"would inspect Goal status for {self.thread_id}; active => Goal continuation",
            output.getvalue(),
        )

    def test_queue_command_success_path(self):
        if MODULE.IS_WINDOWS:
            fake_codex = self.root / "codex.cmd"
            fake_codex.write_text("@exit /b 0\r\n", encoding="ascii")
            codex_bin = str(fake_codex)
        else:
            codex_bin = "/bin/true"
        ok, detail = MODULE.queue_continue(codex_bin, self.thread_id, "continue")
        self.assertTrue(ok)
        self.assertEqual(detail, "")

    def test_goal_continue_sets_active_goal_without_queue_message(self):
        class RecordingStream(io.StringIO):
            def close(self):
                self.closed_by_test = True

        class FakeProcess:
            def __init__(self):
                self.stdin = RecordingStream()
                self.stdout = object()
                self.terminated = False

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

            def kill(self):
                self.terminated = True

        process = FakeProcess()
        with patch.object(MODULE.subprocess, "Popen", return_value=process), patch.object(
            MODULE,
            "_read_json_rpc_response",
            side_effect=[(True, {}, ""), (True, {}, "")],
        ):
            ok, detail = MODULE.goal_continue(
                "/fake/codex",
                self.thread_id,
                goal_active=True,
                timeout_ms=1000,
            )
        self.assertTrue(ok)
        self.assertEqual(detail, "")
        self.assertIn('"method": "thread/goal/set"', process.stdin.getvalue())
        self.assertNotIn('"method": "thread/goal/get"', process.stdin.getvalue())

    def test_desktop_renderer_targets_official_buttons(self):
        script = MODULE._desktop_renderer_script("A thread", True, 700)
        self.assertIn("恢复目标", script)
        self.assertIn("暂停目标", script)
        self.assertIn("重试", script)
        self.assertIn("Continue", script)
        self.assertIn("continue", script)
        self.assertIn("A thread", script)
        self.assertIn("data-app-action-sidebar-thread-selected=\"true\"", script)
        self.assertIn("Could not restore the previously selected Desktop thread", script)
        self.assertIn("source.click()", script)

    def test_desktop_failure_does_not_fallback_to_queue_message(self):
        self.write_records(
            [
                {
                    "ordinal": 0,
                    "type": "session_meta",
                    "payload": {
                        "id": self.thread_id,
                        "session_id": self.thread_id,
                        "originator": "Codex Desktop",
                    },
                },
                {
                    "ordinal": 1,
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "error": {"codex_error_info": "server_overloaded"},
                    },
                },
            ]
        )
        output = io.StringIO()
        lock_file = self.root / "watcher.lock"
        with redirect_stderr(output), patch.object(
            MODULE, "desktop_resume", return_value=(False, "Desktop retry button is not visible")
        ) as resume, patch.object(MODULE, "queue_continue") as queue:
            status = MODULE.main(
                [
                    "--session-root",
                    str(self.root),
                    "--scan-existing",
                    "--once",
                    "--lock-file",
                    str(lock_file),
                ]
            )
        self.assertEqual(status, 0)
        resume.assert_called_once()
        queue.assert_not_called()
        self.assertIn("pausing this event", output.getvalue())
        self.assertNotIn("falling back", output.getvalue())

    def test_no_desktop_ui_queues_continue_without_goal_api(self):
        self.write_records(
            [
                {
                    "ordinal": 0,
                    "type": "session_meta",
                    "payload": {
                        "id": self.thread_id,
                        "session_id": self.thread_id,
                        "originator": "Codex Desktop",
                    },
                },
                {
                    "ordinal": 1,
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_goal_updated",
                        "goal": {"threadId": self.thread_id, "status": "active"},
                    },
                },
                {
                    "ordinal": 2,
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "error": {"codex_error_info": "server_overloaded"},
                    },
                },
            ]
        )
        output = io.StringIO()
        lock_file = self.root / "watcher.lock"
        with redirect_stderr(output), patch.object(
            MODULE, "goal_continue", side_effect=AssertionError("Goal API must not run")
        ) as goal_api, patch.object(
            MODULE, "queue_continue", return_value=(True, "")
        ) as queue:
            status = MODULE.main(
                [
                    "--session-root",
                    str(self.root),
                    "--scan-existing",
                    "--once",
                    "--no-desktop-ui",
                    "--lock-file",
                    str(lock_file),
                ]
            )
        self.assertEqual(status, 0)
        goal_api.assert_not_called()
        queue.assert_called_once()
        self.assertEqual(queue.call_args.args[1:], (self.thread_id, "continue"))
        self.assertIn("queued 'continue'", output.getvalue())

    def test_desktop_options_default_to_live_ui(self):
        args = MODULE.parse_args([])
        self.assertFalse(args.no_desktop_ui)
        self.assertFalse(args.goal_desktop_ui)
        self.assertEqual(args.inspector_port, 9229)
        self.assertEqual(args.desktop_unavailable_retry_ms, 30000)
        self.assertEqual(args.desktop_ui_retry_ms, 5000)

    def test_goal_desktop_mode_queues_ordinary_failure_without_ui(self):
        self.write_records(
            [
                {
                    "ordinal": 0,
                    "type": "session_meta",
                    "payload": {
                        "id": self.thread_id,
                        "session_id": self.thread_id,
                        "originator": "Codex Desktop",
                    },
                },
                {
                    "ordinal": 1,
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "error": {"codex_error_info": "server_overloaded"},
                    },
                },
            ]
        )
        output = io.StringIO()
        lock_file = self.root / "watcher.lock"
        with redirect_stderr(output), patch.object(
            MODULE, "desktop_resume", side_effect=AssertionError("ordinary failure must not use UI")
        ) as resume, patch.object(MODULE, "queue_continue", return_value=(True, "")) as queue:
            status = MODULE.main(
                [
                    "--session-root",
                    str(self.root),
                    "--scan-existing",
                    "--once",
                    "--goal-desktop-ui",
                    "--lock-file",
                    str(lock_file),
                ]
            )
        self.assertEqual(status, 0)
        resume.assert_not_called()
        queue.assert_called_once()
        self.assertEqual(queue.call_args.args[1:], (self.thread_id, "continue"))

    def test_goal_desktop_mode_uses_ui_for_active_goal(self):
        self.write_records(
            [
                {
                    "ordinal": 0,
                    "type": "session_meta",
                    "payload": {
                        "id": self.thread_id,
                        "session_id": self.thread_id,
                        "originator": "Codex Desktop",
                    },
                },
                {
                    "ordinal": 1,
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_goal_updated",
                        "goal": {"threadId": self.thread_id, "status": "active"},
                    },
                },
                {
                    "ordinal": 2,
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "error": {"codex_error_info": "server_overloaded"},
                    },
                },
            ]
        )
        output = io.StringIO()
        lock_file = self.root / "watcher.lock"
        with redirect_stderr(output), patch.object(
            MODULE, "desktop_resume", return_value=(True, "resume-goal")
        ) as resume, patch.object(MODULE, "queue_continue") as queue:
            status = MODULE.main(
                [
                    "--session-root",
                    str(self.root),
                    "--scan-existing",
                    "--once",
                    "--goal-desktop-ui",
                    "--lock-file",
                    str(lock_file),
                ]
            )
        self.assertEqual(status, 0)
        resume.assert_called_once()
        queue.assert_not_called()

    def test_missing_desktop_is_a_paused_condition(self):
        self.assertTrue(MODULE.desktop_unavailable("Desktop unavailable: Codex Desktop main process was not found"))
        self.assertTrue(MODULE.desktop_unavailable("Desktop inspector did not open on 127.0.0.1:9229"))
        self.assertFalse(MODULE.desktop_unavailable("Goal resume button is not visible"))

    def test_desktop_ui_missing_button_is_a_paused_condition(self):
        self.assertTrue(
            MODULE.desktop_ui_unavailable(
                "Desktop thread is not visible in the sidebar: A thread"
            )
        )
        self.assertTrue(MODULE.desktop_ui_unavailable("Goal resume button is not visible"))
        self.assertTrue(MODULE.desktop_ui_unavailable("Desktop retry button is not visible"))
        self.assertFalse(MODULE.desktop_ui_unavailable("Desktop inspector timed out"))

    def test_goal_switch_waits_for_idle_user(self):
        script = MODULE._desktop_renderer_script("Goal thread", True, 700)
        self.assertIn(MODULE.ALLOW_SWITCH_PLACEHOLDER, script)
        guard = script.index("if (sourceTitle !== title && !allowSwitch)")
        self.assertLess(guard, script.index("thread.click()"))
        self.assertTrue(
            MODULE.desktop_ui_unavailable(
                "Deferring: the user is active in another Desktop thread"
            )
        )

    def test_finds_chatgpt_main_in_single_string_cmdline(self):
        self.assertTrue(
            MODULE.is_chatgpt_main_command(
                "ChatGPT", b"/usr/lib/chatgpt/ChatGPT --disable-gpu --enable-logging=stderr"
            )
        )
        self.assertTrue(
            MODULE.is_chatgpt_main_command(
                "ChatGPT", b"/usr/lib/chatgpt/ChatGPT\0--disable-gpu\0"
            )
        )
        self.assertFalse(
            MODULE.is_chatgpt_main_command("ChatGPT", b"/usr/lib/chatgpt/ChatGPT --type=renderer")
        )


class WindowsDesktopProcessTests(unittest.TestCase):
    EXECUTABLES = ("Codex.exe", "ChatGPT.exe")
    APP = "C:\\Program Files\\WindowsApps\\OpenAI.Codex_26.1.0.0_x64__2p2nqsd0c76g0\\app"

    def test_picks_electron_main_over_bundled_cli_and_renderers(self):
        processes = [
            {
                "ProcessId": 300,
                "ParentProcessId": 100,
                "Name": "codex.exe",
                "ExecutablePath": self.APP + "\\resources\\codex.exe",
                "CommandLine": '"codex.exe" app-server',
            },
            {
                "ProcessId": 200,
                "ParentProcessId": 100,
                "Name": "Codex.exe",
                "ExecutablePath": self.APP + "\\Codex.exe",
                "CommandLine": '"Codex.exe" --type=renderer --lang=zh-CN',
            },
            {
                "ProcessId": 100,
                "ParentProcessId": 4,
                "Name": "Codex.exe",
                "ExecutablePath": self.APP + "\\Codex.exe",
                "CommandLine": '"' + self.APP + '\\Codex.exe"',
            },
        ]
        self.assertEqual(
            MODULE.select_windows_desktop_main_pid(processes, self.EXECUTABLES), 100
        )

    def test_prefers_process_with_renderer_children(self):
        processes = [
            {"ProcessId": 10, "ParentProcessId": 1, "Name": "Codex.exe", "CommandLine": "Codex.exe"},
            {"ProcessId": 20, "ParentProcessId": 1, "Name": "Codex.exe", "CommandLine": "Codex.exe"},
            {
                "ProcessId": 21,
                "ParentProcessId": 20,
                "Name": "Codex.exe",
                "CommandLine": "Codex.exe --type=gpu-process",
            },
        ]
        self.assertEqual(MODULE.select_windows_desktop_main_pid(processes, self.EXECUTABLES), 20)

    def test_returns_none_without_desktop(self):
        processes = [
            {"ProcessId": 5, "ParentProcessId": 1, "Name": "notepad.exe", "CommandLine": "notepad"},
            {
                "ProcessId": 6,
                "ParentProcessId": 1,
                "Name": "Codex.exe",
                "CommandLine": "Codex.exe --type=utility",
            },
        ]
        self.assertIsNone(MODULE.select_windows_desktop_main_pid(processes, self.EXECUTABLES))


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _spawn(self, code):
        return MODULE.subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=MODULE.subprocess.PIPE,
            stdout=MODULE.subprocess.PIPE,
            stderr=MODULE.subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

    def test_reads_json_rpc_response_from_real_pipe(self):
        process = self._spawn(
            "import json\n"
            "print('not json', flush=True)\n"
            "print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
            "print(json.dumps({'id': 2, 'result': {'thread': {'name': '测试'}}}), flush=True)\n"
            "input()\n"
        )
        try:
            ok, result, detail = MODULE._read_json_rpc_response(
                process.stdout, 2, MODULE.time.monotonic() + 10
            )
        finally:
            MODULE.stop_app_server(process)
        self.assertTrue(ok, detail)
        self.assertEqual(result, {"thread": {"name": "测试"}})

    def test_json_rpc_read_times_out_on_silent_pipe(self):
        process = self._spawn("input()\n")
        try:
            started = MODULE.time.monotonic()
            ok, _, detail = MODULE._read_json_rpc_response(process.stdout, 1, started + 0.3)
        finally:
            MODULE.stop_app_server(process)
        self.assertFalse(ok)
        self.assertIn("timed out", detail)
        self.assertLess(MODULE.time.monotonic() - started, 5)

    def test_second_watcher_cannot_take_lock(self):
        path = self.root / "nested" / "watcher.lock"
        first = MODULE.acquire_lock(path)
        try:
            with self.assertRaises(RuntimeError):
                MODULE.acquire_lock(path)
        finally:
            first.close()
        MODULE.acquire_lock(path).close()

    def test_default_lock_file_resolves(self):
        self.assertEqual(MODULE.default_lock_file().name, "watcher.lock")

    def test_log_file_receives_output(self):
        log_file = self.root / "logs" / "watcher.log"
        original = sys.stderr
        try:
            code = MODULE.main(
                [
                    "--session-root",
                    str(self.root / "sessions"),
                    "--lock-file",
                    str(self.root / "watcher.lock"),
                    "--log-file",
                    str(log_file),
                    "--once",
                    "--dry-run",
                ]
            )
        finally:
            if sys.stderr is not original:
                sys.stderr.close()
            sys.stderr = original
        self.assertEqual(code, 0)
        self.assertIn("[codex-auto-continue] watching", log_file.read_text(encoding="utf-8"))


@unittest.skipUnless(MODULE.IS_WINDOWS and MODULE.shutil.which("node"), "Windows with Node.js only")
class WindowsNodeInspectorTests(unittest.TestCase):
    """Electron's main process is Node; a plain node.exe exercises the same path."""

    def test_opens_inspector_on_running_node_process(self):
        port = 9339
        node = MODULE.subprocess.Popen(
            ["node", f"--inspect-port={port}", "-e", "setInterval(() => {}, 1000)"],
            stdin=MODULE.subprocess.DEVNULL,
            stdout=MODULE.subprocess.DEVNULL,
            stderr=MODULE.subprocess.DEVNULL,
        )
        try:
            MODULE.time.sleep(1)
            rows = MODULE.windows_desktop_processes(["node.exe"])
            self.assertIn(node.pid, [row.get("ProcessId") for row in rows])
            self.assertEqual(
                MODULE.select_windows_desktop_main_pid(
                    [row for row in rows if row.get("ProcessId") == node.pid], ["node.exe"]
                ),
                node.pid,
            )
            self.assertIsNone(MODULE.inspector_target(port, 250))
            MODULE.windows_enable_inspector(node.pid)
            deadline = MODULE.time.monotonic() + 10
            target = None
            while target is None and MODULE.time.monotonic() < deadline:
                target = MODULE.inspector_target(port, 250)
                MODULE.time.sleep(0.1)
            self.assertIsNotNone(target)
            websocket = MODULE.InspectorWebSocket(target, 5)
            try:
                websocket.send_json(
                    {
                        "id": 1,
                        "method": "Runtime.evaluate",
                        "params": {"expression": "1 + 41", "returnByValue": True},
                    }
                )
                response = websocket.recv_json()
                while response.get("id") != 1:
                    response = websocket.recv_json()
            finally:
                websocket.close()
            self.assertEqual(response["result"]["result"]["value"], 42)
        finally:
            node.kill()
            node.wait()


if __name__ == "__main__":
    unittest.main()
