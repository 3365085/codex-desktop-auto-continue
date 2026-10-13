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

    def test_recognizes_codes_logged_as_objects(self):
        for info, expected in (
            ({"response_stream_disconnected": {"http_status_code": 502}}, "response_stream_disconnected"),
            ({"http_connection_failed": {"http_status_code": None}}, "http_connection_failed"),
            ({"responseTooManyFailedAttempts": {"httpStatusCode": 429}}, "responseTooManyFailedAttempts"),
        ):
            self.assertEqual(
                MODULE.transient_reason({"message": "x", "codex_error_info": info}), expected
            )

    def test_rejects_client_errors_that_would_repeat(self):
        error = {
            "message": "unexpected status 401 Unauthorized",
            "codex_error_info": {"http_connection_failed": {"http_status_code": 401}},
        }
        self.assertIsNone(MODULE.transient_reason(error))

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

    def cli_failure(self, originator, **kwargs):
        state = MODULE.FileState(offset=0, thread_id=self.thread_id, originator=originator)
        record = {
            "ordinal": 8,
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "error": {"codex_error_info": "response_stream_disconnected"},
            },
        }
        return MODULE.event_action(record, state, path=self.path, **kwargs)

    def test_handles_interactive_cli_session_by_default(self):
        for originator in ("codex-tui", "codex_cli_rs"):
            action = self.cli_failure(originator, all_clients=False)
            self.assertIsNotNone(action)
            self.assertEqual(action.client, "cli")
            self.assertEqual(action.thread_id, self.thread_id)

    def test_desktop_only_ignores_cli_session(self):
        self.assertIsNone(self.cli_failure("codex-tui", all_clients=False, include_cli=False))

    def test_ignores_exec_session_unless_all_clients(self):
        self.assertIsNone(self.cli_failure("codex_exec", all_clients=False))
        self.assertEqual(self.cli_failure("codex_exec", all_clients=True).client, "cli")

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

    def run_cli_event(self, outcome, goal_status=None):
        records = [
            {
                "ordinal": 0,
                "type": "session_meta",
                "payload": {"id": self.thread_id, "originator": "codex-tui"},
            }
        ]
        if goal_status:
            records.append(
                {
                    "ordinal": 1,
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_goal_updated",
                        "goal": {"threadId": self.thread_id, "status": goal_status},
                    },
                }
            )
        records.append(
            {
                "ordinal": 2,
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "error": {"codex_error_info": "server_overloaded"},
                },
            }
        )
        self.write_records(records)
        output = io.StringIO()
        with redirect_stderr(output), patch.object(
            MODULE, "daemon_resume", return_value=outcome
        ) as daemon, patch.object(
            MODULE, "queue_continue", return_value=(True, "")
        ) as queue, patch.object(
            MODULE, "desktop_resume", side_effect=AssertionError("CLI must not use Desktop UI")
        ):
            code = MODULE.main(
                [
                    "--session-root",
                    str(self.root),
                    "--lock-file",
                    str(self.root / "watcher.lock"),
                    "--scan-existing",
                    "--goal-desktop-ui",
                    "--once",
                ]
            )
        self.assertEqual(code, 0)
        return daemon, queue, output.getvalue()

    def test_cli_goal_resumes_through_daemon_without_queue_message(self):
        daemon, queue, output = self.run_cli_event(("goal-resumed", ""), goal_status="blocked")
        daemon.assert_called_once()
        queue.assert_not_called()
        self.assertIn("resumed CLI Goal", output)

    def test_cli_ordinary_failure_queues_continue(self):
        daemon, queue, output = self.run_cli_event(("no-goal", ""))
        daemon.assert_called_once()
        queue.assert_called_once()
        self.assertIn("queued 'continue'", output)

    def test_cli_session_not_on_daemon_is_left_alone(self):
        daemon, queue, output = self.run_cli_event(
            ("not-loaded", "the session is not open on the shared app-server daemon")
        )
        queue.assert_not_called()
        self.assertIn("not continuing CLI session", output)

    def test_cli_waits_while_daemon_is_unavailable(self):
        daemon, queue, output = self.run_cli_event(("unavailable", "socket missing"))
        queue.assert_not_called()
        self.assertIn("app-server daemon is unavailable", output)

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
            MODULE, "daemon_resume", side_effect=AssertionError("Goal API must not run")
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

    def client(self, code, timeout_ms=10_000):
        return MODULE.AppServerClient([sys.executable, "-c", code], timeout_ms)

    def test_reads_json_rpc_response_from_real_pipe(self):
        # Several messages arrive in one write, as the real app-server does.
        code = (
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    m = json.loads(line)\n"
            "    if 'id' not in m: continue\n"
            "    r = {'thread': {'name': '测试'}} if m['method'] == 'thread/read' else {}\n"
            "    sys.stdout.write('not json\\n' + json.dumps({'method': 'note'}) + '\\n'\n"
            "                     + json.dumps({'id': m['id'], 'result': r}) + '\\n')\n"
            "    sys.stdout.flush()\n"
        )
        with self.client(code) as client:
            result = client.request("thread/read", {"threadId": "x"})
        self.assertEqual(result, {"thread": {"name": "测试"}})

    def test_json_rpc_read_times_out_on_silent_pipe(self):
        started = MODULE.time.monotonic()
        with self.assertRaises(MODULE.AppServerError) as raised:
            with self.client("import sys\nsys.stdin.read()\n", timeout_ms=300):
                pass
        self.assertIn("timed out", str(raised.exception))
        self.assertLess(MODULE.time.monotonic() - started, 5)

    def test_reports_app_server_exit(self):
        with self.assertRaises(MODULE.AppServerError) as raised:
            with self.client("pass\n"):
                pass
        self.assertIn("closed", str(raised.exception))

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


