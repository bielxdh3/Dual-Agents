from __future__ import annotations

from pathlib import Path
import hashlib
import json
import subprocess
import tempfile
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
    _architect_security_gate_policy,
    _executor_security_gate_policy,
    _ensure_security_gate_fresh,
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


def _decide(repository: Path, scans: list[dict], *, mode: str = "standard", scope: str = ".", reuse: bool = False):
    return arbitrate_security_scans(
        scans,
        plugin_id=PLUGIN_ID,
        plugin_version="0.1.31",
        target_path=repository,
        target_revision="rev-1",
        required_mode=mode,
        required_scope=scope,
        allow_completed_reuse=reuse,
    )


def _completed_authority(repository: Path, scan: dict) -> dict:
    snapshot = scan.get("targetSnapshotDigest")
    identity = hashlib.sha256(str(snapshot).encode("utf-8")).hexdigest() if snapshot else ""
    return {
        "plugin_id": PLUGIN_ID,
        "plugin_version": "0.1.31",
        "target_path": str(repository.resolve()),
        "target_id": stable_target_id(repository),
        "target_revision": "rev-1",
        "required_mode": "standard",
        "required_scope": ".",
        "generation": 1,
        "authority_state": "completed_fresh",
        "ownership_state": "existing_authority",
        "initial_observed_scan_ids": [scan["scanId"]],
        "generation_observed_scan_ids": [scan["scanId"]],
        "selected_scan_id": scan["scanId"],
        "selected_scan_mode": scan["mode"],
        "selected_scan_scope": scan["scope"],
        "completed_validation": {
            "scan_id": scan["scanId"],
            "target_snapshot_identity": identity,
            "current_snapshot_identity": identity,
            "validated_at_checkpoint": "before_executor",
        },
    }


class _FakeProvider:
    plugin_id = PLUGIN_ID
    plugin_version = "0.1.31"

    def __init__(self, scans: list[dict] | None = None):
        self.scans = list(scans or [])
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


def _fake_executor_actor():
    from types import SimpleNamespace

    return SimpleNamespace(account_name="codex-secundario", provider_type="codex", backend="app_server")


def _fake_executor_phase(actor) -> dict:
    return {
        "role": "executor",
        "phase_state": "completed",
        "configured_actor": True,
        "actor_id": actor.account_name,
        "profile_id": actor.account_name,
        "primary_actor": actor.account_name,
        "actual_actor": actor.account_name,
        "provider": actor.provider_type,
        "backend": actor.backend,
        "fallback_used": False,
        "dispatch_failed": False,
    }


def _evidence(
    repository: Path,
    scan_id: str,
    *,
    mode: str = "standard",
    action: str = "started",
    initial: str = "running",
    revision: str = "rev-1",
    scope: str = ".",
    target_id: str | None = None,
    target_path: str | None = None,
) -> dict:
    return {
        "plugin_id": PLUGIN_ID,
        "plugin_version": "0.1.31",
        "target_identity": {
            "path": target_path or str(repository.resolve()),
            "target_id": target_id or stable_target_id(repository),
            "revision": revision,
            "scope": scope,
        },
        "scan_id": scan_id,
        "mode": mode,
        "initial_status": initial,
        "action": action,
        "final_status": "complete",
    }


