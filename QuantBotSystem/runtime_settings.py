"""Tracked runtime settings for the deployed QuantBot process.

Private values stay in this deployment folder's ignored ``config.local.py``.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import config


VERSION = "DEPLOY"
BUILD = "2026.08.20"
VERSION_DISPLAY_NAME = "QuantBot Deploy"
VERSION_LABEL = f"{VERSION_DISPLAY_NAME} (build {BUILD})"

BASE_DIR = Path(__file__).resolve().parent
REPOSITORY_DIR = BASE_DIR.parent
LOCAL_CONFIG_PATH = BASE_DIR / "config.local.py"


def _resolved_path(raw_path: str | os.PathLike[str]) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(os.fspath(raw_path)))
    return Path(expanded).resolve()


def resolve_config_path() -> Path | None:
    """Return the explicit or deployment-local private config path."""
    configured_path = os.environ.get("QUANTBOT_CONFIG_PATH", "").strip()
    if configured_path:
        path = _resolved_path(configured_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"QUANTBOT_CONFIG_PATH does not point to a file: {path}"
            )
        return path

    if LOCAL_CONFIG_PATH.is_file():
        return LOCAL_CONFIG_PATH.resolve()
    return None


def _load_config_module(path: Path) -> ModuleType:
    module_name = f"_quantbot_runtime_config_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load runtime config: {path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_setting(name: str, default=None):
    """Read a private setting, falling back to tracked ``config.py`` values."""
    path = resolve_config_path()
    if path is not None:
        local_module = _load_config_module(path)
        if hasattr(local_module, name):
            return getattr(local_module, name)
    return getattr(config, name, default)


def config_path_for_display() -> str:
    """Describe the effective private config path without exposing its values."""
    path = resolve_config_path()
    display_path = path if path is not None else LOCAL_CONFIG_PATH
    try:
        return display_path.relative_to(REPOSITORY_DIR).as_posix()
    except ValueError:
        return str(display_path)


DATA_DIR = _resolved_path(
    os.environ.get("QUANTBOT_DATA_DIR", str(BASE_DIR / "data"))
)
RUNTIME_DIR = _resolved_path(
    os.environ.get("QUANTBOT_RUNTIME_DIR", str(DATA_DIR / "runtime"))
)
EXPORT_DIR = _resolved_path(
    os.environ.get("QUANTBOT_EXPORT_DIR", str(DATA_DIR / "backtest_exports"))
)
STATE_DIR = _resolved_path(
    os.environ.get("QUANTBOT_STATE_DIR", str(DATA_DIR / "state"))
)
MARKET_DB_PATH = _resolved_path(
    os.environ.get(
        "QUANTBOT_MARKET_DB_PATH",
        str(DATA_DIR / "market_data.sqlite"),
    )
)
