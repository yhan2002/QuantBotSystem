# File name: local_settings.py
# Loads optional local-only settings without committing private values.
import importlib
import importlib.util
import os

import config


def _load_dot_local_module():
    path = os.path.join(os.path.dirname(__file__), "config.local.py")
    if not os.path.exists(path):
        return None

    spec = importlib.util.spec_from_file_location("config_dot_local", path)
    if spec is None or spec.loader is None:
        return None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_local_module():
    try:
        return importlib.import_module("config_local")
    except ModuleNotFoundError:
        return _load_dot_local_module()


def get_setting(name, default=None):
    local_module = _load_local_module()
    if local_module is not None and hasattr(local_module, name):
        return getattr(local_module, name)
    return getattr(config, name, default)


def hidden_backtest_chat_ids():
    values = get_setting("BACKTEST_DETAIL_HIDDEN_CHAT_IDS", set())
    return {str(value) for value in values}
