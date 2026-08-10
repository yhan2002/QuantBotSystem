import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import RUN_quantbot_version_switch as selector


class ActiveVersionTests(unittest.TestCase):
    def test_missing_file_defaults_to_unified_beta(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "active_version.local.json"
            self.assertEqual(
                selector.BETA,
                selector.read_selected_version(path),
            )

    def test_selected_version_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "active_version.local.json"
            selected = selector.save_selected_version(
                selector.BETA, path
            )

            self.assertEqual(selector.BETA, selected)
            self.assertEqual(
                selector.BETA,
                selector.read_selected_version(path),
            )
            self.assertEqual(
                selector.BETA,
                json.loads(path.read_text(encoding="utf-8"))["active_version"],
            )

    def test_legacy_beta_names_resolve_to_unified_beta(self):
        self.assertEqual(selector.BETA, selector.normalize_version("CLASSIC_BETA"))
        self.assertEqual(selector.BETA, selector.normalize_version("PORTFOLIO_BETA"))
        args = selector.build_parser().parse_args(["run", "CLASSIC_BETA"])
        self.assertEqual(selector.BETA, args.version)

    def test_invalid_selected_version_is_rejected(self):
        with self.assertRaises(selector.SelectorError):
            selector.normalize_version("experimental")
        with self.assertRaisesRegex(selector.argparse.ArgumentTypeError, "BETA"):
            selector.parse_version("experimental")


class ChildEnvironmentTests(unittest.TestCase):
    def test_portfolio_reuses_beta_private_config_and_market_database(self):
        environment = selector.build_child_environment(
            selector.BETA, {"PATH": "test-path"}
        )

        classic_dir = selector.CLASSIC_DATA_DIR
        portfolio_dir = selector.VERSION_SPECS[selector.BETA].directory
        self.assertEqual(
            str((classic_dir / "config.local.py").resolve()),
            environment["QUANTBOT_CONFIG_PATH"],
        )
        self.assertEqual(
            str((classic_dir / "data" / "market_data.sqlite").resolve()),
            environment["QUANTBOT_MARKET_DB_PATH"],
        )
        self.assertEqual(
            str((portfolio_dir / "data" / "backtest_exports").resolve()),
            environment["QUANTBOT_EXPORT_DIR"],
        )
        self.assertEqual(
            str((portfolio_dir / "data" / "state").resolve()),
            environment["QUANTBOT_STATE_DIR"],
        )
        self.assertEqual("2026.08.10", environment["QUANTBOT_BUILD"])

    def test_legacy_classic_alias_uses_unified_environment(self):
        inherited = {"PATH": "test-path"}
        for key in selector.PORTFOLIO_PATH_ENV_KEYS:
            inherited[key] = "should-not-leak"
        inherited["QUANTBOT_BUILD"] = "old-build"

        environment = selector.build_child_environment(
            selector.CLASSIC_BETA, inherited
        )

        for key in selector.PORTFOLIO_PATH_ENV_KEYS:
            self.assertIn(key, environment)
        self.assertEqual("2026.08.10", environment["QUANTBOT_BUILD"])
        self.assertEqual(selector.BETA, environment["QUANTBOT_VERSION"])


class PollerLockTests(unittest.TestCase):
    def test_common_lock_blocks_second_selector_poller(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / ".quantbot_selector.lock"
            first = selector.PollerLock(selector.CLASSIC_BETA, lock_path)
            second = selector.PollerLock(selector.PORTFOLIO_BETA, lock_path)
            first.acquire()
            try:
                with self.assertRaises(selector.SelectorError):
                    second.acquire()
            finally:
                first.release()

            self.assertFalse(lock_path.exists())

    def test_stop_waits_for_starting_manager_to_publish_child(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / ".quantbot_selector.lock"
            payload = {
                "schema_version": selector.LOCK_SCHEMA_VERSION,
                "token": "race-token",
                "version": selector.PORTFOLIO_BETA,
                "state": selector.LOCK_STATE_STARTING,
                "manager_pid": 101,
                "manager_identity": "manager-identity",
                "child_pid": None,
                "child_identity": None,
            }
            lock_path.write_text(json.dumps(payload), encoding="utf-8")
            alive = {101: True, 202: True}
            identities = {
                101: "manager-identity",
                202: "child-identity",
            }
            killed = []
            published = False

            def process_matches(pid, expected_identity):
                return bool(alive.get(pid)) and identities.get(pid) == expected_identity

            def publish_child(_seconds):
                nonlocal published
                if published:
                    return
                published = True
                payload.update(
                    {
                        "state": selector.LOCK_STATE_RUNNING,
                        "child_pid": 202,
                        "child_identity": "child-identity",
                    }
                )
                lock_path.write_text(json.dumps(payload), encoding="utf-8")

            def terminate_child(pid, _signal):
                killed.append(pid)
                alive[pid] = False
                alive[101] = False
                lock_path.unlink()

            with patch.object(selector, "LOCK_PATH", lock_path), patch.object(
                selector, "_process_matches", side_effect=process_matches
            ), patch.object(
                selector,
                "_is_process_running",
                side_effect=lambda pid: bool(alive.get(pid)),
            ), patch.object(
                selector.time, "sleep", side_effect=publish_child
            ), patch.object(
                selector.os, "kill", side_effect=terminate_child
            ):
                stopped = selector.stop_running(
                    timeout_seconds=0.1,
                    startup_wait_seconds=0.1,
                )

            self.assertTrue(stopped)
            self.assertEqual([202], killed)

    def test_stop_never_kills_starting_manager_without_child(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / ".quantbot_selector.lock"
            payload = {
                "schema_version": selector.LOCK_SCHEMA_VERSION,
                "token": "starting-token",
                "version": selector.CLASSIC_BETA,
                "state": selector.LOCK_STATE_STARTING,
                "manager_pid": 101,
                "manager_identity": "manager-identity",
                "child_pid": None,
                "child_identity": None,
            }
            lock_path.write_text(json.dumps(payload), encoding="utf-8")

            with patch.object(selector, "LOCK_PATH", lock_path), patch.object(
                selector,
                "_process_matches",
                side_effect=lambda pid, identity: (
                    pid == 101 and identity == "manager-identity"
                ),
            ), patch.object(
                selector, "_is_process_running", return_value=True
            ), patch.object(selector.os, "kill") as kill:
                with self.assertRaisesRegex(
                    selector.SelectorError,
                    "still starting",
                ):
                    selector.stop_running(startup_wait_seconds=0.0)

            kill.assert_not_called()

    def test_stop_refuses_reused_child_pid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / ".quantbot_selector.lock"
            payload = {
                "schema_version": selector.LOCK_SCHEMA_VERSION,
                "token": "reused-token",
                "version": selector.CLASSIC_BETA,
                "state": selector.LOCK_STATE_RUNNING,
                "manager_pid": 101,
                "manager_identity": "manager-identity",
                "child_pid": 202,
                "child_identity": "old-child-identity",
            }
            lock_path.write_text(json.dumps(payload), encoding="utf-8")

            def process_matches(pid, expected_identity):
                return pid == 101 and expected_identity == "manager-identity"

            with patch.object(selector, "LOCK_PATH", lock_path), patch.object(
                selector, "_process_matches", side_effect=process_matches
            ), patch.object(
                selector, "_is_process_running", return_value=True
            ), patch.object(selector.os, "kill") as kill:
                with self.assertRaisesRegex(
                    selector.SelectorError,
                    "identity changed",
                ):
                    selector.stop_running()

            kill.assert_not_called()

    def test_manager_stop_signal_terminates_child_before_lock_release(self):
        class FakeProcess:
            pid = 202

            def __init__(self):
                self.return_code = None
                self.terminated = False

            def poll(self):
                return self.return_code

            def wait(self, timeout=None):
                if timeout is None and not self.terminated:
                    raise selector._ManagerStopSignal(selector.signal.SIGTERM)
                return self.return_code

            def terminate(self):
                self.terminated = True
                self.return_code = -int(selector.signal.SIGTERM)

            def kill(self):
                self.terminated = True
                self.return_code = -9

        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / ".quantbot_selector.lock"
            process = FakeProcess()

            def process_identity(pid):
                if pid == os.getpid():
                    return "manager-identity"
                if pid == process.pid and process.poll() is None:
                    return "child-identity"
                return None

            with patch.object(selector, "LOCK_PATH", lock_path), patch.object(
                selector, "_process_identity", side_effect=process_identity
            ), patch.object(
                selector.subprocess, "Popen", return_value=process
            ):
                return_code = selector.run_version(selector.CLASSIC_BETA)

            self.assertEqual(
                128 + int(selector.signal.SIGTERM),
                return_code,
            )
            self.assertTrue(process.terminated)
            self.assertFalse(lock_path.exists())

    def test_manager_signal_during_popen_waits_for_child_handle(self):
        class FakeProcess:
            pid = 303

            def __init__(self):
                self.return_code = None
                self.terminated = False

            def poll(self):
                return self.return_code

            def wait(self, timeout=None):
                return self.return_code

            def terminate(self):
                self.terminated = True
                self.return_code = -int(selector.signal.SIGTERM)

            def kill(self):
                self.terminated = True
                self.return_code = -9

        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / ".quantbot_selector.lock"
            process = FakeProcess()
            signal_holder = {}

            @contextmanager
            def fake_signal_handlers():
                state = selector._ManagerSignalState()
                signal_holder["state"] = state
                yield state

            def signal_during_popen(*_args, **_kwargs):
                signal_holder["state"].handle(selector.signal.SIGTERM, None)
                return process

            with patch.object(selector, "LOCK_PATH", lock_path), patch.object(
                selector,
                "_process_identity",
                side_effect=lambda pid: (
                    "manager-identity" if pid == os.getpid() else "child-identity"
                ),
            ), patch.object(
                selector,
                "_manager_stop_signal_handlers",
                side_effect=fake_signal_handlers,
            ), patch.object(
                selector.subprocess,
                "Popen",
                side_effect=signal_during_popen,
            ):
                return_code = selector.run_version(selector.CLASSIC_BETA)

            self.assertEqual(
                128 + int(selector.signal.SIGTERM),
                return_code,
            )
            self.assertTrue(process.terminated)
            self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
