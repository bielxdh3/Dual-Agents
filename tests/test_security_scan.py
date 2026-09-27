from __future__ import annotations

from pathlib import Path
import unittest

from dual_codex.security_scan import (
    PLUGIN_ID,
    _READ_TOOLS,
    CodexSecurityProvider,
    SecurityScanError,
    arbitrate_security_scans,
    stable_target_id,
    validate_scan_provenance,
)
from dual_codex.orchestrator import _security_scan_request


def _scan(repository: Path, scan_id: str, mode: str, status: str, *, revision: str = "rev-1", scope: str = ".") -> dict:
    return {
        "scanId": scan_id,
        "mode": mode,
        "progress": {"status": status},
        "targetPath": str(repository.resolve()),
        "targetId": stable_target_id(repository),
        "targetRevision": revision,
        "scope": scope,
        "updatedAt": "2026-09-26T00:00:00Z",
        "targetSnapshotDigest": "snapshot-current",
        "currentSnapshotDigest": "snapshot-current",
    }


def _decide(repository: Path, scans: list[dict], *, mode: str = "standard", reuse: bool = False):
    return arbitrate_security_scans(
        scans,
        plugin_id=PLUGIN_ID,
        plugin_version="0.1.31",
        target_path=repository,
        target_revision="rev-1",
        required_mode=mode,
        required_scope=".",
        allow_completed_reuse=reuse,
    )


