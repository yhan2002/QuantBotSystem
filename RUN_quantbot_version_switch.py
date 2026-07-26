"""Select and launch one QuantBot Beta line as an isolated subprocess.

Examples:
    python RUN_quantbot_version_switch.py status
    python RUN_quantbot_version_switch.py select PORTFOLIO_BETA
    python RUN_quantbot_version_switch.py run
    python RUN_quantbot_version_switch.py stop

The launcher intentionally does not import either bot package.  A single
repository-level lock prevents two launcher-managed Telegram pollers from
running at the same time.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


CLASSIC_BETA = "CLASSIC_BETA"
PORTFOLIO_BETA = "PORTFOLIO_BETA"
VALID_VERSIONS = (CLASSIC_BETA, PORTFOLIO_BETA)

ROOT_DIR = Path(__file__).resolve().parent
ACTIVE_VERSION_PATH = ROOT_DIR / "active_version.local.json"
LOCK_PATH = ROOT_DIR / ".quantbot_selector.lock"
LOCK_SCHEMA_VERSION = 2
LOCK_STATE_STARTING = "STARTING"
LOCK_STATE_RUNNING = "RUNNING"
LOCK_STATES = {
    LOCK_STATE_STARTING,
    LOCK_STATE_RUNNING,
}
STARTUP_PID_WAIT_SECONDS = 2.0
PROCESS_IDENTITY_WAIT_SECONDS = 0.5
LOCK_POLL_SECONDS = 0.05


class SelectorError(RuntimeError):
    """Raised for safe, user-actionable launcher failures."""


class _ManagerStopSignal(BaseException):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = int(signum)


class _ManagerSignalState:
    def __init__(self):
        self.requested_signum: int | None = None
        self.interruptible = False

    def handle(self, signum, _frame):
        self.requested_signum = int(signum)
        if self.interruptible:
            raise _ManagerStopSignal(signum)

    def checkpoint(self):
        if self.requested_signum is not None:
            raise _ManagerStopSignal(self.requested_signum)


@dataclass(frozen=True)
class VersionSpec:
    name: str
    directory: Path
    entrypoint: Path
    build: str | None = None


VERSION_SPECS = {
    CLASSIC_BETA: VersionSpec(
        name=CLASSIC_BETA,
        directory=ROOT_DIR / "QuantBotSystem_Beta",
        entrypoint=ROOT_DIR
        / "QuantBotSystem_Beta"
        / "RUN_telegram_bot_server.py",
    ),
    PORTFOLIO_BETA: VersionSpec(
        name=PORTFOLIO_BETA,
        directory=ROOT_DIR / "QuantBotSystem_Portfolio_Beta",
        entrypoint=ROOT_DIR
        / "QuantBotSystem_Portfolio_Beta"
        / "RUN_telegram_bot_server.py",
        build="2026.07.24",
    ),
}

PORTFOLIO_PATH_ENV_KEYS = (
    "QUANTBOT_CONFIG_PATH",
    "QUANTBOT_DATA_DIR",
    "QUANTBOT_MARKET_DB_PATH",
    "QUANTBOT_RUNTIME_DIR",
    "QUANTBOT_EXPORT_DIR",
    "QUANTBOT_STATE_DIR",
)


def normalize_version(value: str) -> str:
    version = str(value).strip().upper()
    if version not in VALID_VERSIONS:
        choices = ", ".join(VALID_VERSIONS)
        raise SelectorError(f"Unknown version {value!r}. Choose one of: {choices}")
    return version


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def read_selected_version(path: Path | None = None) -> str:
    target = ACTIVE_VERSION_PATH if path is None else Path(path)
    if not target.exists():
        return CLASSIC_BETA
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        return normalize_version(payload["active_version"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SelectorError(f"Invalid active version file: {target}") from exc


def save_selected_version(version: str, path: Path | None = None) -> str:
    selected = normalize_version(version)
    target = ACTIVE_VERSION_PATH if path is None else Path(path)
    _atomic_write_json(
        target,
        {
            "active_version": selected,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )
    return selected


def _normalize_pid(pid: object) -> int | None:
    try:
        process_id = int(pid)
    except (TypeError, ValueError):
        return None
    if process_id <= 0:
        return None
    return process_id


def _windows_process_info(process_id: int) -> tuple[bool, str | None]:
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = (
            ("low", wintypes.DWORD),
            ("high", wintypes.DWORD),
        )

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        process_query_limited_information | synchronize,
        False,
        process_id,
    )
    if not handle:
        return False, None
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False, None
        if exit_code.value != 259:  # STILL_ACTIVE
            return False, None

        creation = FileTime()
        exit_time = FileTime()
        kernel_time = FileTime()
        user_time = FileTime()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return True, None
        creation_ticks = (int(creation.high) << 32) | int(creation.low)
        return True, f"windows-filetime:{creation_ticks}"
    finally:
        kernel32.CloseHandle(handle)


def _is_process_running(pid: object) -> bool:
    process_id = _normalize_pid(pid)
    if process_id is None:
        return False

    if os.name == "nt":
        running, _ = _windows_process_info(process_id)
        return running

    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_identity(pid: object) -> str | None:
    """Return an OS process creation identity, not merely a reusable PID."""
    process_id = _normalize_pid(pid)
    if process_id is None:
        return None

    if os.name == "nt":
        running, identity = _windows_process_info(process_id)
        return identity if running else None

    proc_stat_path = Path(f"/proc/{process_id}/stat")
    try:
        stat_text = proc_stat_path.read_text(encoding="utf-8")
    except OSError:
        return None
    closing_paren = stat_text.rfind(")")
    if closing_paren < 0:
        return None
    fields_after_name = stat_text[closing_paren + 1 :].split()
    if len(fields_after_name) <= 19:
        return None
    return f"proc-start-ticks:{fields_after_name[19]}"


def _process_matches(pid: object, expected_identity: object) -> bool:
    if not expected_identity:
        return False
    return _process_identity(pid) == str(expected_identity)


def _role_is_live_for_lock(
    info: Mapping[str, object], role: str
) -> bool:
    pid = info.get(f"{role}_pid")
    identity = info.get(f"{role}_identity")
    if identity:
        current_identity = _process_identity(pid)
        if current_identity is None:
            return _is_process_running(pid)
        return current_identity == str(identity)
    # Legacy locks without creation identity are blocked conservatively, but
    # stop_running never sends a signal to an unverified PID.
    return _is_process_running(pid)


def read_lock_info(path: Path | None = None) -> dict[str, object] | None:
    target = LOCK_PATH if path is None else Path(path)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise SelectorError(
            f"Cannot read selector lock {target}. Check it before removing it."
        ) from exc
    if not isinstance(payload, dict):
        raise SelectorError(f"Invalid selector lock: {target}")
    return payload


def lock_is_live(info: Mapping[str, object] | None) -> bool:
    if not info:
        return False
    return _role_is_live_for_lock(info, "manager") or _role_is_live_for_lock(
        info, "child"
    )


def _remove_lock_if_token_matches(path: Path, token: object) -> bool:
    try:
        current = read_lock_info(path)
    except SelectorError:
        return False
    if current is None or current.get("token") != token:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return True


class PollerLock:
    """File-based ownership record shared by both launcher-managed versions."""

    def __init__(self, version: str, path: Path | None = None):
        self.version = normalize_version(version)
        self.path = LOCK_PATH if path is None else Path(path)
        self.token = uuid.uuid4().hex
        self.acquired = False
        self.state = LOCK_STATE_STARTING
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.manager_identity = _process_identity(os.getpid())
        self.child_pid: int | None = None
        self.child_identity: str | None = None

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": LOCK_SCHEMA_VERSION,
            "token": self.token,
            "version": self.version,
            "state": self.state,
            "manager_pid": os.getpid(),
            "manager_identity": self.manager_identity,
            "child_pid": self.child_pid,
            "child_identity": self.child_identity,
            "started_at": self.started_at,
        }

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                with self.path.open("x", encoding="utf-8") as lock_file:
                    json.dump(self._payload(), lock_file, ensure_ascii=False, indent=2)
                    lock_file.write("\n")
                self.acquired = True
                return
            except FileExistsError:
                existing = read_lock_info(self.path)
                if lock_is_live(existing):
                    version = existing.get("version", "UNKNOWN")
                    child_pid = existing.get("child_pid") or "starting"
                    raise SelectorError(
                        f"{version} poller is already running (pid={child_pid}). "
                        "Use the stop command before switching."
                    )
                token = existing.get("token") if existing else None
                if not _remove_lock_if_token_matches(self.path, token):
                    raise SelectorError(
                        "Selector lock changed while checking it; retry the command."
                    )
        raise SelectorError(f"Unable to acquire selector lock: {self.path}")

    def set_child_pid(
        self,
        child_pid: int,
        child_identity: str | None = None,
    ) -> None:
        if not self.acquired:
            raise SelectorError("Selector lock is not acquired.")
        self.child_pid = int(child_pid)
        self.child_identity = child_identity or _process_identity(self.child_pid)
        if not self.child_identity:
            raise SelectorError(
                f"Unable to verify child process identity for pid={self.child_pid}."
            )
        self.state = LOCK_STATE_RUNNING
        _atomic_write_json(self.path, self._payload())

    def release(self) -> None:
        if self.acquired:
            _remove_lock_if_token_matches(self.path, self.token)
            self.acquired = False

    def __enter__(self) -> "PollerLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def _read_same_lock(token: object) -> dict[str, object] | None:
    current = read_lock_info()
    if current is None:
        return None
    if current.get("token") != token:
        raise SelectorError("Selector lock ownership changed; retry the command.")
    return current


def _wait_for_child_publication(
    info: Mapping[str, object],
    timeout_seconds: float = STARTUP_PID_WAIT_SECONDS,
) -> dict[str, object] | None:
    """Wait for a verified STARTING manager to publish its child identity."""
    token = info.get("token")
    current = dict(info)
    deadline = time.monotonic() + max(float(timeout_seconds), 0.0)
    while True:
        if current.get("child_pid") is not None:
            return current
        state = current.get("state")
        if state != LOCK_STATE_STARTING:
            raise SelectorError(
                f"Selector lock has no child in unexpected state {state!r}."
            )

        manager_pid = current.get("manager_pid")
        manager_identity = current.get("manager_identity")
        if not manager_identity:
            raise SelectorError(
                "Selector manager identity is unavailable; refusing to signal its PID."
            )
        if not _process_matches(manager_pid, manager_identity):
            if _is_process_running(manager_pid):
                raise SelectorError(
                    "Selector manager PID identity changed; refusing to signal it."
                )
            return current

        if time.monotonic() >= deadline:
            raise SelectorError(
                "Selector is still starting and has not published a child PID; "
                "retry stop shortly."
            )
        time.sleep(LOCK_POLL_SECONDS)
        latest = _read_same_lock(token)
        if latest is None:
            return None
        current = latest


def _wait_for_process_identity(
    process: subprocess.Popen,
    timeout_seconds: float = PROCESS_IDENTITY_WAIT_SECONDS,
) -> str | None:
    deadline = time.monotonic() + max(float(timeout_seconds), 0.0)
    while True:
        identity = _process_identity(process.pid)
        if identity:
            return identity
        if process.poll() is not None or time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


@contextmanager
def _manager_stop_signal_handlers():
    previous_handlers = {}
    signal_state = _ManagerSignalState()

    managed_signals = [signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        managed_signals.append(signal.SIGBREAK)
    for managed_signal in managed_signals:
        try:
            previous_handlers[managed_signal] = signal.getsignal(managed_signal)
            signal.signal(managed_signal, signal_state.handle)
        except (OSError, ValueError):
            previous_handlers.pop(managed_signal, None)
    try:
        yield signal_state
    finally:
        for managed_signal, previous_handler in previous_handlers.items():
            signal.signal(managed_signal, previous_handler)


def build_child_environment(
    version: str, base_environment: Mapping[str, str] | None = None
) -> dict[str, str]:
    selected = normalize_version(version)
    spec = VERSION_SPECS[selected]
    environment = dict(os.environ if base_environment is None else base_environment)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["QUANTBOT_VERSION"] = selected

    if selected == PORTFOLIO_BETA:
        classic_dir = VERSION_SPECS[CLASSIC_BETA].directory
        data_dir = spec.directory / "data"
        environment.update(
            {
                "QUANTBOT_BUILD": spec.build or "",
                "QUANTBOT_CONFIG_PATH": str(
                    (classic_dir / "config.local.py").resolve()
                ),
                "QUANTBOT_DATA_DIR": str(data_dir.resolve()),
                "QUANTBOT_MARKET_DB_PATH": str(
                    (classic_dir / "data" / "market_data.sqlite").resolve()
                ),
                "QUANTBOT_RUNTIME_DIR": str((data_dir / "runtime").resolve()),
                "QUANTBOT_EXPORT_DIR": str(
                    (data_dir / "backtest_exports").resolve()
                ),
                "QUANTBOT_STATE_DIR": str((data_dir / "state").resolve()),
            }
        )
    else:
        environment.pop("QUANTBOT_BUILD", None)
        for key in PORTFOLIO_PATH_ENV_KEYS:
            environment.pop(key, None)
    return environment


def _prepare_runtime_directories(version: str, environment: Mapping[str, str]) -> None:
    if normalize_version(version) != PORTFOLIO_BETA:
        return
    for key in (
        "QUANTBOT_DATA_DIR",
        "QUANTBOT_RUNTIME_DIR",
        "QUANTBOT_EXPORT_DIR",
        "QUANTBOT_STATE_DIR",
    ):
        Path(environment[key]).mkdir(parents=True, exist_ok=True)


def _terminate_child(process: subprocess.Popen, timeout_seconds: float = 5.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout_seconds)


def run_version(version: str) -> int:
    selected = normalize_version(version)
    spec = VERSION_SPECS[selected]
    if not spec.directory.is_dir():
        raise SelectorError(f"Version directory does not exist: {spec.directory}")
    if not spec.entrypoint.is_file():
        raise SelectorError(f"Bot entrypoint does not exist: {spec.entrypoint}")

    environment = build_child_environment(selected)
    _prepare_runtime_directories(selected, environment)
    command = [sys.executable, "-u", str(spec.entrypoint)]
    process: subprocess.Popen | None = None

    with _manager_stop_signal_handlers() as signal_state:
        with PollerLock(selected) as lock:
            try:
                signal_state.checkpoint()
                process = subprocess.Popen(
                    command,
                    cwd=str(spec.directory),
                    env=environment,
                )
                signal_state.interruptible = True
                signal_state.checkpoint()
                child_identity = _wait_for_process_identity(process)
                if child_identity is None:
                    if process.poll() is None:
                        raise SelectorError(
                            f"Unable to verify child process pid={process.pid}."
                        )
                    return_code = process.wait()
                    print(
                        f"[selector] {selected} exited before PID publication "
                        f"with exit_code={return_code}"
                    )
                    return int(return_code)
                lock.set_child_pid(process.pid, child_identity)
                build_text = f" build={spec.build}" if spec.build else ""
                print(
                    f"[selector] started {selected}{build_text} "
                    f"as subprocess pid={process.pid}"
                )
                return_code = process.wait()
                print(f"[selector] {selected} stopped with exit_code={return_code}")
                return int(return_code)
            except KeyboardInterrupt:
                print(f"\n[selector] stopping {selected}...")
                return 130
            except _ManagerStopSignal as stop_signal:
                print(
                    f"\n[selector] received signal {stop_signal.signum}; "
                    f"stopping {selected}..."
                )
                return 128 + stop_signal.signum
            finally:
                signal_state.interruptible = False
                if process is not None and process.poll() is None:
                    _terminate_child(process)


def stop_running(
    timeout_seconds: float = 5.0,
    startup_wait_seconds: float = STARTUP_PID_WAIT_SECONDS,
) -> bool:
    info = read_lock_info()
    if info is None:
        return False
    if not lock_is_live(info):
        _remove_lock_if_token_matches(LOCK_PATH, info.get("token"))
        return False

    token = info.get("token")
    state = info.get("state")
    if state not in LOCK_STATES:
        raise SelectorError(
            "Selector lock has no verifiable lifecycle state; refusing to stop a PID."
        )

    if info.get("child_pid") is None:
        if state != LOCK_STATE_STARTING:
            raise SelectorError(
                f"Selector lock state {state!r} has no published child PID."
            )
        info = _wait_for_child_publication(info, startup_wait_seconds)
        if info is None:
            return False

    child_pid = info.get("child_pid")
    child_identity = info.get("child_identity")
    manager_pid = info.get("manager_pid")
    manager_identity = info.get("manager_identity")
    if child_pid is None:
        if manager_identity and _process_matches(manager_pid, manager_identity):
            raise SelectorError(
                "Selector is still starting; refusing to terminate its manager "
                "before a child PID is published."
            )
        if _is_process_running(manager_pid):
            raise SelectorError(
                "Selector manager PID identity changed; refusing to terminate it."
            )
        _remove_lock_if_token_matches(LOCK_PATH, token)
        return False
    if not child_identity:
        raise SelectorError(
            "Selector child identity is unavailable; refusing to signal its PID."
        )

    if not _process_matches(child_pid, child_identity):
        if _is_process_running(child_pid):
            raise SelectorError(
                "Selector child PID identity changed; refusing to terminate an "
                "unrelated process."
            )
        if not (
            manager_identity
            and _process_matches(manager_pid, manager_identity)
        ):
            _remove_lock_if_token_matches(LOCK_PATH, token)
        return False

    latest = _read_same_lock(token)
    if latest is None:
        return False
    if (
        latest.get("state") not in LOCK_STATES
        or latest.get("child_pid") != child_pid
        or latest.get("child_identity") != child_identity
    ):
        raise SelectorError(
            "Selector child ownership changed while stopping; refusing to signal it."
        )
    if not _process_matches(child_pid, child_identity):
        if _is_process_running(child_pid):
            raise SelectorError(
                "Selector child PID was reused while stopping; refusing to signal it."
            )
        return False
    try:
        os.kill(int(child_pid), signal.SIGTERM)
    except ProcessLookupError:
        return False
    except (OSError, ValueError) as exc:
        raise SelectorError(f"Unable to stop poller pid={child_pid}: {exc}") from exc

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _process_matches(child_pid, child_identity):
            break
        time.sleep(0.1)

    if _process_matches(child_pid, child_identity):
        raise SelectorError(
            f"Stop was requested but pid={child_pid} is still running."
        )

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        latest = read_lock_info()
        if latest is None or latest.get("token") != token:
            break
        time.sleep(0.05)
    latest = read_lock_info()
    if latest and latest.get("token") == token and not lock_is_live(latest):
        _remove_lock_if_token_matches(LOCK_PATH, token)
    return True


def print_status() -> None:
    selected = read_selected_version()
    info = read_lock_info()
    print(f"selected: {selected}")
    if info is None:
        print("poller: stopped")
        return
    state = "running" if lock_is_live(info) else "stale-lock"
    print(
        f"poller: {state} version={info.get('version', 'UNKNOWN')} "
        f"manager_pid={info.get('manager_pid')} child_pid={info.get('child_pid')}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Select and launch the Classic or Portfolio QuantBot Beta."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Show selected and running versions.")

    select_parser = subparsers.add_parser(
        "select", help="Persist the version used by the next run command."
    )
    select_parser.add_argument("version", choices=VALID_VERSIONS)

    run_parser = subparsers.add_parser(
        "run", help="Launch the selected bot in a foreground subprocess."
    )
    run_parser.add_argument(
        "version",
        nargs="?",
        choices=VALID_VERSIONS,
        help="Optionally select this version before launching it.",
    )
    run_parser.add_argument(
        "--replace",
        action="store_true",
        help="Stop a launcher-managed poller before starting the selected one.",
    )

    subparsers.add_parser(
        "stop", help="Stop the currently launcher-managed bot subprocess."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            print_status()
            return 0
        if args.command == "select":
            selected = save_selected_version(args.version)
            print(f"selected: {selected}")
            return 0
        if args.command == "stop":
            stopped = stop_running()
            print("poller: stopped" if stopped else "poller: already stopped")
            return 0
        if args.command == "run":
            if args.version:
                selected = save_selected_version(args.version)
            else:
                selected = read_selected_version()
            if args.replace:
                stop_running()
            return run_version(selected)
    except (SelectorError, FileNotFoundError) as exc:
        print(f"[selector error] {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
