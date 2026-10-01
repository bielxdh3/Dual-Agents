from __future__ import annotations

from pathlib import Path
import hashlib
import json
import queue
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dual_codex.security_scan import (
    PLUGIN_ID,
    _READ_TOOLS,
    _McpReadClient,
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
    _max_security_generations,
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


def _commit_test_file(repository: Path, relative_path: str = "tracked.txt") -> Path:
    path = repository / relative_path
    path.write_text("initial content\n", encoding="utf-8")
    subprocess.run(["git", "add", relative_path], cwd=repository, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial"],
        cwd=repository,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return path


class SecurityScanArbitrationTests(unittest.TestCase):
    def test_ownerless_deep_cancel_calls_only_the_exact_app_admin_tool(self) -> None:
        client = object.__new__(_McpReadClient)
        client.tool_names = frozenset({"cancel_codex_security_scan_from_app"})
        calls: list[tuple[str, dict]] = []

        def request(method: str, params: dict) -> dict:
            calls.append((method, params))
            return {
                "structuredContent": {
                    "workspace": {
                        "results": {
                            "scanId": params["arguments"]["scanId"],
                            "progress": {"status": "canceled"},
                        }
                    }
                },
                "isError": False,
            }

        client._request = request
        scan_id = "964f6fef-4dac-4401-8143-5814bb0acdf3"
        result = client.cancel_ownerless_deep_scan(scan_id)
        self.assertEqual(calls, [("tools/call", {"name": "cancel_codex_security_scan_from_app", "arguments": {"scanId": scan_id}})])
        self.assertTrue(result["tool_call_attempted"])
        self.assertTrue(result["result_scan_id_matches"])
        self.assertEqual(result["result_status"], "canceled")

    def test_ownerless_deep_cancel_reports_unsupported_without_calling_any_tool(self) -> None:
        client = object.__new__(_McpReadClient)
        client.tool_names = frozenset()
        client._request = lambda *_args: self.fail("unsupported provider must not receive a tool call")
        result = client.cancel_ownerless_deep_scan("964f6fef-4dac-4401-8143-5814bb0acdf3")
        self.assertFalse(result["tool_available"])
        self.assertFalse(result["tool_call_attempted"])
        self.assertEqual(result["failure_class"], "SECURITY_SCAN_OWNERLESS_ADMIN_UNSUPPORTED")

    def test_mcp_read_client_rejects_non_object_jsonrpc_frames(self) -> None:
        client = object.__new__(_McpReadClient)
        client._request_id = 0
        client._lines = queue.Queue()
        client._lines.put("[]\n")
        with patch.object(client, "_send"):
            with self.assertRaisesRegex(SecurityScanError, "invalid protocol data") as raised:
                client._request("tools/list", {})
        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_PROVIDER_UNAVAILABLE")

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
            _, generation3_decision, _ = _prepare_security_scan(
                **common,
                checkpoint="before_reviewer_1",
                authorize_executor_dispatch=True,
            )
            self.assertEqual(generation3_decision.action, "start")
            self.assertEqual(run_state["security_scan_authority"]["generation"], 3)
            generation3 = _scan(repository, "generation-3", "standard", "complete")
            generation3["targetSnapshotDigest"] = "snapshot-after-second-mutation"
            generation3["currentSnapshotDigest"] = "snapshot-after-second-mutation"
            provider.scans.append(generation3)
            phase_provenance.append(_fake_executor_phase(actor))
            _record_security_scan_result(
                provider=provider,
                decision=generation3_decision,
                checkpoint="before_reviewer_1",
                implementation={"security_scan_provenance": _evidence(repository, "generation-3")},
                run_state=run_state,
                phase_provenance=phase_provenance,
                config=config,
                canonical_root=repository,
                run_dir=repository,
            )
            self.assertEqual(run_state["security_scan_authority"]["authority_state"], "completed_fresh")
            self.assertEqual(run_state["security_scan_authority"]["selected_scan_id"], "generation-3")

            stale_generation3 = {**generation3, "currentSnapshotDigest": "snapshot-after-third-mutation"}
            provider.scans = [stale_generation1, stale_generation2, stale_generation3]
            with self.assertRaises(SecurityScanError) as raised:
                _prepare_security_scan(**common, checkpoint="before_reviewer_2")

        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_RESCAN_LIMIT")
        self.assertEqual(run_state["security_scan_authority"]["generation"], 3)
        self.assertEqual(run_state["security_scan_authority"]["authority_state"], "completed_stale")
        events = [event.get("event") for event in run_state["security_scan_authority_history"]]
        self.assertEqual(events.count("generation_authorized"), 2)
        self.assertEqual(events.count("rescan_limit"), 1)
        serialized = json.dumps(run_state)
        self.assertNotIn("snapshot-after-mutation", serialized)
        self.assertNotIn("snapshot-after-second-mutation", serialized)
        self.assertNotIn("snapshot-after-third-mutation", serialized)

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
        authority.update({"generation": 1, "max_generations": 3, "ownership_state": "run_owned"})
        run_state = {"security_scan_authority": authority, "security_scan_authority_history": []}
        phase_provenance = []
        dispatched: list[str] = []
        run_dir = repository / ".dual_codex" / "runs" / "scan-only"
        run_dir.mkdir(parents=True, exist_ok=True)

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
                run_dir=run_dir,
                phase_provenance=phase_provenance,
                state_lock=threading.RLock(),
                checkpoint="before_reviewer",
            )

        self.assertTrue(dispatched_rescan)
        self.assertEqual(dispatched, ["executor"])
        self.assertEqual(run_state["security_scan_authority"]["authority_state"], "completed_fresh")
        self.assertEqual(run_state["security_scan_authority"]["generation"], 2)
        self.assertEqual(run_state["security_scan_authority"]["selected_scan_id"], "replacement-generation")

    def _run_scan_only_turn(
        self,
        mutation=None,
        *,
        unknown_attribution: bool = False,
        capture_error: bool = False,
        attribution_error: bool = False,
        dispatch_error: bool = False,
        external_run_dir: bool = False,
        external_symlink: bool = False,
        write_report: bool = True,
    ) -> dict:
        from types import SimpleNamespace
        import threading

        import dual_codex.git as git_module
        import dual_codex.orchestrator as orchestrator

        repository = _disposable_repo(self)
        tracked = _commit_test_file(repository)
        ignored_file = repository / "pre-existing-ignored.txt"
        ignored_nested_file = repository / "pre-existing-ignored-dir" / "nested" / "baseline.py"
        exclude_file = repository / ".git" / "info" / "exclude"
        with exclude_file.open("a", encoding="utf-8") as stream:
            stream.write("\npre-existing-ignored.txt\npre-existing-ignored-dir/\n")
        ignored_file.write_text("ignored baseline\n", encoding="utf-8")
        ignored_nested_file.parent.mkdir(parents=True)
        ignored_nested_file.write_text("nested ignored baseline\n", encoding="utf-8")
        external_target = None
        if external_symlink:
            external_target = repository.parent / "external-symlink-target.txt"
            external_target.write_text("external baseline\n", encoding="utf-8")
            try:
                (repository / "external-link.txt").symlink_to(external_target)
            except (OSError, NotImplementedError) as exc:
                raise unittest.SkipTest(f"symlink creation is unavailable: {type(exc).__name__}") from exc
        if external_run_dir:
            temporary_run = tempfile.TemporaryDirectory()
            self.addCleanup(temporary_run.cleanup)
            run_dir = Path(temporary_run.name)
        else:
            run_dir = repository / ".dual_codex" / "runs" / "scan-only"
        run_dir.mkdir(parents=True, exist_ok=True)
        orchestrator._safe_atomic_write_control_json(run_dir / "run_state.json", {"host": "initial state"})
        orchestrator._safe_atomic_write_control_json(run_dir / "provenance.json", {"host": "initial provenance"})
        actor = _fake_executor_actor()
        first = _scan(repository, "prior-generation", "standard", "complete")
        stale = {**first, "currentSnapshotDigest": "workspace-before-rescan"}
        replacement = _scan(repository, "replacement-generation", "standard", "complete")
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
        events: list[str] = []
        dispatched_roles: list[str] = []
        git_commands: list[list[str]] = []
        original_capture = orchestrator.capture_git_baseline
        original_attribute = orchestrator.attribute_git_mutations
        original_run_git = git_module.run_git
        original_validate = orchestrator.validate_scan_provenance

        def capture(*args, **kwargs):
            events.append("baseline")
            if capture_error:
                raise OSError("baseline unavailable")
            return original_capture(*args, **kwargs)

        def attribute(*args, **kwargs):
            events.append("attribution")
            if attribution_error:
                raise OSError("attribution unavailable")
            if unknown_attribution:
                return {
                    "status": "unknown",
                    "unknown_paths": ["tracked.txt"],
                    "observed_at": "2026-09-27T00:00:00Z",
                }
            return original_attribute(*args, **kwargs)

        def logged_run_git(args, *call_args, **kwargs):
            git_commands.append([str(item) for item in args])
            return original_run_git(args, *call_args, **kwargs)

        def validate(*args, **kwargs):
            events.append("security_provenance")
            return original_validate(*args, **kwargs)

        def fake_dispatch(**kwargs):
            events.append("dispatch")
            dispatched_roles.append(kwargs["role"])
            provider.scans = [stale, replacement]
            phase_provenance.append(_fake_executor_phase(actor))
            self.assertIsNone(orchestrator._relative_repository_path_lexical(kwargs["output_path"], repository))
            if mutation is not None:
                mutation(repository, tracked, run_dir, kwargs["output_path"])
            for control_path, control_data in (
                (run_dir / "run_state.json", {"host": "state update"}),
                (run_dir / "provenance.json", {"host": "provenance update"}),
            ):
                try:
                    orchestrator._safe_atomic_write_control_json(control_path, control_data)
                except OSError:
                    pass
            if write_report and not kwargs["output_path"].is_dir() and not kwargs["output_path"].is_symlink():
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
            if dispatch_error:
                raise RuntimeError("executor dispatch failed after the test mutation")

        with patch("dual_codex.orchestrator.CodexSecurityProvider", return_value=provider), patch(
            "dual_codex.orchestrator.capture_git_baseline", side_effect=capture
        ), patch("dual_codex.orchestrator.attribute_git_mutations", side_effect=attribute), patch(
            "dual_codex.git.run_git", side_effect=logged_run_git
        ), patch("dual_codex.orchestrator.validate_scan_provenance", side_effect=validate), patch(
            "dual_codex.orchestrator._write_provenance"
        ), patch("dual_codex.orchestrator._dispatch_phase", side_effect=fake_dispatch):
            failure = None
            try:
                _ensure_security_gate_fresh(
                    config=config,
                    requirement=(True, "standard", "."),
                    target_revision="rev-1",
                    run_state=run_state,
                    canonical_root=repository,
                    run_dir=run_dir,
                    phase_provenance=phase_provenance,
                    state_lock=threading.RLock(),
                    checkpoint="before_reviewer",
                )
            except SecurityScanError as exc:
                failure = exc

        try:
            persisted = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            persisted = {}
        return {
            "repository": repository,
            "tracked": tracked,
            "ignored": ignored_file,
            "external_target": external_target,
            "run_dir": run_dir,
            "run_state": run_state,
            "persisted": persisted,
            "failure": failure,
            "events": events,
            "dispatched_roles": dispatched_roles,
            "git_commands": git_commands,
        }

    def test_scan_only_turn_host_detects_mutations_before_security_provenance(self) -> None:
        def modify_ignored_file(repository, _tracked, _run, _output):
            (repository / "pre-existing-ignored.txt").write_text("ignored changed\n", encoding="utf-8")

        def add_and_hide_untracked_file(repository, _tracked, _run, _output):
            (repository / "hidden-source.py").write_text("malicious source\n", encoding="utf-8")
            with (repository / ".git" / "info" / "exclude").open("a", encoding="utf-8") as stream:
                stream.write("\nhidden-source.py\n")

        mutations = {
            "tracked modification": lambda _repo, tracked, _run, _output: tracked.write_text("changed secret content\n", encoding="utf-8"),
            "untracked addition": lambda repo, _tracked, _run, _output: (repo / "added.txt").write_text("new file\n", encoding="utf-8"),
            "pre-existing ignored modification": modify_ignored_file,
            "nested pre-existing ignored modification": lambda repo, _tracked, _run, _output: (
                (repo / "pre-existing-ignored-dir" / "nested" / "baseline.py").write_text(
                    "nested ignored changed\n", encoding="utf-8"
                )
            ),
            "nested ignored file addition": lambda repo, _tracked, _run, _output: (
                (repo / "pre-existing-ignored-dir" / "nested" / "added.py").write_text(
                    "nested ignored addition\n", encoding="utf-8"
                )
            ),
            "new source hidden by git info exclude": add_and_hide_untracked_file,
            "git info exclude edit": lambda repo, _tracked, _run, _output: (repo / ".git" / "info" / "exclude").write_text(
                "pre-existing-ignored.txt\nnew-pattern\n", encoding="utf-8"
            ),
            "local Git config edit": lambda repo, _tracked, _run, _output: subprocess.run(
                ["git", "config", "--local", "dual-agents.scan-only-probe", "changed"],
                cwd=repo,
                check=True,
            ),
            "loose branch ref addition": lambda repo, _tracked, _run, _output: subprocess.run(
                ["git", "update-ref", "refs/heads/scan-only-created", "HEAD"], cwd=repo, check=True
            ),
            "tag ref addition": lambda repo, _tracked, _run, _output: subprocess.run(
                ["git", "tag", "scan-only-created"], cwd=repo, check=True
            ),
            "hook content edit": lambda repo, _tracked, _run, _output: (
                (repo / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            ),
            "packed refs edit": lambda repo, _tracked, _run, _output: subprocess.run(
                ["git", "pack-refs", "--all", "--prune"], cwd=repo, check=True
            ),
            "pseudoref edit": lambda repo, _tracked, _run, _output: (
                (repo / ".git" / "ORIG_HEAD").write_text(
                    subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, stdout=subprocess.PIPE).stdout,
                    encoding="utf-8",
                )
            ),
            "new object addition": lambda repo, _tracked, _run, _output: subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=repo,
                input="new scan-only object\n",
                text=True,
                check=True,
                stdout=subprocess.PIPE,
            ),
            "deletion": lambda _repo, tracked, _run, _output: tracked.unlink(),
            "rename": lambda repo, tracked, _run, _output: tracked.rename(repo / "renamed.txt"),
            "index change": lambda repo, tracked, _run, _output: (
                tracked.write_text("staged change\n", encoding="utf-8"),
                subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True),
            ),
            "unrelated run artifact": lambda _repo, _tracked, run, _output: (run / "executor-added.txt").write_text("not host state\n", encoding="utf-8"),
            "generated-looking bootstrap artifact": lambda repo, _tracked, _run, _output: (
                (repo / ".dual_codex" / "bootstrap" / ".canonical-bootstrap-executor-ABCDEFGH.md").parent.mkdir(
                    parents=True, exist_ok=True
                ),
                (repo / ".dual_codex" / "bootstrap" / ".canonical-bootstrap-executor-ABCDEFGH.md").write_text(
                    "executor-created control-name collision\n", encoding="utf-8"
                ),
            ),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                result = self._run_scan_only_turn(mutation)
                self.assertIsNotNone(result["failure"])
                self.assertEqual(result["failure"].failure_class, "SECURITY_SCAN_ONLY_MUTATION")
                self.assertEqual(result["events"][:3], ["baseline", "dispatch", "attribution"])
                self.assertNotIn("security_provenance", result["events"])
                self.assertEqual(result["dispatched_roles"], ["executor"])
                authority = result["run_state"]["security_scan_authority"]
                self.assertEqual(authority["authority_state"], "failed")
                self.assertEqual(authority.get("selected_scan_id", ""), "")
                self.assertNotIn("security_scan_provenance", result["run_state"])
                diagnostic = result["persisted"]["security_scan_only_mutation_history"][-1]
                self.assertEqual(diagnostic["failure_class"], "SECURITY_SCAN_ONLY_MUTATION")
                self.assertTrue(diagnostic["mutation_categories"])
                self.assertNotIn("changed secret content", json.dumps(diagnostic))
                if name == "tracked modification":
                    self.assertEqual(result["tracked"].read_text(encoding="utf-8"), "changed secret content\n")
                elif name == "deletion":
                    self.assertFalse(result["tracked"].exists())
                elif name == "rename":
                    self.assertTrue((result["repository"] / "renamed.txt").is_file())
                elif name == "untracked addition":
                    self.assertTrue((result["repository"] / "added.txt").is_file())
                elif name == "generated-looking bootstrap artifact":
                    self.assertIn(
                        ".dual_codex/bootstrap/.canonical-bootstrap-executor-ABCDEFGH.md",
                        diagnostic["mutated_paths"]["added"],
                    )
                elif name == "pre-existing ignored modification":
                    self.assertEqual(result["ignored"].read_text(encoding="utf-8"), "ignored changed\n")
                    self.assertIn("pre-existing-ignored.txt", diagnostic["mutated_paths"]["modified"])
                elif name == "nested pre-existing ignored modification":
                    self.assertIn(
                        "pre-existing-ignored-dir/nested/baseline.py",
                        diagnostic["mutated_paths"]["modified"],
                    )
                elif name == "nested ignored file addition":
                    self.assertIn(
                        "pre-existing-ignored-dir/nested/added.py",
                        diagnostic["mutated_paths"]["added"],
                    )
                elif name == "new source hidden by git info exclude":
                    self.assertIn("hidden-source.py", diagnostic["mutated_paths"]["added"])
                    self.assertTrue((result["repository"] / "hidden-source.py").is_file())
                elif name == "git info exclude edit":
                    self.assertIn("git_metadata_changed", diagnostic["mutation_categories"])
                elif name == "unrelated run artifact":
                    self.assertTrue((result["repository"] / ".dual_codex" / "runs" / "scan-only" / "executor-added.txt").is_file())
                self.assertFalse(
                    any(command and command[0] == "git" and command[1] in {"reset", "stash", "clean", "revert"}
                        for command in result["git_commands"])
                )

    def test_exact_file_exclusion_does_not_exempt_new_descendant(self) -> None:
        from dual_codex.git import attribute_git_mutations, capture_git_baseline

        repository = _disposable_repo(self)
        excluded_file = repository / "run_state.json"
        excluded_file.write_text("host state", encoding="utf-8")
        baseline = capture_git_baseline(repository)
        excluded_file.unlink()
        excluded_file.mkdir()
        (excluded_file / "evil.txt").write_text("executor file", encoding="utf-8")

        attribution = attribute_git_mutations(
            repository,
            baseline,
            exact_excluded_paths=["run_state.json"],
        )
        self.assertEqual(attribution["status"], "complete")
        self.assertEqual(attribution["run_created_paths"], ["run_state.json/evil.txt"])

    def test_ignored_directory_tree_is_snapshotted_recursively(self) -> None:
        from dual_codex.git import attribute_git_mutations, capture_git_baseline

        repository = _disposable_repo(self)
        ignored_dir = repository / "ignored-dir"
        nested_file = ignored_dir / "nested" / "baseline.py"
        nested_file.parent.mkdir(parents=True)
        nested_file.write_text("baseline\n", encoding="utf-8")
        with (repository / ".git" / "info" / "exclude").open("a", encoding="utf-8") as stream:
            stream.write("\nignored-dir/\n")

        baseline = capture_git_baseline(repository, include_ignored=True)
        self.assertTrue(baseline["complete"], baseline["worktree_snapshots"])
        self.assertIn("ignored-dir/nested/baseline.py", baseline["worktree_snapshots"])

        nested_file.write_text("changed\n", encoding="utf-8")
        added_file = ignored_dir / "nested" / "added.py"
        added_file.write_text("added\n", encoding="utf-8")
        attribution = attribute_git_mutations(repository, baseline, include_ignored=True)
        self.assertEqual(attribution["status"], "complete", attribution)
        self.assertIn("ignored-dir/nested/baseline.py", attribution["run_touched_paths"])
        self.assertIn("ignored-dir/nested/added.py", attribution["run_created_paths"])

    def test_scan_only_capture_and_attribution_errors_fail_closed(self) -> None:
        for options, expected_events in (
            ({"capture_error": True}, ["baseline"]),
            ({"attribution_error": True}, ["baseline", "dispatch", "attribution"]),
        ):
            with self.subTest(options=options):
                result = self._run_scan_only_turn(**options)
                self.assertEqual(result["failure"].failure_class, "SECURITY_SCAN_ONLY_MUTATION_UNKNOWN")
                self.assertEqual(result["events"], expected_events)
                self.assertNotIn("security_provenance", result["events"])
                self.assertNotIn("security_scan_provenance", result["run_state"])
                self.assertEqual(result["dispatched_roles"], [] if options.get("capture_error") else ["executor"])

    def test_scan_only_dispatch_error_is_attributed_before_bubbling(self) -> None:
        result = self._run_scan_only_turn(
            lambda _repo, tracked, _run, _output: tracked.write_text("changed before failure\n", encoding="utf-8"),
            dispatch_error=True,
        )
        self.assertEqual(result["failure"].failure_class, "SECURITY_SCAN_ONLY_MUTATION")
        self.assertEqual(result["events"][:3], ["baseline", "dispatch", "attribution"])
        self.assertNotIn("security_provenance", result["events"])
        self.assertEqual(result["tracked"].read_text(encoding="utf-8"), "changed before failure\n")

    def test_scan_only_state_symlink_never_overwrites_target_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            protected_target = Path(temporary) / "protected.txt"
            protected_target.write_text("do not overwrite\n", encoding="utf-8")
            probe = Path(temporary) / "symlink-probe"
            try:
                probe.symlink_to(protected_target)
            except (OSError, NotImplementedError) as exc:
                raise unittest.SkipTest(f"symlink creation is unavailable: {type(exc).__name__}") from exc
            probe.unlink()

            def link_run_state_to_external_target(_repo, _tracked, run, _output):
                state_path = run / "run_state.json"
                state_path.unlink()
                state_path.symlink_to(protected_target)

            result = self._run_scan_only_turn(
                link_run_state_to_external_target,
                external_run_dir=False,
            )
            self.assertEqual(result["failure"].failure_class, "SECURITY_SCAN_ONLY_MUTATION_UNKNOWN")
            self.assertEqual(result["events"][:3], ["baseline", "dispatch", "attribution"])
            self.assertNotIn("security_provenance", result["events"])
            self.assertNotIn("security_scan_provenance", result["run_state"])
            self.assertEqual(protected_target.read_text(encoding="utf-8"), "do not overwrite\n")
            diagnostic_files = list(result["run_dir"].glob("security-scan-only-mutation-*.json"))
            self.assertEqual(len(diagnostic_files), 1)
            diagnostic = json.loads(diagnostic_files[0].read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["failure_class"], "SECURITY_SCAN_ONLY_MUTATION_UNKNOWN")
            self.assertTrue(
                any(
                    path.endswith("run_state.json")
                    for paths in diagnostic["mutated_paths"].values()
                    for path in paths
                ),
                diagnostic["mutated_paths"],
            )

    def test_scan_only_output_symlink_is_rejected_without_reading_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            protected_target = Path(temporary) / "protected-report.txt"
            protected_target.write_text("do not overwrite or adopt\n", encoding="utf-8")
            probe = Path(temporary) / "symlink-probe"
            try:
                probe.symlink_to(protected_target)
            except (OSError, NotImplementedError) as exc:
                raise unittest.SkipTest(f"symlink creation is unavailable: {type(exc).__name__}") from exc
            probe.unlink()

            def link_output_to_external_target(_repo, _tracked, _run, output):
                output.symlink_to(protected_target)

            result = self._run_scan_only_turn(
                link_output_to_external_target,
                write_report=False,
            )
            self.assertEqual(result["failure"].failure_class, "SECURITY_SCAN_ONLY_OUTPUT_INVALID")
            self.assertEqual(result["events"][:3], ["baseline", "dispatch", "attribution"])
            self.assertNotIn("security_provenance", result["events"])
            self.assertNotIn("security_scan_provenance", result["run_state"])
            self.assertEqual(protected_target.read_text(encoding="utf-8"), "do not overwrite or adopt\n")

    def test_scan_only_unknown_attribution_fails_closed_without_provenance_validation(self) -> None:
        result = self._run_scan_only_turn(unknown_attribution=True)
        self.assertIsNotNone(result["failure"])
        self.assertEqual(result["failure"].failure_class, "SECURITY_SCAN_ONLY_MUTATION_UNKNOWN")
        self.assertEqual(result["events"][:3], ["baseline", "dispatch", "attribution"])
        self.assertNotIn("security_provenance", result["events"])
        diagnostic = result["persisted"]["security_scan_only_mutation_history"][-1]
        self.assertEqual(diagnostic["attribution_status"], "unknown")
        self.assertEqual(diagnostic["mutated_paths"]["unknown"], ["tracked.txt"])
        self.assertEqual(diagnostic["failure_class"], "SECURITY_SCAN_ONLY_MUTATION_UNKNOWN")

    def test_clean_scan_only_turn_ignores_exact_host_control_files(self) -> None:
        result = self._run_scan_only_turn()
        self.assertIsNone(result["failure"])
        self.assertEqual(
            result["events"],
            ["baseline", "dispatch", "attribution", "security_provenance"],
        )
        self.assertEqual(result["dispatched_roles"], ["executor"])
        self.assertEqual(result["run_state"]["security_scan_authority"]["authority_state"], "completed_fresh")
        diagnostic = result["persisted"]["security_scan_only_mutation_history"][-1]
        self.assertEqual(diagnostic["attribution_status"], "complete")
        self.assertIsNone(diagnostic["failure_class"])
        self.assertEqual(diagnostic["mutation_categories"], [])

    def test_scan_only_baseline_fails_closed_on_repository_symlink_targets(self) -> None:
        result = self._run_scan_only_turn(external_symlink=True)
        self.assertEqual(result["failure"].failure_class, "SECURITY_SCAN_ONLY_MUTATION_UNKNOWN")
        self.assertEqual(result["events"], ["baseline"])
        self.assertEqual(result["dispatched_roles"], [])
        self.assertNotIn("security_provenance", result["events"])
        self.assertEqual(result["external_target"].read_text(encoding="utf-8"), "external baseline\n")

    def test_security_generation_budget_matches_correction_cycles_and_bounds_rescans(self) -> None:
        from types import SimpleNamespace

        self.assertEqual(
            [_max_security_generations(SimpleNamespace(max_correction_cycles=count)) for count in (0, 1, 2)],
            [2, 3, 4],
        )
        for correction_cycles, expected in ((0, 2), (1, 3), (2, 4)):
            with self.subTest(initial_authority_correction_cycles=correction_cycles):
                initial_repository = _disposable_repo(self)
                initial_provider = _FakeProvider()
                initial_state = {}
                initial_config = SimpleNamespace(
                    repository=initial_repository,
                    max_correction_cycles=correction_cycles,
                    agent_for_role=lambda _role: _fake_executor_actor(),
                )
                with patch("dual_codex.orchestrator.CodexSecurityProvider", return_value=initial_provider), patch(
                    "dual_codex.orchestrator._persist_run_state"
                ):
                    _prepare_security_scan(
                        config=initial_config,
                        requirement=(True, "standard", "."),
                        target_revision="rev-1",
                        run_state=initial_state,
                        canonical_root=initial_repository,
                        run_dir=initial_repository,
                        phase_provenance=[],
                        checkpoint="before_executor",
                        authorize_executor_dispatch=True,
                    )
                self.assertEqual(initial_state["security_scan_authority"]["max_generations"], expected)

        repository = _disposable_repo(self)
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
        with patch("dual_codex.orchestrator.CodexSecurityProvider", return_value=provider), patch(
            "dual_codex.orchestrator._persist_run_state"
        ):
            _, decision, _ = _prepare_security_scan(
                **common,
                checkpoint="before_executor",
                authorize_executor_dispatch=True,
            )
            self.assertEqual(decision.action, "start")
            self.assertEqual(run_state["security_scan_authority"]["max_generations"], 4)
            for generation in range(1, 5):
                scan = _scan(repository, f"generation-{generation}", "standard", "complete")
                provider.scans.append(scan)
                phase_provenance.append(_fake_executor_phase(actor))
                _record_security_scan_result(
                    provider=provider,
                    decision=decision,
                    checkpoint=f"generation_{generation}",
                    implementation={
                        "security_scan_provenance": _evidence(repository, f"generation-{generation}")
                    },
                    run_state=run_state,
                    phase_provenance=phase_provenance,
                    config=config,
                    canonical_root=repository,
                    run_dir=repository,
                )
                self.assertEqual(run_state["security_scan_authority"]["generation"], generation)
                self.assertEqual(run_state["security_scan_authority"]["max_generations"], 4)
                stale = {**scan, "currentSnapshotDigest": f"workspace-after-generation-{generation}"}
                provider.scans[-1] = stale
                if generation < 4:
                    _, decision, _ = _prepare_security_scan(
                        **common,
                        checkpoint=f"before_generation_{generation + 1}",
                        authorize_executor_dispatch=True,
                    )
                    self.assertEqual(decision.action, "start")
                    self.assertEqual(run_state["security_scan_authority"]["generation"], generation + 1)
                else:
                    with self.assertRaises(SecurityScanError) as raised:
                        _prepare_security_scan(**common, checkpoint="beyond_generation_budget")

        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_RESCAN_LIMIT")
        self.assertEqual(run_state["security_scan_authority"]["generation"], 4)
        self.assertEqual(run_state["security_scan_authority"]["authority_state"], "completed_stale")
        events = [event.get("event") for event in run_state["security_scan_authority_history"]]
        self.assertEqual(events.count("generation_authorized"), 3)
        self.assertEqual(events.count("rescan_limit"), 1)

    def test_security_freshness_loop_uses_bounded_generation_helper(self) -> None:
        from types import SimpleNamespace
        import threading

        repository = _disposable_repo(self)
        run_dir = repository / "runs"
        run_dir.mkdir()
        config = SimpleNamespace(repository=repository, project_root=Path.cwd(), max_correction_cycles=0)
        provider = object()
        decision = SimpleNamespace(action="start")

        def fake_dispatch(**kwargs):
            kwargs["output_path"].write_text("{}", encoding="utf-8")

        with patch("dual_codex.orchestrator._max_security_generations", wraps=_max_security_generations) as budget, patch(
            "dual_codex.orchestrator._prepare_security_scan", return_value=(provider, decision, "")
        ) as prepare, patch("dual_codex.orchestrator._persist_run_state"), patch(
            "dual_codex.orchestrator._dispatch_phase", side_effect=fake_dispatch
        ), patch("dual_codex.orchestrator._record_security_scan_result") as record:
            with self.assertRaises(SecurityScanError) as raised:
                _ensure_security_gate_fresh(
                    config=config,
                    requirement=(True, "standard", "."),
                    target_revision="rev-1",
                    run_state={},
                    canonical_root=repository,
                    run_dir=run_dir,
                    phase_provenance=[],
                    state_lock=threading.RLock(),
                    checkpoint="before_reviewer",
                )

        self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_RESCAN_LIMIT")
        self.assertEqual(budget.call_count, 1)
        self.assertEqual(prepare.call_count, 3)
        self.assertEqual(record.call_count, 2)

    def test_security_freshness_loop_accepts_fresh_last_generation(self) -> None:
        from types import SimpleNamespace
        import threading

        repository = _disposable_repo(self)
        run_dir = repository / "runs"
        run_dir.mkdir()
        config = SimpleNamespace(repository=repository, project_root=Path.cwd(), max_correction_cycles=0)
        provider = object()
        decisions = iter((SimpleNamespace(action="start"), SimpleNamespace(action="start"), SimpleNamespace(action="reused")))

        def fake_dispatch(**kwargs):
            kwargs["output_path"].write_text("{}", encoding="utf-8")

        with patch("dual_codex.orchestrator._prepare_security_scan", side_effect=lambda **_kwargs: (provider, next(decisions), "")), patch(
            "dual_codex.orchestrator._persist_run_state"
        ), patch("dual_codex.orchestrator._dispatch_phase", side_effect=fake_dispatch), patch(
            "dual_codex.orchestrator._record_security_scan_result"
        ) as record:
            dispatched = _ensure_security_gate_fresh(
                config=config,
                requirement=(True, "standard", "."),
                target_revision="rev-1",
                run_state={},
                canonical_root=repository,
                run_dir=run_dir,
                phase_provenance=[],
                state_lock=threading.RLock(),
                checkpoint="before_reviewer",
            )

        self.assertTrue(dispatched)
        self.assertEqual(record.call_count, 2)

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
