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
        ok, detail = MODULE.queue_continue("/bin/true", self.thread_id, "continue")
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
        self.assertIn("A thread", script)

    def test_desktop_options_default_to_live_ui(self):
        args = MODULE.parse_args([])
        self.assertFalse(args.no_desktop_ui)
        self.assertEqual(args.inspector_port, 9229)
        self.assertEqual(args.desktop_unavailable_retry_ms, 30000)

    def test_missing_desktop_is_a_paused_condition(self):
        self.assertTrue(MODULE.desktop_unavailable("Desktop unavailable: Codex Desktop main process was not found"))
        self.assertTrue(MODULE.desktop_unavailable("Desktop inspector did not open on 127.0.0.1:9229"))
        self.assertFalse(MODULE.desktop_unavailable("Goal resume button is not visible"))

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


if __name__ == "__main__":
    unittest.main()
