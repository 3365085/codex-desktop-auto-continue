import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path


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
            f"would queue 'continue' on {self.thread_id} after server_overloaded",
            output.getvalue(),
        )

    def test_queue_command_success_path(self):
        ok, detail = MODULE.queue_continue("/bin/true", self.thread_id, "continue")
        self.assertTrue(ok)
        self.assertEqual(detail, "")


if __name__ == "__main__":
    unittest.main()
