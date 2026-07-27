"""Validate independent agent-review evidence without external services."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "harness" / "review-policy.json"
ALLOWED_VERDICTS = {"APPROVE", "REQUEST_CHANGES"}
ALLOWED_FINDING_STATUSES = {"open", "resolved", "accepted_risk"}
ALLOWED_VALIDATION_STATUSES = {"passed", "failed", "not_run"}


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _identity(value: str) -> str:
    return value.strip().casefold()


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if _nonempty(item)}


def validate_record(record: Any, policy: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(policy, dict):
        return ["policy must be a JSON object"]
    if not isinstance(record, dict):
        return ["review record must be a JSON object"]

    schema_version = policy.get("schema_version")
    if record.get("schema_version") != schema_version:
        errors.append(f"schema_version must be {schema_version}")

    implementer = record.get("implementer")
    if not _nonempty(record.get("task_id")):
        errors.append("task_id must be a non-empty string")
    if not _nonempty(record.get("change_summary")):
        errors.append("change_summary must be a non-empty string")
    if not _nonempty(implementer):
        errors.append("implementer must be a non-empty string")

    scope = record.get("scope")
    if not isinstance(scope, list) or not scope or not all(_nonempty(item) for item in scope):
        errors.append("scope must be a non-empty list of file or component names")

    allowed_severities = _string_set(policy.get("allowed_severities"))
    blocking_severities = _string_set(policy.get("blocking_severities"))
    required_commands = _string_set(policy.get("required_validation_commands"))
    max_rounds = policy.get("max_rounds")
    if not isinstance(max_rounds, int) or max_rounds < 1:
        errors.append("policy max_rounds must be a positive integer")
        max_rounds = 1

    rounds = record.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        return sorted(set(errors + ["at least one review round is required"]))
    if len(rounds) > max_rounds:
        errors.append(f"review rounds exceed policy maximum of {max_rounds}")

    first_reviewer_identity: str | None = None
    finding_ids: set[str] = set()
    latest_passed_commands: set[str] = set()

    for expected_round, review_round in enumerate(rounds, start=1):
        prefix = f"round {expected_round}"
        if not isinstance(review_round, dict):
            errors.append(f"{prefix} must be a JSON object")
            continue
        if review_round.get("round") != expected_round:
            errors.append(f"{prefix} number must be {expected_round}")

        reviewer = review_round.get("reviewer")
        if not _nonempty(reviewer):
            errors.append(f"{prefix} reviewer must be a non-empty string")
        else:
            reviewer_identity = _identity(reviewer)
            if _nonempty(implementer) and reviewer_identity == _identity(implementer):
                errors.append(f"{prefix} reviewer must differ from implementer")
            if first_reviewer_identity is None:
                first_reviewer_identity = reviewer_identity
            elif (
                policy.get("same_reviewer_required") is True
                and reviewer_identity != first_reviewer_identity
            ):
                errors.append(f"{prefix} must use the original reviewer")

        verdict = review_round.get("verdict")
        if verdict not in ALLOWED_VERDICTS:
            errors.append(f"{prefix} verdict must be APPROVE or REQUEST_CHANGES")

        findings = review_round.get("findings")
        if not isinstance(findings, list):
            errors.append(f"{prefix} findings must be a list")
            findings = []
        for index, finding in enumerate(findings, start=1):
            label = f"{prefix} finding {index}"
            if not isinstance(finding, dict):
                errors.append(f"{label} must be a JSON object")
                continue
            finding_id = finding.get("id")
            if not _nonempty(finding_id):
                errors.append(f"{label} id must be a non-empty string")
            elif finding_id in finding_ids:
                errors.append(f"{label} id is duplicated: {finding_id}")
            else:
                finding_ids.add(finding_id)

            severity = finding.get("severity")
            if severity not in allowed_severities:
                errors.append(f"{label} has unsupported severity: {severity}")
            status = finding.get("status")
            if status not in ALLOWED_FINDING_STATUSES:
                errors.append(f"{label} has unsupported status: {status}")
            if not _nonempty(finding.get("file")):
                errors.append(f"{label} file must be a non-empty string")
            if not _nonempty(finding.get("summary")):
                errors.append(f"{label} summary must be a non-empty string")
            if status == "resolved" and not _nonempty(finding.get("resolution")):
                errors.append(f"{label} resolved status requires a resolution")
            if severity in blocking_severities and status != "resolved":
                errors.append(f"{label} {severity} must be resolved")

        validations = review_round.get("validations")
        if not isinstance(validations, list):
            errors.append(f"{prefix} validations must be a list")
            validations = []
        passed_commands: set[str] = set()
        for index, validation in enumerate(validations, start=1):
            label = f"{prefix} validation {index}"
            if not isinstance(validation, dict):
                errors.append(f"{label} must be a JSON object")
                continue
            command = validation.get("command")
            status = validation.get("status")
            if not _nonempty(command):
                errors.append(f"{label} command must be a non-empty string")
            if status not in ALLOWED_VALIDATION_STATUSES:
                errors.append(f"{label} has unsupported status: {status}")
            if status == "failed":
                errors.append(f"{label} is failed")
            if status == "not_run" and not _nonempty(validation.get("reason")):
                errors.append(f"{label} not_run status requires a reason")
            if _nonempty(command) and status == "passed":
                passed_commands.add(command)
        if expected_round == len(rounds):
            latest_passed_commands = passed_commands

    latest = rounds[-1] if isinstance(rounds[-1], dict) else {}
    if latest.get("verdict") != "APPROVE":
        errors.append("latest review round must have verdict APPROVE")
    for command in sorted(required_commands - latest_passed_commands):
        errors.append(f"latest review round is missing passed validation: {command}")

    return sorted(set(errors))


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _template(policy: dict[str, Any]) -> dict[str, Any]:
    commands = policy.get("required_validation_commands", [])
    return {
        "schema_version": policy.get("schema_version", 1),
        "task_id": "replace-me",
        "change_summary": "Describe the completed change.",
        "implementer": "implementer-agent-id",
        "scope": ["path/or/component"],
        "rounds": [
            {
                "round": 1,
                "reviewer": "independent-reviewer-agent-id",
                "verdict": "APPROVE",
                "findings": [],
                "validations": [
                    {"command": command, "status": "passed"}
                    for command in commands
                ],
            }
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", nargs="?", type=Path)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--print-template", action="store_true")
    args = parser.parse_args(argv)

    try:
        policy = _load_json(args.policy)
        if args.print_template:
            print(json.dumps(_template(policy), ensure_ascii=False, indent=2))
            return 0
        if args.record is None:
            parser.error("record is required unless --print-template is used")
        record = _load_json(args.record)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"Review gate input error: {exc}")
        return 2

    errors = validate_record(record, policy)
    if errors:
        print("Review gate failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Review gate passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