FAKE_APP_SERVER = r"""
import json, os, struct, sys
stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
log = open(os.environ["FAKE_APP_SERVER_LOG"], "a", encoding="utf-8")
log.write(json.dumps(sys.argv[1:]) + "\n")
loaded = os.environ.get("FAKE_LOADED", "")
goal = os.environ.get("FAKE_GOAL_STATUS", "")

def exact(n):
    data = stdin.read(n)
    if len(data) != n:
        sys.exit(0)
    return data

def frame(payload):
    data = json.dumps(payload).encode()
    header = bytes((0x81, len(data))) if len(data) < 126 else bytes((0x81, 126)) + struct.pack("!H", len(data))
    return header + data

header = b""
while not header.endswith(b"\r\n\r\n"):
    header += exact(1)
assert header.startswith(b"GET / HTTP/1.1"), header
if os.name != "nt":
    import select
    # The real daemon loses a frame sent before the 101 arrives.
    if select.select([stdin], [], [], 0.3)[0]:
        log.write(json.dumps({"early_frame": True}) + "\n")
stdout.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
stdout.write(bytes((0x89, 0)))  # a ping the client must answer
stdout.flush()
while True:
    first = exact(2)
    assert first[1] & 0x80, "client frames must be masked"
    length = first[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", exact(2))[0]
    mask = exact(4)
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(exact(length)))
    if first[0] & 0x0F == 0xA:
        log.write(json.dumps({"pong": True}) + "\n")
        continue
    message = json.loads(data)
    log.write(json.dumps(message) + "\n")
    log.flush()
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {"userAgent": "fake"}
    elif method == "thread/loaded/list":
        result = {"data": [loaded] if loaded else [], "nextCursor": None}
    elif method == "thread/goal/get":
        result = {"goal": {"status": goal} if goal else None}
    elif method == "thread/goal/set":
        result = {"goal": {"status": message["params"]["status"]}}
    else:
        result = {}
    # A notification ahead of the response, in the same write, like the real server.
    stdout.write(frame({"method": "thread/status/changed", "params": {}})
                 + frame({"id": message["id"], "result": result}))
    stdout.flush()
"""


class DaemonResumeTests(unittest.TestCase):
    thread_id = "11111111-2222-3333-4444-555555555555"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        script = self.root / "fake_app_server.py"
        script.write_text(FAKE_APP_SERVER, encoding="utf-8")
        if MODULE.IS_WINDOWS:
            self.codex = self.root / "codex.cmd"
            self.codex.write_text(
                f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
            )
        else:
            self.codex = self.root / "codex"
            self.codex.write_text(
                f"#!/bin/sh\nexec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8"
            )
            self.codex.chmod(0o755)
        self.log = self.root / "requests.log"
        self.env = patch.dict(MODULE.os.environ, {"FAKE_APP_SERVER_LOG": str(self.log)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def resume(self, loaded, goal_status):
        with patch.dict(
            MODULE.os.environ,
            {"FAKE_LOADED": loaded, "FAKE_GOAL_STATUS": goal_status},
        ):
            return MODULE.daemon_resume(str(self.codex), self.thread_id, timeout_ms=10_000)

    def requests(self):
        lines = self.log.read_text(encoding="utf-8").splitlines()
        messages = [json.loads(line) for line in lines[1:]]
        self.assertIn({"pong": True}, messages)
        self.assertNotIn({"early_frame": True}, messages)
        return json.loads(lines[0]), [m for m in messages if "pong" not in m]

    def test_reactivates_blocked_goal_on_daemon(self):
        self.assertEqual(self.resume(self.thread_id, "blocked"), ("goal-resumed", ""))
        argv, requests = self.requests()
        self.assertEqual(argv, ["app-server", "proxy"])
        goal_set = [r for r in requests if r.get("method") == "thread/goal/set"]
        self.assertEqual(
            goal_set[0]["params"],
            {"threadId": self.thread_id, "status": "active", "origin": "automatic"},
        )

    def test_does_not_touch_session_missing_from_daemon(self):
        outcome, _ = self.resume("", "blocked")
        self.assertEqual(outcome, "not-loaded")
        _, requests = self.requests()
        self.assertNotIn("thread/goal/set", [r.get("method") for r in requests])

    def test_leaves_paused_goal_alone(self):
        self.assertEqual(self.resume(self.thread_id, "paused"), ("goal-inactive", "Goal status is paused"))
        _, requests = self.requests()
        self.assertNotIn("thread/goal/set", [r.get("method") for r in requests])

    def test_reports_ordinary_thread(self):
        self.assertEqual(self.resume(self.thread_id, ""), ("no-goal", ""))

    def test_missing_codex_is_unavailable(self):
        outcome, _ = MODULE.daemon_resume(
            str(self.root / "missing-codex"), self.thread_id, timeout_ms=2000
        )
        self.assertEqual(outcome, "unavailable")


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