class SecurityScanArbitrationTests(unittest.TestCase):
    def test_no_existing_scan_starts_one(self) -> None:
        with self.subTest("empty ledger"):
            repository = Path(".").resolve()
            decision = _decide(repository, [])
            self.assertEqual(decision.action, "start")
            self.assertIsNone(decision.selected_scan)

    def test_active_deep_scan_satisfies_standard_and_is_awaited(self) -> None:
        repository = Path(".").resolve()
        deep = _scan(repository, "deep-1", "deep", "running")
        decision = _decide(repository, [deep])
        self.assertEqual(decision.action, "awaited")
        self.assertEqual(decision.selected_scan["scanId"], "deep-1")
        self.assertIn('"decision":"awaited"', decision.executor_instruction())
        self.assertIn("do not start a competing scan", decision.executor_instruction())

    def test_active_standard_scan_is_awaited(self) -> None:
        repository = Path(".").resolve()
        standard = _scan(repository, "standard-1", "standard", "running")
        decision = _decide(repository, [standard])
        self.assertEqual(decision.action, "awaited")
        self.assertEqual(decision.selected_scan["scanId"], "standard-1")

    def test_completed_scan_reuse_requires_explicit_policy_and_fresh_digest(self) -> None:
        repository = Path(".").resolve()
        completed = _scan(repository, "complete-1", "standard", "complete")
        self.assertEqual(_decide(repository, [completed]).action, "start")
        self.assertEqual(_decide(repository, [completed], reuse=True).action, "reused")
        stale = {**completed, "currentSnapshotDigest": "different"}
        self.assertEqual(_decide(repository, [stale], reuse=True).action, "start")
        warned = {**completed, "warnings": ["incomplete target"]}
        self.assertEqual(_decide(repository, [warned], reuse=True).action, "start")

    def test_incompatible_completed_scan_does_not_block_new_start(self) -> None:
        repository = Path(".").resolve()
        stale = _scan(repository, "old-1", "standard", "complete", revision="old-rev")
        decision = _decide(repository, [stale], reuse=True)
        self.assertEqual(decision.action, "start")

    def test_other_target_scans_are_ignored(self) -> None:
        repository = Path(".").resolve()
        other = Path("..", "other-repo").resolve()
        scan = _scan(other, "other-1", "standard", "running")
        decision = _decide(repository, [scan])
        self.assertEqual(decision.action, "start")

    def test_multiple_active_scans_fail_closed_with_identifiable_records(self) -> None:
        repository = Path(".").resolve()
        scans = [
            _scan(repository, "deep-1", "deep", "running"),
            _scan(repository, "standard-1", "standard", "running"),
        ]
        decision = _decide(repository, scans)
        self.assertEqual(decision.action, "conflict")
        self.assertEqual(decision.failure_class, "SECURITY_SCAN_CONFLICT")
        self.assertEqual(
            {(item["scanId"], item["mode"], item["progress"]["status"]) for item in decision.observed_scans},
            {("deep-1", "deep", "running"), ("standard-1", "standard", "running")},
        )
        record = decision.public_record()
        self.assertEqual(
            {(item["scan_id"], item["mode"], item["status"]) for item in record["observed_scans"]},
            {("deep-1", "deep", "running"), ("standard-1", "standard", "running")},
        )

    def test_arbitrator_exposes_only_read_apis_and_never_cancels(self) -> None:
        self.assertTrue(_READ_TOOLS)
        self.assertTrue(all(name.startswith(("list_", "get_")) for name in _READ_TOOLS))
        self.assertFalse(any("cancel" in name or "start" in name or "resume" in name for name in _READ_TOOLS))

    def test_scan_detection_requires_an_affirmative_scan_action(self) -> None:
        self.assertEqual(_security_scan_request("Do not start a competing security scan.", {})[0], False)
        self.assertEqual(_security_scan_request("The security scan must not be started.", {})[0], False)
        self.assertEqual(_security_scan_request("Run a Codex Security standard scan.", {})[0], True)
        self.assertEqual(_security_scan_request("Implement arbitration.", {"steps": ["Run a deep security scan."]}), (True, "deep"))

    def test_provider_reads_paginated_scan_ledger_without_mutating_it(self) -> None:
        from dual_codex.config import AgentConfig
        from unittest.mock import Mock, patch

        repository = Path(".").resolve()
        scan = _scan(repository, "deep-1", "deep", "running")
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=None)
        client.call_read_tool.side_effect = [
            {"scans": [scan], "nextOffset": 50},
            {"scans": []},
        ]
        agent = AgentConfig(
            codex_home=Path("C:/CodexProfiles/secondary"),
            model="",
            reasoning_effort="high",
            sandbox="workspace-write",
            account_name="codex-secundario",
            backend="app_server",
        )
        with patch("dual_codex.security_scan._installed_plugin", return_value=(Path("C:/plugin"), PLUGIN_ID, "0.1.31")), patch(
            "dual_codex.security_scan.codex_environment", return_value={"PATH": "node"}
        ), patch("dual_codex.security_scan._node_executable", return_value="node"), patch(
            "dual_codex.security_scan._McpReadClient", return_value=client
        ):
            provider = CodexSecurityProvider(agent)
            result = provider.list_target_scans(repository)
        self.assertEqual([item["scanId"] for item in result], ["deep-1"])
        calls = client.call_read_tool.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["list_codex_security_scans", "list_codex_security_scans"])
        self.assertEqual([call.args[1]["offset"] for call in calls], [0, 50])
        self.assertTrue(all(call.args[1]["query"] == str(repository) for call in calls))

    def test_selected_scan_provenance_is_checked_against_provider_ledger(self) -> None:
        repository = Path(".").resolve()
        active = _scan(repository, "deep-1", "deep", "running")
        decision = _decide(repository, [active])
        complete = {**active, "progress": {"status": "complete"}}
        evidence = {
            "plugin_id": PLUGIN_ID,
            "plugin_version": "0.1.31",
            "target_identity": {
                "path": str(repository),
                "target_id": stable_target_id(repository),
                "revision": "rev-1",
                "scope": ".",
            },
            "scan_id": "deep-1",
            "mode": "deep",
            "initial_status": "running",
            "action": "awaited",
            "final_status": "complete",
        }
        result = validate_scan_provenance(evidence, decision=decision, final_scans=[complete])
        self.assertEqual(result["scan_id"], "deep-1")
        self.assertEqual(result["scan_mode"], "deep")
        self.assertEqual(result["action"], "awaited")
        self.assertEqual(result["final_status"], "complete")

    def test_scan_conflict_has_specific_class_and_no_mutation_unknown_fallback(self) -> None:
        repository = Path(".").resolve()
        scans = [
            _scan(repository, "deep-1", "deep", "running"),
            _scan(repository, "standard-1", "standard", "running"),
        ]
        decision = _decide(repository, scans)
        with self.assertRaises(SecurityScanError) as raised:
            raise SecurityScanError(
                "SECURITY_SCAN_CONFLICT: multiple active exact-target scans.",
                failure_class=decision.failure_class,
            )
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_CONFLICT")
        self.assertNotEqual(raised.exception.failure_class, "UNKNOWN_MUTATION_STATE")

    def test_postflight_rejects_competing_scan_and_cancellation(self) -> None:
        repository = Path(".").resolve()
        active = _scan(repository, "deep-1", "deep", "running")
        decision = _decide(repository, [active])
        extra = _scan(repository, "new-standard", "standard", "running")
        evidence = {
            "plugin_id": PLUGIN_ID,
            "plugin_version": "0.1.31",
            "target_identity": {
                "path": str(repository),
                "target_id": stable_target_id(repository),
                "revision": "rev-1",
                "scope": ".",
            },
            "scan_id": "deep-1",
            "mode": "deep",
            "initial_status": "running",
            "action": "awaited",
            "final_status": "complete",
        }
        with self.assertRaises(SecurityScanError) as raised:
            validate_scan_provenance(evidence, decision=decision, final_scans=[active, extra])
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_CONFLICT")

        selected_complete = {**active, "progress": {"status": "complete"}}
        competing_complete = {**extra, "progress": {"status": "complete"}}
        with self.assertRaises(SecurityScanError) as raised:
            validate_scan_provenance(evidence, decision=decision, final_scans=[selected_complete, competing_complete])
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_CONFLICT")

        canceled = {**active, "progress": {"status": "canceled"}}
        with self.assertRaises(SecurityScanError) as raised:
            validate_scan_provenance(evidence, decision=decision, final_scans=[canceled])
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_CONFLICT")


if __name__ == "__main__":
    unittest.main()
