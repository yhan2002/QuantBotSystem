from __future__ import annotations

import unittest

from tools.review_gate import validate_record


COMMAND = "python -m unittest test_architecture.py -v"
POLICY = {
    "schema_version": 1,
    "max_rounds": 3,
    "same_reviewer_required": True,
    "allowed_severities": ["P0", "P1", "P2", "P3"],
    "blocking_severities": ["P0", "P1", "P2"],
    "required_validation_commands": [COMMAND],
}


def approved_record() -> dict:
    return {
        "schema_version": 1,
        "task_id": "review-gate-test",
        "change_summary": "Exercise the review gate.",
        "implementer": "implementer",
        "scope": ["tools/review_gate.py"],
        "rounds": [
            {
                "round": 1,
                "reviewer": "reviewer",
                "verdict": "APPROVE",
                "findings": [],
                "validations": [{"command": COMMAND, "status": "passed"}],
            }
        ],
    }


class ReviewGateTest(unittest.TestCase):
    def test_approved_independent_review_passes(self) -> None:
        self.assertEqual([], validate_record(approved_record(), POLICY))

    def test_self_review_is_rejected(self) -> None:
        record = approved_record()
        record["rounds"][0]["reviewer"] = record["implementer"]
        self.assertIn(
            "round 1 reviewer must differ from implementer",
            validate_record(record, POLICY),
        )

    def test_self_review_with_whitespace_and_case_is_rejected(self) -> None:
        record = approved_record()
        record["implementer"] = "Agent-A"
        record["rounds"][0]["reviewer"] = " agent-a "
        self.assertIn(
            "round 1 reviewer must differ from implementer",
            validate_record(record, POLICY),
        )

    def test_open_blocking_finding_is_rejected(self) -> None:
        record = approved_record()
        record["rounds"][0]["findings"] = [
            {
                "id": "F1",
                "severity": "P1",
                "status": "open",
                "file": "module.py",
                "summary": "Blocking defect.",
            }
        ]
        self.assertIn(
            "round 1 finding 1 P1 must be resolved",
            validate_record(record, POLICY),
        )

    def test_latest_round_requires_approval_and_validation(self) -> None:
        record = approved_record()
        record["rounds"][0]["verdict"] = "REQUEST_CHANGES"
        record["rounds"][0]["validations"] = []
        errors = validate_record(record, POLICY)
        self.assertIn("latest review round must have verdict APPROVE", errors)
        self.assertIn(
            f"latest review round is missing passed validation: {COMMAND}",
            errors,
        )

    def test_failed_validation_blocks_approval(self) -> None:
        record = approved_record()
        record["rounds"][0]["validations"].append(
            {"command": "python -m py_compile changed.py", "status": "failed"}
        )
        self.assertIn(
            "round 1 validation 2 is failed",
            validate_record(record, POLICY),
        )

    def test_not_run_validation_requires_reason(self) -> None:
        record = approved_record()
        record["rounds"][0]["validations"].append(
            {"command": "focused test", "status": "not_run"}
        )
        self.assertIn(
            "round 1 validation 2 not_run status requires a reason",
            validate_record(record, POLICY),
        )


if __name__ == "__main__":
    unittest.main()
