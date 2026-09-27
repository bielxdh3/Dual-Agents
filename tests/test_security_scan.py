from __future__ import annotations

from pathlib import Path
import hashlib
import json
import unittest
from unittest.mock import patch

from dual_codex.security_scan import (
    PLUGIN_ID,
    _READ_TOOLS,
    CodexSecurityProvider,
    SecurityScanError,
    arbitrate_security_scans,
    continue_security_scan_authority,
    stable_target_id,
    validate_scan_provenance,
)
from dual_codex.orchestrator import (
    _executor_security_gate_policy,
    _prepare_security_scan,
    _record_security_scan_result,
    _security_scan_request,
)


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
        self.assertIn('"scan_id":"deep-1"', decision.executor_instruction())
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

    def test_run_authority_preserves_running_scan_after_it_completes(self) -> None:
        from types import SimpleNamespace

        repository = Path(".").resolve()
        running = _scan(repository, "deep-authority", "deep", "running")
        completed = {**running, "progress": {"status": "complete"}}

        class FakeProvider:
            plugin_id = PLUGIN_ID
            plugin_version = "0.1.31"

            def __init__(self):
                self.scans = [running]
                self.arbitrations = []

            def arbitrate(self, **kwargs):
                self.arbitrations.append(dict(kwargs))
                return arbitrate_security_scans(
                    self.scans,
                    plugin_id=self.plugin_id,
                    plugin_version=self.plugin_version,
                    target_path=kwargs["repository"],
                    target_revision=kwargs["target_revision"],
                    required_mode=kwargs["required_mode"],
                    required_scope=kwargs["required_scope"],
                    allow_completed_reuse=kwargs["allow_completed_reuse"],
                )

            def list_target_scans(self, _repository):
                return list(self.scans)

        provider = FakeProvider()
        config = SimpleNamespace(repository=repository, agent_for_role=lambda _role: object())
        run_state = {}
        common = {
            "config": config,
            "requirement": (True, "standard", "."),
            "target_revision": "rev-1",
            "run_state": run_state,
            "canonical_root": repository,
            "run_dir": repository,
            "phase_provenance": [],
        }
        with patch("dual_codex.orchestrator.CodexSecurityProvider", return_value=provider), patch(
            "dual_codex.orchestrator._persist_run_state"
        ):
            _, first, _ = _prepare_security_scan(**common, checkpoint="before_architect")
            provider.scans = [completed]
            _, second, policy = _prepare_security_scan(**common, checkpoint="before_executor")
            provider.scans = [{**completed, "currentSnapshotDigest": "workspace-changed-during-correction"}]
            _, correction, _ = _prepare_security_scan(**common, checkpoint="before_correction_1")

        self.assertEqual(first.action, "awaited")
        self.assertEqual(second.action, "reused")
        self.assertEqual(correction.action, "reused")
        self.assertEqual(first.selected_scan["scanId"], second.selected_scan["scanId"])
        self.assertEqual(second.selected_scan["scanId"], correction.selected_scan["scanId"])
        self.assertEqual(len(provider.arbitrations), 1)
        self.assertFalse(provider.arbitrations[0]["allow_completed_reuse"])
        self.assertIn('"decision":"reused"', policy)
        self.assertIn("do not start a competing scan", policy)
        history = run_state["security_scan_authority_history"]
        self.assertEqual([entry["checkpoint"] for entry in history], [
            "before_architect", "before_executor", "before_correction_1"
        ])
        self.assertEqual([entry["selected_scan_id"] for entry in history], [
            "deep-authority", "deep-authority", "deep-authority"
        ])

    def test_start_authority_adopts_one_scan_created_during_architect_phase(self) -> None:
        from types import SimpleNamespace

        repository = Path(".").resolve()
        created = _scan(repository, "created-in-run", "standard", "running")

        class FakeProvider:
            plugin_id = PLUGIN_ID
            plugin_version = "0.1.31"

            def __init__(self):
                self.scans = []
                self.arbitrations = 0

            def arbitrate(self, **kwargs):
                self.arbitrations += 1
                return arbitrate_security_scans(
                    self.scans, plugin_id=self.plugin_id, plugin_version=self.plugin_version,
                    target_path=kwargs["repository"], target_revision=kwargs["target_revision"],
                    required_mode=kwargs["required_mode"], required_scope=kwargs["required_scope"],
                    allow_completed_reuse=kwargs["allow_completed_reuse"],
                )

            def list_target_scans(self, _repository):
                return list(self.scans)

        provider = FakeProvider()
        run_state = {}
        config = SimpleNamespace(repository=repository, agent_for_role=lambda _role: object())
        common = {
            "config": config, "requirement": (True, "standard", "."), "target_revision": "rev-1",
            "run_state": run_state, "canonical_root": repository, "run_dir": repository,
            "phase_provenance": [],
        }
        with patch("dual_codex.orchestrator.CodexSecurityProvider", return_value=provider), patch(
            "dual_codex.orchestrator._persist_run_state"
        ):
            _, initial, _ = _prepare_security_scan(**common, checkpoint="before_architect")
            provider.scans = [created]
            _, adopted, _ = _prepare_security_scan(**common, checkpoint="before_executor")

        self.assertEqual(initial.action, "start")
        self.assertEqual(adopted.action, "awaited")
        self.assertEqual(adopted.selected_scan["scanId"], "created-in-run")
        self.assertEqual(run_state["security_scan_authority"]["selected_scan_id"], "created-in-run")
        self.assertEqual(provider.arbitrations, 1)

    def test_scan_completing_during_executor_turn_is_pinned_for_correction(self) -> None:
        from types import SimpleNamespace

        repository = Path(".").resolve()
        running = _scan(repository, "deep-in-flight", "deep", "running")

        class FakeProvider:
            plugin_id = PLUGIN_ID
            plugin_version = "0.1.31"

            def __init__(self):
                self.scans = [running]
                self.arbitrations = []

            def arbitrate(self, **kwargs):
                self.arbitrations.append(dict(kwargs))
                return arbitrate_security_scans(
                    self.scans,
                    plugin_id=self.plugin_id,
                    plugin_version=self.plugin_version,
                    target_path=kwargs["repository"],
                    target_revision=kwargs["target_revision"],
                    required_mode=kwargs["required_mode"],
                    required_scope=kwargs["required_scope"],
                    allow_completed_reuse=kwargs["allow_completed_reuse"],
                )

            def list_target_scans(self, _repository):
                return list(self.scans)

        provider = FakeProvider()
        config = SimpleNamespace(repository=repository, agent_for_role=lambda _role: object())
        run_state = {}
        phase_provenance = []
        common = {
            "config": config,
            "requirement": (True, "standard", "."),
            "target_revision": "rev-1",
            "run_state": run_state,
            "canonical_root": repository,
            "run_dir": repository,
            "phase_provenance": phase_provenance,
        }
        with patch("dual_codex.orchestrator.CodexSecurityProvider", return_value=provider), patch(
            "dual_codex.orchestrator._persist_run_state"
        ):
            _, initial, _ = _prepare_security_scan(**common, checkpoint="before_architect")
            _, preflight, _ = _prepare_security_scan(**common, checkpoint="before_executor")
            completed = {**running, "progress": {"status": "complete"}}
            provider.scans = [completed]
            _record_security_scan_result(
                provider=provider,
                decision=preflight,
                checkpoint="before_executor",
                implementation={
                    "security_scan_provenance": {
                        "plugin_id": PLUGIN_ID,
                        "plugin_version": "0.1.31",
                        "target_identity": {
                            "path": str(repository),
                            "target_id": stable_target_id(repository),
                            "revision": "rev-1",
                            "scope": ".",
                        },
                        "scan_id": "deep-in-flight",
                        "mode": "deep",
                        "initial_status": "running",
                        "action": "awaited",
                        "final_status": "complete",
                    }
                },
                run_state=run_state,
                phase_provenance=phase_provenance,
                config=config,
                canonical_root=repository,
                run_dir=repository,
            )
            provider.scans = [{**completed, "currentSnapshotDigest": "workspace-changed-during-correction"}]
            _, correction, _ = _prepare_security_scan(**common, checkpoint="before_correction_1")

        authority = run_state["security_scan_authority"]
        self.assertEqual(initial.action, "awaited")
        self.assertEqual(preflight.action, "awaited")
        self.assertEqual(correction.action, "reused")
        self.assertEqual(correction.selected_scan["scanId"], "deep-in-flight")
        self.assertEqual(authority["selected_scan_id"], "deep-in-flight")
        self.assertEqual(authority["completed_validation"]["validated_at_checkpoint"], "before_executor")
        self.assertNotIn("targetSnapshotDigest", json.dumps(authority))
        self.assertEqual(len(provider.arbitrations), 1)
        self.assertFalse(provider.arbitrations[0]["allow_completed_reuse"])
        self.assertEqual(
            [entry.get("event") for entry in run_state["security_scan_authority_history"]],
            [None, None, "completed_validation", None],
        )

    def test_run_authority_fails_closed_for_lost_failed_competing_or_mismatched_scan(self) -> None:
        repository = Path(".").resolve()
        running = _scan(repository, "deep-authority", "deep", "running")
        authority = {
            "plugin_id": PLUGIN_ID,
            "plugin_version": "0.1.31",
            "target_path": str(repository),
            "target_id": stable_target_id(repository),
            "target_revision": "rev-1",
            "required_mode": "standard",
            "required_scope": ".",
            "initial_observed_scan_ids": ["deep-authority"],
            "selected_scan_id": "deep-authority",
            "selected_scan_mode": "deep",
            "selected_scan_scope": ".",
        }
        base = {
            "authority": authority,
            "plugin_id": PLUGIN_ID,
            "plugin_version": "0.1.31",
            "target_path": repository,
            "target_revision": "rev-1",
            "required_mode": "standard",
            "required_scope": ".",
        }
        cases = [
            ([], "SECURITY_SCAN_AUTHORITY_LOST"),
            ([{**running, "progress": {"status": "failed"}}], "SECURITY_SCAN_AUTHORITY_FAILED"),
            ([{**running, "progress": {"status": "canceled"}}], "SECURITY_SCAN_AUTHORITY_FAILED"),
            ([running, _scan(repository, "competitor", "standard", "running")], "SECURITY_SCAN_CONFLICT"),
            ([{**running, "targetRevision": "other-rev"}], "SECURITY_SCAN_AUTHORITY_INVALID"),
            ([{**running, "scope": "src"}], "SECURITY_SCAN_AUTHORITY_INVALID"),
            ([{**running, "mode": "standard"}], "SECURITY_SCAN_AUTHORITY_INVALID"),
            ([{**running, "targetId": "different-target"}], "SECURITY_SCAN_CONFLICT"),
            ([running, _scan(repository, "duplicate-complete", "standard", "complete")], "SECURITY_SCAN_CONFLICT"),
        ]
        for scans, failure_class in cases:
            with self.subTest(failure_class=failure_class, scans=len(scans)):
                with self.assertRaises(SecurityScanError) as raised:
                    continue_security_scan_authority(scans, **base)
                self.assertEqual(raised.exception.failure_class, failure_class)

        with self.assertRaises(SecurityScanError) as raised:
            continue_security_scan_authority([running], **{**base, "target_revision": "new-rev"})
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_AUTHORITY_INVALID")

        narrower_compatible = _scan(repository, "deep-authority", "deep", "running", scope="src")
        broader_pinned_authority = {**authority, "required_scope": "src", "selected_scan_scope": "."}
        with self.assertRaises(SecurityScanError) as raised:
            continue_security_scan_authority(
                [narrower_compatible], **{**base, "authority": broader_pinned_authority, "required_scope": "src"}
            )
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_AUTHORITY_INVALID")

        completed = {**running, "progress": {"status": "complete"}}
        validated_authority = {
            **authority,
            "completed_validation": {
                "scan_id": "deep-authority",
                "target_snapshot_identity": hashlib.sha256(b"snapshot-current").hexdigest(),
                "validated_at_checkpoint": "before_executor",
            },
        }
        stale_target_snapshot = {**completed, "targetSnapshotDigest": "different-snapshot"}
        with self.assertRaises(SecurityScanError) as raised:
            continue_security_scan_authority([stale_target_snapshot], **{**base, "authority": validated_authority})
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_AUTHORITY_INVALID")

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

    def test_scan_detection_uses_actual_bielos_mandatory_wording_from_mission(self) -> None:
        task = (
            "The Codex Security plugin/tool available in the Codex environment MUST run.\n"
            "Run it at least after the initial code and architecture audit."
        )
        self.assertEqual(_security_scan_request(task), (True, "standard", "."))

    def test_scan_detection_ignores_architect_plan_wording_and_negative_mission_text(self) -> None:
        self.assertEqual(_security_scan_request("Implement arbitration."), (False, "standard", "."))
        self.assertEqual(
            _security_scan_request("The Codex Security plugin/tool MUST NOT run. Do not start a competing security scan."),
            (False, "standard", "."),
        )

    def test_mission_without_security_requirement_skips_provider_arbitration(self) -> None:
        requirement = _security_scan_request("Implement a harmless formatting change.")
        with patch("dual_codex.orchestrator.CodexSecurityProvider") as provider:
            result = _prepare_security_scan(
                config=object(),
                requirement=requirement,
                target_revision="rev-1",
                run_state={},
                canonical_root=Path("."),
                run_dir=Path("."),
                phase_provenance=[],
                checkpoint="before_architect",
            )
        self.assertEqual(result, (None, None, ""))
        provider.assert_not_called()

    def test_executor_host_security_policy_overrides_a_contradictory_plan(self) -> None:
        repository = Path(".").resolve()
        decision = _decide(repository, [])
        architect_plan = {"steps": ["Do not run the Codex Security scan."]}
        policy = _executor_security_gate_policy(decision.executor_instruction())

        self.assertEqual(architect_plan["steps"], ["Do not run the Codex Security scan."])
        self.assertEqual(decision.action, "start")
        self.assertIn("supersedes any conflicting Architect plan", policy)
        self.assertIn('"decision":"start"', policy)

    def test_scan_detection_keeps_required_mode_and_scope_in_original_mission(self) -> None:
        self.assertEqual(
            _security_scan_request("Run a Codex Security standard scan.\nSecurity scan scope: src/dual_codex"),
            (True, "standard", "src/dual_codex"),
        )
        self.assertEqual(_security_scan_request("Run a deep security scan."), (True, "deep", "."))
        self.assertEqual(
            _security_scan_request(
                "The Codex Security plugin/tool MUST run.\nDeep security scan is not required; use the standard scan."
            ),
            (True, "standard", "."),
        )

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

    def test_initial_start_authority_rejects_mode_below_required_deep(self) -> None:
        repository = Path(".").resolve()
        decision = _decide(repository, [], mode="deep")
        standard = _scan(repository, "standard-created", "standard", "complete")
        evidence = {
            "plugin_id": PLUGIN_ID,
            "plugin_version": "0.1.31",
            "target_identity": {
                "path": str(repository),
                "target_id": stable_target_id(repository),
                "revision": "rev-1",
                "scope": ".",
            },
            "scan_id": "standard-created",
            "mode": "standard",
            "initial_status": "running",
            "action": "started",
            "final_status": "complete",
        }
        self.assertEqual(decision.action, "start")
        with self.assertRaises(SecurityScanError) as raised:
            validate_scan_provenance(evidence, decision=decision, final_scans=[standard])
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_EVIDENCE_INVALID")

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