def _disposable_repo(testcase: unittest.TestCase) -> Path:
    temporary = tempfile.TemporaryDirectory()
    testcase.addCleanup(temporary.cleanup)
    repository = Path(temporary.name)
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
    return repository


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

    def test_completed_authority_requires_pinned_current_snapshot_at_every_checkpoint(self) -> None:
        repository = Path(".").resolve()
        completed = _scan(repository, "pinned", "standard", "complete")
        kwargs = {
            "plugin_id": PLUGIN_ID,
            "plugin_version": "0.1.31",
            "target_path": repository,
            "target_revision": "rev-1",
            "required_mode": "standard",
            "required_scope": ".",
        }
        authority = _completed_authority(repository, completed)
        fresh = continue_security_scan_authority([completed], authority=authority, **kwargs)
        self.assertEqual(fresh.action, "reused")
        self.assertEqual(fresh.selected_scan["scanId"], "pinned")

        stale = {**completed, "currentSnapshotDigest": "workspace-mutated"}
        with self.assertRaises(SecurityScanError) as raised:
            continue_security_scan_authority([stale], authority=authority, **kwargs)
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_SNAPSHOT_STALE")
        self.assertNotIn("UNKNOWN_MUTATION_STATE", str(raised.exception))

        warned = {**completed, "warnings": ["provider warning"]}
        with self.assertRaises(SecurityScanError) as raised:
            continue_security_scan_authority([warned], authority=authority, **kwargs)
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_AUTHORITY_INVALID")

        target_changed = {**completed, "targetSnapshotDigest": "unexpected-provider-change"}
        with self.assertRaises(SecurityScanError) as raised:
            continue_security_scan_authority([target_changed], authority=authority, **kwargs)
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_AUTHORITY_INVALID")

        no_target = {**completed, "targetSnapshotDigest": ""}
        with self.assertRaises(SecurityScanError) as raised:
            continue_security_scan_authority([no_target], authority=authority, **kwargs)
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_AUTHORITY_INVALID")

        no_current = {**completed, "currentSnapshotDigest": ""}
        with self.assertRaises(SecurityScanError) as raised:
            continue_security_scan_authority([no_current], authority=authority, **kwargs)
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_SNAPSHOT_STALE")

    def test_changed_current_snapshot_authorizes_a_new_generation(self) -> None:
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
            _, second, _ = _prepare_security_scan(**common, checkpoint="before_executor")
            provider.scans = [{**completed, "currentSnapshotDigest": "workspace-changed-during-correction"}]
            _, correction, replacement_policy = _prepare_security_scan(**common, checkpoint="before_correction_1")

        self.assertEqual(first.action, "awaited")
        self.assertEqual(second.action, "reused")
        self.assertEqual(correction.action, "start")
        self.assertEqual(first.selected_scan["scanId"], second.selected_scan["scanId"])
        self.assertIsNone(correction.selected_scan)
        self.assertEqual(len(provider.arbitrations), 1)
        self.assertFalse(provider.arbitrations[0]["allow_completed_reuse"])
        self.assertIn('"decision":"start"', replacement_policy)
        self.assertIn("do not start a competing scan", replacement_policy)
        authority = run_state["security_scan_authority"]
        self.assertEqual(authority["generation"], 2)
        self.assertEqual(authority["authority_state"], "start_authorized_unclaimed")
        self.assertEqual(authority["selected_scan_id"], "")
        history = run_state["security_scan_authority_history"]
        self.assertEqual(
            [entry["event"] for entry in history if entry.get("event") in {"completed_stale", "generation_authorized"}],
            ["completed_stale", "generation_authorized"],
        )

    def test_new_scan_during_architect_is_unowned_and_blocks_executor(self) -> None:
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
            with self.assertRaises(SecurityScanError) as raised:
                _prepare_security_scan(**common, checkpoint="before_executor")

        self.assertEqual(initial.action, "start")
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_UNOWNED_ACTIVITY")
        self.assertEqual(run_state["security_scan_authority"]["selected_scan_id"], "")
        self.assertEqual(run_state["security_scan_authority"]["authority_state"], "conflict")
        self.assertEqual(provider.arbitrations, 1)

    def test_baseline_scan_becoming_active_during_architect_blocks_executor(self) -> None:
        from types import SimpleNamespace

        repository = Path(".").resolve()
        baseline_scan = _scan(repository, "old-completed", "standard", "complete")
        provider = _FakeProvider([baseline_scan])
        run_state = {}
        config = SimpleNamespace(repository=repository, agent_for_role=lambda _role: object())
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
            _, initial, _ = _prepare_security_scan(**common, checkpoint="before_architect")
            provider.scans = [{**baseline_scan, "progress": {"status": "running"}}]
            with self.assertRaises(SecurityScanError) as raised:
                _prepare_security_scan(**common, checkpoint="before_executor")

        self.assertEqual(initial.action, "start")
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_UNOWNED_ACTIVITY")
        self.assertEqual(run_state["security_scan_authority"]["selected_scan_id"], "")
        self.assertEqual(run_state["security_scan_authority"]["authority_state"], "conflict")

    def test_executor_evidence_and_one_new_scan_establish_run_ownership(self) -> None:
        from types import SimpleNamespace

        repository = _disposable_repo(self)
        actor = _fake_executor_actor()
        provider = _FakeProvider()
        config = SimpleNamespace(
            repository=repository,
            max_correction_cycles=1,
            agent_for_role=lambda _role: actor,
        )
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
        scan = _scan(repository, "executor-started", "standard", "complete")
        with patch("dual_codex.orchestrator.CodexSecurityProvider", return_value=provider), patch(
            "dual_codex.orchestrator._persist_run_state"
        ):
            _, initial, _ = _prepare_security_scan(**common, checkpoint="before_architect")
            self.assertEqual(run_state["security_scan_authority"]["ownership_state"], "unclaimed")
            self.assertNotIn("acquisition_checkpoint", run_state["security_scan_authority"])
            provider, decision, _ = _prepare_security_scan(
                **common,
                checkpoint="before_executor",
                authorize_executor_dispatch=True,
            )
            provider.scans = [scan]
            phase_provenance.append(_fake_executor_phase(actor))
            _record_security_scan_result(
                provider=provider,
                decision=decision,
                checkpoint="before_executor",
                implementation={"security_scan_provenance": _evidence(repository, "executor-started")},
                run_state=run_state,
                phase_provenance=phase_provenance,
                config=config,
                canonical_root=repository,
                run_dir=repository,
            )
            owned_authority = dict(run_state["security_scan_authority"])
            provider.scans = [scan, _scan(repository, "later-competitor", "standard", "complete")]
            with self.assertRaises(SecurityScanError) as raised:
                _prepare_security_scan(**common, checkpoint="after_executor")

        self.assertEqual(initial.action, "start")
        self.assertEqual(owned_authority["authority_state"], "completed_fresh")
        self.assertEqual(owned_authority["ownership_state"], "run_owned")
        self.assertEqual(owned_authority["selected_scan_id"], "executor-started")
        self.assertEqual(owned_authority["generation"], 1)
        self.assertEqual(owned_authority["acquisition_checkpoint"], "before_executor")
        self.assertEqual(owned_authority["acquisition_actor"], "codex-secundario")
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_CONFLICT")
        self.assertEqual(run_state["security_scan_authority"]["authority_state"], "conflict")
        self.assertTrue(
            any(
                event.get("event") == "run_owned"
                and event.get("selected_scan_id") == "executor-started"
                and event.get("generation") == 1
                for event in run_state["security_scan_authority_history"]
            )
        )

    def test_executor_reported_scan_plus_competitor_is_a_conflict(self) -> None:
        from types import SimpleNamespace

        repository = Path(".").resolve()
        actor = _fake_executor_actor()
        provider = _FakeProvider()
        config = SimpleNamespace(repository=repository, max_correction_cycles=1, agent_for_role=lambda _role: actor)
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
        scan_a = _scan(repository, "executor-scan-a", "standard", "complete")
        scan_b = _scan(repository, "external-scan-b", "standard", "complete")
        with patch("dual_codex.orchestrator.CodexSecurityProvider", return_value=provider), patch(
            "dual_codex.orchestrator._persist_run_state"
        ):
            _prepare_security_scan(**common, checkpoint="before_architect")
            provider, decision, _ = _prepare_security_scan(
                **common,
                checkpoint="before_executor",
                authorize_executor_dispatch=True,
            )
            provider.scans = [scan_a, scan_b]
            phase_provenance.append(_fake_executor_phase(actor))
            with self.assertRaises(SecurityScanError) as raised:
                _record_security_scan_result(
                    provider=provider,
                    decision=decision,
                    checkpoint="before_executor",
                    implementation={"security_scan_provenance": _evidence(repository, "executor-scan-a")},
                    run_state=run_state,
                    phase_provenance=phase_provenance,
                    config=config,
                    canonical_root=repository,
                    run_dir=repository,
                )
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_CONFLICT")
        self.assertEqual(run_state["security_scan_authority"]["authority_state"], "conflict")

    def test_run_owned_scan_requires_exact_id_target_revision_scope_and_mode(self) -> None:
        from types import SimpleNamespace

        repository = Path(".").resolve()
        actor = _fake_executor_actor()
        cases = [
            ("wrong-id", _scan(repository, "actual-id", "deep", "complete"), {"mode": "deep"}, "SECURITY_SCAN_EVIDENCE_INVALID"),
            ("wrong-revision", _scan(repository, "scan", "deep", "complete", revision="other-rev"), {"mode": "deep"}, "SECURITY_SCAN_EVIDENCE_INVALID"),
            ("narrow-scope", _scan(repository, "scan", "deep", "complete", scope="src/sub"), {"mode": "deep", "scope": "src"}, "SECURITY_SCAN_EVIDENCE_INVALID"),
            ("weak-mode", _scan(repository, "scan", "standard", "complete"), {"mode": "standard"}, "SECURITY_SCAN_EVIDENCE_INVALID"),
            ("wrong-target", {**_scan(repository, "scan", "deep", "complete"), "targetId": "different-target"}, {"mode": "deep"}, "SECURITY_SCAN_CONFLICT"),
            ("wrong-path", _scan(Path("..", "other-repository").resolve(), "scan", "deep", "complete"), {"mode": "deep"}, "SECURITY_SCAN_EVIDENCE_INVALID"),
        ]
        for label, scan, evidence_options, expected_class in cases:
            with self.subTest(label=label):
                provider = _FakeProvider()
                config = SimpleNamespace(repository=repository, max_correction_cycles=1, agent_for_role=lambda _role: actor)
                run_state = {}
                phase_provenance = []
                common = {
                    "config": config,
                    "requirement": (True, "deep", "src" if label == "narrow-scope" else "."),
                    "target_revision": "rev-1",
                    "run_state": run_state,
                    "canonical_root": repository,
                    "run_dir": repository,
                    "phase_provenance": phase_provenance,
                }
                with patch("dual_codex.orchestrator.CodexSecurityProvider", return_value=provider), patch(
                    "dual_codex.orchestrator._persist_run_state"
                ):
                    _prepare_security_scan(**common, checkpoint="before_architect")
                    provider, decision, _ = _prepare_security_scan(
                        **common,
                        checkpoint="before_executor",
                        authorize_executor_dispatch=True,
                    )
                    provider.scans = [scan]
                    phase_provenance.append(_fake_executor_phase(actor))
                    evidence = _evidence(
                        repository,
                        "wrong-id" if label == "wrong-id" else "scan",
                        **evidence_options,
                    )
                    with self.assertRaises(SecurityScanError) as raised:
                        _record_security_scan_result(
                            provider=provider,
                            decision=decision,
                            checkpoint="before_executor",
                            implementation={"security_scan_provenance": evidence},
                            run_state=run_state,
                            phase_provenance=phase_provenance,
                            config=config,
                            canonical_root=repository,
                            run_dir=repository,
                        )
                self.assertEqual(raised.exception.failure_class, expected_class)

    def test_new_run_owned_scan_must_use_exact_requested_mode_and_scope(self) -> None:
        from types import SimpleNamespace

        repository = Path(".").resolve()
        actor = _fake_executor_actor()
        cases = [
            ("broader-mode", (True, "standard", "."), _scan(repository, "scan", "deep", "complete"), "deep", "."),
            ("broader-scope", (True, "standard", "src"), _scan(repository, "scan", "standard", "complete", scope="."), "standard", "src"),
        ]
        for label, requirement, scan, evidence_mode, evidence_scope in cases:
            with self.subTest(label=label):
                provider = _FakeProvider()
                config = SimpleNamespace(repository=repository, max_correction_cycles=1, agent_for_role=lambda _role: actor)
                run_state = {}
                phase_provenance = []
                common = {
                    "config": config,
                    "requirement": requirement,
                    "target_revision": "rev-1",
                    "run_state": run_state,
                    "canonical_root": repository,
                    "run_dir": repository,
                    "phase_provenance": phase_provenance,
                }
                with patch("dual_codex.orchestrator.CodexSecurityProvider", return_value=provider), patch(
                    "dual_codex.orchestrator._persist_run_state"
                ):
                    _prepare_security_scan(**common, checkpoint="before_architect")
                    provider, decision, _ = _prepare_security_scan(
                        **common,
                        checkpoint="before_executor",
                        authorize_executor_dispatch=True,
                    )
                    provider.scans = [scan]
                    phase_provenance.append(_fake_executor_phase(actor))
                    with self.assertRaises(SecurityScanError) as raised:
                        _record_security_scan_result(
                            provider=provider,
                            decision=decision,
                            checkpoint="before_executor",
                            implementation={
                                "security_scan_provenance": _evidence(
                                    repository,
                                    "scan",
                                    mode=evidence_mode,
                                    scope=evidence_scope,
                                )
                            },
                            run_state=run_state,
                            phase_provenance=phase_provenance,
                            config=config,
                            canonical_root=repository,
                            run_dir=repository,
                        )
                self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_EVIDENCE_INVALID")

    def test_fallback_or_substitute_executor_cannot_establish_scan_ownership(self) -> None:
        from types import SimpleNamespace

        repository = Path(".").resolve()
        actor = _fake_executor_actor()
        provider = _FakeProvider()
        config = SimpleNamespace(repository=repository, max_correction_cycles=1, agent_for_role=lambda _role: actor)
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
        scan = _scan(repository, "substitute-scan", "standard", "complete")
        phase = _fake_executor_phase(actor)
        phase.update({"actual_actor": "principal", "fallback_used": True})
        with patch("dual_codex.orchestrator.CodexSecurityProvider", return_value=provider), patch(
            "dual_codex.orchestrator._persist_run_state"
        ):
            _prepare_security_scan(**common, checkpoint="before_architect")
            provider, decision, _ = _prepare_security_scan(
                **common,
                checkpoint="before_executor",
                authorize_executor_dispatch=True,
            )
            provider.scans = [scan]
            phase_provenance.append(phase)
            with self.assertRaises(SecurityScanError) as raised:
                _record_security_scan_result(
                    provider=provider,
                    decision=decision,
                    checkpoint="before_executor",
                    implementation={"security_scan_provenance": _evidence(repository, "substitute-scan")},
                    run_state=run_state,
                    phase_provenance=phase_provenance,
                    config=config,
                    canonical_root=repository,
                    run_dir=repository,
                )
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_EVIDENCE_INVALID")
        self.assertEqual(run_state["security_scan_authority"]["authority_state"], "failed")

    def test_preselected_scan_completing_during_architect_keeps_same_authority(self) -> None:
        from types import SimpleNamespace

        repository = Path(".").resolve()
        running = _scan(repository, "preselected-deep", "deep", "running")
        completed = {**running, "progress": {"status": "complete"}}
        provider = _FakeProvider([running])
        config = SimpleNamespace(repository=repository, max_correction_cycles=1, agent_for_role=lambda _role: object())
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
            _, initial, _ = _prepare_security_scan(**common, checkpoint="before_architect")
            provider.scans = [completed]
            _, before_executor, _ = _prepare_security_scan(**common, checkpoint="before_executor")

        self.assertEqual(initial.action, "awaited")
        self.assertEqual(before_executor.action, "reused")
        self.assertEqual(before_executor.selected_scan["scanId"], "preselected-deep")
        self.assertEqual(run_state["security_scan_authority"]["ownership_state"], "existing_authority")
        self.assertEqual(run_state["security_scan_authority"]["selected_scan_id"], "preselected-deep")
        self.assertNotEqual(run_state["security_scan_authority"]["ownership_state"], "run_owned")

    def test_run_owned_generations_preserve_history_and_bound_rescans(self) -> None:
        from types import SimpleNamespace

        repository = Path(".").resolve()
        actor = _fake_executor_actor()
        provider = _FakeProvider()
        config = SimpleNamespace(repository=repository, max_correction_cycles=1, agent_for_role=lambda _role: actor)
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
        generation1 = _scan(repository, "generation-1", "standard", "complete")
        generation2 = _scan(
            repository,
            "generation-2",
            "standard",
            "complete",
        )
        generation2["targetSnapshotDigest"] = "snapshot-after-mutation"
        generation2["currentSnapshotDigest"] = "snapshot-after-mutation"

        with patch("dual_codex.orchestrator.CodexSecurityProvider", return_value=provider), patch(
            "dual_codex.orchestrator._persist_run_state"
        ):
            _prepare_security_scan(**common, checkpoint="before_architect")
            _, first_decision, _ = _prepare_security_scan(
                **common,
                checkpoint="before_executor",
                authorize_executor_dispatch=True,
            )
            provider.scans = [generation1]
            phase_provenance.append(_fake_executor_phase(actor))
            _record_security_scan_result(
                provider=provider,
                decision=first_decision,
                checkpoint="before_executor",
                implementation={"security_scan_provenance": _evidence(repository, "generation-1")},
                run_state=run_state,
                phase_provenance=phase_provenance,
                config=config,
                canonical_root=repository,
                run_dir=repository,
            )
            self.assertEqual(run_state["security_scan_authority"]["authority_state"], "completed_fresh")

            stale_generation1 = {**generation1, "currentSnapshotDigest": "snapshot-after-mutation"}
            provider.scans = [stale_generation1]
            _, replacement, _ = _prepare_security_scan(
                **common,
                checkpoint="before_reviewer_0",
                authorize_executor_dispatch=True,
            )
            self.assertEqual(replacement.action, "start")
            self.assertEqual(run_state["security_scan_authority"]["generation"], 2)
            self.assertEqual(run_state["security_scan_authority"]["authority_state"], "start_authorized_unclaimed")

            provider.scans = [stale_generation1, generation2]
            phase_provenance.append(_fake_executor_phase(actor))
            _record_security_scan_result(
                provider=provider,
                decision=replacement,
                checkpoint="before_reviewer_0",
                implementation={"security_scan_provenance": _evidence(repository, "generation-2")},
                run_state=run_state,
                phase_provenance=phase_provenance,
                config=config,
                canonical_root=repository,
                run_dir=repository,
            )
            authority = run_state["security_scan_authority"]
            self.assertEqual(authority["authority_state"], "completed_fresh")
            self.assertEqual(authority["ownership_state"], "run_owned")
            self.assertEqual(authority["selected_scan_id"], "generation-2")
            self.assertEqual(authority["generation"], 2)
            self.assertEqual([item["scan_id"] for item in run_state["security_scan_provenance"]], ["generation-1", "generation-2"])

            stale_generation2 = {**generation2, "currentSnapshotDigest": "snapshot-after-second-mutation"}
            provider.scans = [stale_generation1, stale_generation2]
            with self.assertRaises(SecurityScanError) as raised:
                _prepare_security_scan(**common, checkpoint="before_reviewer_1")

        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_RESCAN_LIMIT")
        self.assertEqual(run_state["security_scan_authority"]["generation"], 2)
        self.assertEqual(run_state["security_scan_authority"]["authority_state"], "completed_stale")
        events = [event.get("event") for event in run_state["security_scan_authority_history"]]
        self.assertEqual(events.count("generation_authorized"), 1)
        self.assertEqual(events.count("rescan_limit"), 1)
        serialized = json.dumps(run_state)
        self.assertNotIn("snapshot-after-mutation", serialized)
        self.assertNotIn("snapshot-after-second-mutation", serialized)

    def test_external_competitor_after_replacement_authorization_blocks_ownership(self) -> None:
        from types import SimpleNamespace

        repository = Path(".").resolve()
        actor = _fake_executor_actor()
        provider = _FakeProvider()
        config = SimpleNamespace(repository=repository, max_correction_cycles=2, agent_for_role=lambda _role: actor)
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
        first = _scan(repository, "generation-1", "standard", "complete")
        second = _scan(
            repository,
            "generation-2",
            "standard",
            "complete",
        )
        second["targetSnapshotDigest"] = "new-snapshot"
        second["currentSnapshotDigest"] = "new-snapshot"
        with patch("dual_codex.orchestrator.CodexSecurityProvider", return_value=provider), patch(
            "dual_codex.orchestrator._persist_run_state"
        ):
            _prepare_security_scan(**common, checkpoint="before_architect")
            _, first_decision, _ = _prepare_security_scan(
                **common,
                checkpoint="before_executor",
                authorize_executor_dispatch=True,
            )
            provider.scans = [first]
            phase_provenance.append(_fake_executor_phase(actor))
            _record_security_scan_result(
                provider=provider,
                decision=first_decision,
                checkpoint="before_executor",
                implementation={"security_scan_provenance": _evidence(repository, "generation-1")},
                run_state=run_state,
                phase_provenance=phase_provenance,
                config=config,
                canonical_root=repository,
                run_dir=repository,
            )
            stale_first = {**first, "currentSnapshotDigest": "new-snapshot"}
            provider.scans = [stale_first]
            _, replacement, _ = _prepare_security_scan(
                **common,
                checkpoint="before_reviewer_0",
                authorize_executor_dispatch=True,
            )
            external = _scan(repository, "external-competitor", "standard", "complete")
            provider.scans = [stale_first, external, second]
            phase_provenance.append(_fake_executor_phase(actor))
            with self.assertRaises(SecurityScanError) as raised:
                _record_security_scan_result(
                    provider=provider,
                    decision=replacement,
                    checkpoint="before_reviewer_0",
                    implementation={"security_scan_provenance": _evidence(repository, "generation-2")},
                    run_state=run_state,
                    phase_provenance=phase_provenance,
                    config=config,
                    canonical_root=repository,
                    run_dir=repository,
                )

        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_CONFLICT")
        self.assertEqual(run_state["security_scan_authority"]["generation"], 2)
        self.assertEqual(run_state["security_scan_authority"]["authority_state"], "conflict")
        self.assertEqual(run_state["security_scan_authority"]["selected_scan_id"], "")

    def test_final_reviewer_gate_runs_scan_only_executor_until_fresh(self) -> None:
        from types import SimpleNamespace
        import threading

        repository = _disposable_repo(self)
        actor = _fake_executor_actor()
        first = _scan(repository, "prior-generation", "standard", "complete")
        stale = {**first, "currentSnapshotDigest": "workspace-changed"}
        replacement = _scan(repository, "replacement-generation", "standard", "complete")
        replacement["targetSnapshotDigest"] = "workspace-changed"
        replacement["currentSnapshotDigest"] = "workspace-changed"
        provider = _FakeProvider([stale])
        config = SimpleNamespace(
            repository=repository,
            project_root=Path.cwd(),
            max_correction_cycles=1,
            agent_for_role=lambda _role: actor,
        )
        authority = _completed_authority(repository, first)
        authority.update({"generation": 1, "max_generations": 2, "ownership_state": "run_owned"})
        run_state = {"security_scan_authority": authority, "security_scan_authority_history": []}
        phase_provenance = []
        dispatched: list[str] = []

        def fake_dispatch(**kwargs):
            dispatched.append(kwargs["role"])
            self.assertEqual(kwargs["role"], "executor")
            self.assertIn("scan-only continuation", kwargs["task"])
            self.assertIn('"decision":"start"', kwargs["task"])
            provider.scans = [stale, replacement]
            phase_provenance.append(_fake_executor_phase(actor))
            kwargs["output_path"].write_text(
                json.dumps(
                    {
                        "summary": "scan-only continuation complete",
                        "files_changed": [],
                        "commands_run": [],
                        "tests": [],
                        "remaining_issues": [],
                        "security_scan_provenance": _evidence(repository, "replacement-generation"),
                    }
                ),
                encoding="utf-8",
            )

        with patch("dual_codex.orchestrator.CodexSecurityProvider", return_value=provider), patch(
            "dual_codex.orchestrator._persist_run_state"
        ), patch("dual_codex.orchestrator._dispatch_phase", side_effect=fake_dispatch):
            dispatched_rescan = _ensure_security_gate_fresh(
                config=config,
                requirement=(True, "standard", "."),
                target_revision="rev-1",
                run_state=run_state,
                canonical_root=repository,
                run_dir=repository,
                phase_provenance=phase_provenance,
                state_lock=threading.RLock(),
                checkpoint="before_reviewer",
            )

        self.assertTrue(dispatched_rescan)
        self.assertEqual(dispatched, ["executor"])
        self.assertEqual(run_state["security_scan_authority"]["authority_state"], "completed_fresh")
        self.assertEqual(run_state["security_scan_authority"]["generation"], 2)
        self.assertEqual(run_state["security_scan_authority"]["selected_scan_id"], "replacement-generation")

    def test_preselected_scan_completes_fresh_then_mutation_authorizes_replacement(self) -> None:
        from types import SimpleNamespace

        repository = Path(".").resolve()
        running = _scan(repository, "deep-in-flight", "deep", "running")
        actor = _fake_executor_actor()

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
        config = SimpleNamespace(repository=repository, max_correction_cycles=1, agent_for_role=lambda _role: actor)
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
            phase_provenance.append(_fake_executor_phase(actor))
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
        self.assertEqual(correction.action, "start")
        self.assertIsNone(correction.selected_scan)
        self.assertEqual(authority["generation"], 2)
        self.assertEqual(authority["authority_state"], "start_authorized_unclaimed")
        self.assertEqual(authority["selected_scan_id"], "")
        self.assertIsNone(authority["completed_validation"])
        self.assertNotIn("targetSnapshotDigest", json.dumps(authority))
        self.assertEqual(len(provider.arbitrations), 1)
        self.assertFalse(provider.arbitrations[0]["allow_completed_reuse"])
        self.assertEqual(
            [entry.get("event") for entry in run_state["security_scan_authority_history"] if entry.get("event")],
            ["completed_fresh", "completed_stale", "generation_authorized"],
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

    def test_architect_receives_only_declarative_security_summary(self) -> None:
        repository = Path(".").resolve()
        selected = _scan(repository, "selected-deep", "deep", "running", scope=".")
        decision = _decide(repository, [selected], scope="src")
        policy = _architect_security_gate_policy((True, "standard", "src"), decision)

        self.assertIn('"required":true', policy)
        self.assertIn('"required_mode":"standard"', policy)
        self.assertIn('"required_scope":"src"', policy)
        self.assertIn('"host_decision":"awaited"', policy)
        self.assertIn('"scan_id":"selected-deep"', policy)
        self.assertIn('"mode":"deep"', policy)
        self.assertIn('"status":"running"', policy)
        self.assertIn('"target_id":"', policy)
        self.assertIn('"revision":"rev-1"', policy)
        self.assertIn("must not start, resume, cancel, await operationally", policy)
        for operational in (
            "TRUSTED CODEX SECURITY SCAN POLICY",
            "Return a security_scan_provenance object",
            "If the decision is 'start'",
            "do not start a competing scan",
            "inspect and await the selected scan",
            "resume the selected scan",
        ):
            self.assertNotIn(operational, policy)

    def test_architect_no_requirement_still_cannot_operate_security_scans(self) -> None:
        policy = _architect_security_gate_policy((False, "standard", "."), None)
        self.assertIn("does not require a Codex Security scan", policy)
        self.assertIn("must not start, resume, cancel, await operationally", policy)
        self.assertNotIn("security_scan_provenance", policy)

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
