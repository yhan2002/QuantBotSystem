"""Deterministic architecture checks for all QuantBot code lines."""

from __future__ import annotations

import ast
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
VERSION_NAMES = (
    "QuantBotSystem",
    "QuantBotSystem_Beta",
    "QuantBotSystem_Portfolio_Beta",
)

PRIVATE_SOURCE_NAMES = {
    "config.local.py",
    "config_backup.py",
    "local_settings.py",
}

REQUIRED_COMMON_MODULES = {
    "RUN_telegram_bot_server.py",
    "app_bot_reports.py",
    "app_bot_session.py",
    "app_bot_ui.py",
    "app_daily_market_scheduler.py",
    "config.py",
    "core_backtest_periods.py",
    "core_cross_sectional_neutralization.py",
    "core_entry_execution.py",
    "core_market_data_store.py",
    "core_portfolio_performance.py",
    "core_position_mode.py",
    "core_universe_backtest.py",
    "strategy_box_range.py",
    "strategy_ma_cross.py",
    "strategy_price_liquidity_reversion.py",
}

REQUIRED_PORTFOLIO_MODULES = {
    "app_runtime_lock.py",
    "app_scan_jobs.py",
    "core_portfolio_allocator.py",
    "core_portfolio_ledger.py",
    "core_signal_model.py",
    "core_signal_scoring.py",
    "runtime_settings.py",
}

REQUIRED_ROOT_PATHS = {
    "AGENTS.md",
    "README.md",
    "RUN_quantbot_version_switch.py",
    "test_RUN_quantbot_version_switch.py",
    "test_architecture.py",
    "docs/architecture.md",
    "docs/validation.md",
    "QuantBotSystem_Portfolio_Beta/ARCHITECTURE.md",
    "QuantBotSystem_Portfolio_Beta/RUNBOOK.md",
}

CORE_COMPATIBILITY = {"market_data_store"}
STRATEGY_COMPATIBILITY = {
    "box_range_engine",
    "ma_cross_engine",
    "value_reversion_engine",
}
APP_COMPATIBILITY = {
    "bot_reports",
    "bot_session",
    "bot_ui",
    "daily_market_scheduler",
}
ENTRY_COMPATIBILITY = {"telegram_bot_server"}


def _module_name(path: Path) -> str:
    return path.name.removesuffix(".py")


def _layer(module: str) -> int | None:
    if module == "runtime_settings" or module.startswith("config"):
        return 0
    if module.startswith("core_") or module in CORE_COMPATIBILITY:
        return 1
    if (
        module.startswith("strategy_")
        or module.startswith("portfolio_")
        or module in STRATEGY_COMPATIBILITY
    ):
        return 2
    if module.startswith("app_") or module in APP_COMPATIBILITY:
        return 3
    if (
        module.startswith("RUN_")
        or module.startswith("analyze_")
        or module.startswith("experiment_")
        or module in ENTRY_COMPATIBILITY
    ):
        return 4
    return None


def _imports(path: Path) -> tuple[list[str], list[str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        return [], [f"{path.relative_to(ROOT)}: cannot parse: {exc}"]

    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.append("." * node.level + (node.module or ""))
            elif node.module:
                names.append(node.module)
    return names, []


def _production_files(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.glob("*.py")
        if not path.name.startswith("test_")
        and path.name not in PRIVATE_SOURCE_NAMES
    )


def _cycle_violations(version: str, graph: dict[str, set[str]]) -> list[str]:
    violations: set[str] = set()
    visited: set[str] = set()
    active: list[str] = []
    active_set: set[str] = set()

    def visit(module: str) -> None:
        if module in active_set:
            start = active.index(module)
            cycle = active[start:] + [module]
            violations.add(f"{version}: import cycle: {' -> '.join(cycle)}")
            return
        if module in visited:
            return
        active.append(module)
        active_set.add(module)
        for dependency in sorted(graph.get(module, set())):
            visit(dependency)
        active.pop()
        active_set.remove(module)
        visited.add(module)

    for module in sorted(graph):
        visit(module)
    return sorted(violations)


def _check_version(version: str) -> list[str]:
    directory = ROOT / version
    violations: list[str] = []
    if not directory.is_dir():
        return [f"missing version directory: {version}"]

    required = set(REQUIRED_COMMON_MODULES)
    if version == "QuantBotSystem_Portfolio_Beta":
        required.update(REQUIRED_PORTFOLIO_MODULES)
    for name in sorted(required):
        if not (directory / name).is_file():
            violations.append(f"{version}: missing required module: {name}")

    files = _production_files(directory)
    modules = {_module_name(path): path for path in files}
    graph: dict[str, set[str]] = {module: set() for module in modules}

    for module, path in sorted(modules.items()):
        source_layer = _layer(module)
        relative = path.relative_to(ROOT).as_posix()
        if source_layer is None:
            violations.append(f"{relative}: unclassified production module")

        imports, errors = _imports(path)
        violations.extend(errors)
        for imported in imports:
            if imported.startswith("."):
                violations.append(
                    f"{relative}: relative imports are not allowed: {imported}"
                )
                continue

            top = imported.split(".", 1)[0]
            if top in VERSION_NAMES:
                violations.append(
                    f"{relative}: direct cross-version import is not allowed: {imported}"
                )
                continue
            if top not in modules:
                continue

            graph[module].add(top)
            target_layer = _layer(top)
            if target_layer is None or source_layer is None:
                continue
            if target_layer > source_layer:
                violations.append(
                    f"{relative}: layer {source_layer} must not import higher "
                    f"layer {target_layer} ({top})"
                )

    violations.extend(_cycle_violations(version, graph))
    return violations


def _check_selector() -> list[str]:
    path = ROOT / "RUN_quantbot_version_switch.py"
    imports, errors = _imports(path)
    violations = list(errors)
    version_modules: set[str] = set()
    for version in VERSION_NAMES:
        version_modules.update(
            _module_name(item)
            for item in _production_files(ROOT / version)
        )
    for imported in imports:
        top = imported.lstrip(".").split(".", 1)[0]
        if top in VERSION_NAMES or top in version_modules:
            violations.append(
                "RUN_quantbot_version_switch.py: selector must launch versions "
                f"as child processes, not import {imported}"
            )
    return violations


def find_violations() -> list[str]:
    violations: list[str] = []
    for relative in sorted(REQUIRED_ROOT_PATHS):
        if not (ROOT / relative).exists():
            violations.append(f"missing required path: {relative}")
    for version in VERSION_NAMES:
        violations.extend(_check_version(version))
    violations.extend(_check_selector())
    return sorted(set(violations))


def main() -> int:
    violations = find_violations()
    if violations:
        print("Architecture violations:")
        for violation in violations:
            print(f"- {violation}")
        return 1
    print("Architecture check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
