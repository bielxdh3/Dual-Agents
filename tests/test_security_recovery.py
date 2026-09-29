from __future__ import annotations

from collections import deque
import copy
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dual_codex.app_server import (
    AppServerError,
    _AppServerProcess,
    _validate_security_cancel_tool_catalog,
    _validate_security_recovery_native_tool_policy,
    run_codex_security_cancel_turn,
)
from dual_codex.config import AccountConfig, OrchestratorConfig
from dual_codex.security_recovery import SecurityRecoveryError, cancel_security_scan
from dual_codex.security_scan import stable_target_id


SCAN_ID = "3451b87d-834a-4186-a891-c29f3ca39055"
DEEP_SCAN_ID = "964f6fef-4dac-4401-8143-5814bb0acdf3"
OWNER_THREAD = "01a0e004-72d8-7bf2-bec9-01862e53aafb"
REVISION = "8bedd62487ea19c9d624d83907e5f1d5cc58a4d7"


class _FakeSecurityProvider:
    def __init__(self, scans: list[dict], *, admin_result: dict | None = None):
        self.scans = copy.deepcopy(scans)
        self.admin_result = admin_result
        self.admin_calls: list[str] = []

    def list_target_scans(self, _target: Path) -> list[dict]:
        return copy.deepcopy(self.scans)

    def set_status(self, status: str) -> None:
        self.scans[0]["progress"]["status"] = status

    def cancel_ownerless_deep_scan(self, scan_id: str) -> dict:
        self.admin_calls.append(scan_id)
        if self.admin_result is not None:
            result = dict(self.admin_result)
            after_status = result.pop("ledger_after_status", None)
            if after_status:
                self.set_status(str(after_status))
            return result
        self.set_status("canceled")
        return {
            "tool": "cancel_codex_security_scan_from_app",
            "scan_id": scan_id,
            "tool_available": True,
            "tool_call_attempted": True,
            "result_returned": True,
            "provider_error": False,
            "result_scan_id_matches": True,
            "result_status": "canceled",
        }


def _scan(repository: Path, *, mode: str = "standard", status: str = "running") -> dict:
    return {
        "scanId": SCAN_ID if mode == "standard" else DEEP_SCAN_ID,
        "targetPath": str(repository),
        "targetId": stable_target_id(repository),
        "targetRevision": REVISION,
        "scope": ".",
        "mode": mode,
        "progress": {"status": status},
    }


def _config(root: Path) -> OrchestratorConfig:
    account = AccountConfig(
        name="codex-secundario",
        label="Executor",
        codex_home=root / "profile",
        model="",
        reasoning_effort="high",
        backend="app_server",
    )
    return OrchestratorConfig(
        repository=root / "repo",
        runs_dir=root / "runs",
        max_correction_cycles=1,
        require_clean_git=True,
        codex_command="codex",
        accounts={account.name: account},
        roles={"executor": account.name},
        project_root=root,
        config_path=root / "config.toml",
    )


def _init_repo(root: Path) -> Path:
    repository = root / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Recovery Test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "recovery-test@example.invalid"], cwd=repository, check=True)
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "test baseline"], cwd=repository, check=True)
    return repository


def _owner_result() -> dict:
    root = "C:/test/repo"
    return {
        "thread_id": OWNER_THREAD,
        "turn_id": "turn-1",
        "thread_resumed": True,
        "thread_binding": {
            "cwd": root,
            "runtimeWorkspaceRoots": [root],
            "environments": [{"environmentId": "local", "cwd": root, "runtimeWorkspaceRoots": [root]}],
        },
        "turn_state": "completed",
        "approval_policy": "on-request",
        "approval_granted": True,
        "approval_request_count": 1,
        "approval_denial_reason": "",
        "tool_call_attempted": True,
        "tool_call_count": 1,
        "matching_tool_call_count": 1,
        "unexpected_item_seen": False,
        "provider_tool_result": {
            "item_status": "completed",
            "result_returned": True,
            "provider_error": False,
            "provider_error_class": "",
        },
        "approval_state_cleared": True,
        "approval_policy_reset": True,
        "tool_catalog": {"server_count": 2, "available_tool_count": 1, "exact_allowlist_verified": True},
        "tool_catalog_validated": True,
        "native_tool_policy": {
            "verified": True,
            "disabled_features": [
                "apps", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
                "computer_use", "hooks", "image_generation", "multi_agent", "sleep_tool", "shell_tool",
            ],
            "web_search": "disabled",
        },
        "native_tool_policy_validated": True,
        "temporary_tool_policy_cleared": True,
        "failure_class": "",
    }


class SecurityRecoveryTests(unittest.TestCase):
    def _run(
        self,
        root: Path,
        provider: _FakeSecurityProvider,
        *,
        mode: str = "standard",
        owner_thread: str | None = OWNER_THREAD,
        ownerless_admin: bool = False,
    ) -> dict:
        repository = root / "repo"
        with patch("dual_codex.security_recovery.CodexSecurityProvider", return_value=provider):
            return cancel_security_scan(
                _config(root),
                repository=repository,
                target_path=repository,
                scan_id=SCAN_ID if mode == "standard" else DEEP_SCAN_ID,
                expected_revision=REVISION,
                scope=".",
                mode=mode,
                owner_thread_id=owner_thread,
                ownerless_admin=ownerless_admin,
            )

    def test_preflight_exception_is_finalized_without_claiming_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = _init_repo(root)
            provider = _FakeSecurityProvider([_scan(repository)])
            failure = AttributeError("fixture")
            failure.failure_class = "private provider detail / " + ("x" * 200)
            with patch("dual_codex.security_recovery.capture_git_baseline", side_effect=failure), patch(
                "dual_codex.security_recovery.CodexSecurityProvider", return_value=provider
            ) as provider_constructor:
                result = self._run(root, provider)

            provider_constructor.assert_not_called()
            self.assertFalse(result["cancellation_attempted"])
            self.assertIsNone(result["ledger_before"])
            self.assertIsNone(result["ledger_after"])
            self.assertFalse(result["scan_terminal"])
            self.assertFalse(result["success"])
            self.assertEqual(result["classification"], "SECURITY_RECOVERY_MUTATION_ATTRIBUTION_FAILED")
            self.assertEqual(result["preflight_failure_class"], "ATTRIBUTEERROR")
            self.assertLessEqual(len(result["preflight_failure_class"]), 128)
            self.assertRegex(result["preflight_failure_class"], re.compile(r"^[A-Z0-9_]+$"))
            self.assertNotIn("private provider detail", json.dumps(result))
            saved = json.loads(Path(result["provenance_path"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["classification"], result["classification"])
            self.assertEqual(saved["preflight_failure_class"], "ATTRIBUTEERROR")

    def test_incomplete_mutation_baseline_prevents_provider_and_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = _init_repo(root)
            provider = _FakeSecurityProvider([_scan(repository)])
            incomplete_baseline = {
                "repository": str(repository.resolve()),
                "complete": False,
                "worktree_snapshots": {"unsafe-link": {"kind": "unknown", "reason": "reparse_point"}},
            }
            unknown_attribution = {
                "status": "unknown",
                "unknown_paths": ["unsafe-link"],
            }
            with patch("dual_codex.security_recovery.capture_git_baseline", return_value=incomplete_baseline), patch(
                "dual_codex.security_recovery.attribute_git_mutations", return_value=unknown_attribution
            ), patch("dual_codex.security_recovery.CodexSecurityProvider", return_value=provider) as provider_constructor:
                result = self._run(root, provider)

            provider_constructor.assert_not_called()
            self.assertFalse(result["cancellation_attempted"])
            self.assertFalse(result["tool_call_attempted"])
            self.assertIsNone(result["ledger_before"])
            self.assertIsNone(result["ledger_after"])
            self.assertEqual(result["preflight_failure_class"], "SECURITY_RECOVERY_MUTATION_BASELINE_INCOMPLETE")
            self.assertEqual(result["classification"], "SECURITY_RECOVERY_MUTATION_ATTRIBUTION_FAILED")
            self.assertEqual(result["mutation_attribution"]["status"], "unknown")
            self.assertEqual(result["mutation_attribution"]["unknown_path_count"], 1)

    def test_cancellation_requires_exact_owner_or_explicit_deep_admin_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            with patch("dual_codex.security_recovery.CodexSecurityProvider") as provider:
                with self.assertRaises(SecurityRecoveryError):
                    cancel_security_scan(
                        _config(root),
                        repository=root / "repo",
                        target_path=root / "repo",
                        scan_id=SCAN_ID,
                        expected_revision=REVISION,
                        scope=".",
                        mode="standard",
                    )
                with self.assertRaises(SecurityRecoveryError):
                    cancel_security_scan(
                        _config(root),
                        repository=root / "repo",
                        target_path=root / "repo",
                        scan_id=SCAN_ID,
                        expected_revision=REVISION,
                        scope=".",
                        mode="standard",
                        ownerless_admin=True,
                    )
                provider.assert_not_called()

    def test_ledger_identity_mismatch_prevents_owner_thread_cancellation(self) -> None:
        for field, value in (
            ("targetPath", "C:/different/repo"),
            ("targetRevision", "f" * 40),
            ("scope", "src"),
            ("mode", "deep"),
            ("targetId", "target_sha256_wrong"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository = _init_repo(root)
                scan = _scan(repository)
                scan[field] = value
                provider = _FakeSecurityProvider([scan])
                with patch("dual_codex.security_recovery.run_codex_security_cancel_turn") as cancel:
                    result = self._run(root, provider)
                cancel.assert_not_called()
                self.assertFalse(result["success"])
                self.assertEqual(result["classification"], "SECURITY_RECOVERY_LEDGER_IDENTITY_MISMATCH")
                self.assertEqual(result["mutation_attribution"]["status"], "complete")

    def test_terminal_scan_is_not_cancelled_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = _init_repo(root)
            provider = _FakeSecurityProvider([_scan(repository, status="complete")])
            with patch("dual_codex.security_recovery.run_codex_security_cancel_turn") as cancel:
                result = self._run(root, provider)
            cancel.assert_not_called()
            self.assertTrue(result["success"])
            self.assertTrue(result["scan_terminal"])
            self.assertEqual(result["classification"], "SECURITY_RECOVERY_ALREADY_TERMINAL")

    def test_terminal_before_action_requires_terminal_final_ledger_confirmation(self) -> None:
        class ChangingProvider(_FakeSecurityProvider):
            def __init__(self, scans: list[dict]):
                super().__init__(scans)
                self.read_count = 0

            def list_target_scans(self, target: Path) -> list[dict]:
                self.read_count += 1
                if self.read_count == 2:
                    self.scans[0]["progress"]["status"] = "running"
                return super().list_target_scans(target)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = _init_repo(root)
            provider = ChangingProvider([_scan(repository, status="complete")])
            with patch("dual_codex.security_recovery.run_codex_security_cancel_turn") as cancel:
                result = self._run(root, provider)
            cancel.assert_not_called()
            self.assertFalse(result["success"])
            self.assertFalse(result["scan_terminal"])
            self.assertEqual(result["classification"], "SECURITY_RECOVERY_LEDGER_STATE_UNCONFIRMED")

    def test_clean_owner_thread_cancellation_persists_bounded_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = _init_repo(root)
            provider = _FakeSecurityProvider([_scan(repository)])

            def cancel(**_kwargs):
                provider.set_status("canceled")
                result = _owner_result()
                result["thread_binding"]["cwd"] = str(repository)
                result["thread_binding"]["runtimeWorkspaceRoots"] = [str(repository)]
                result["thread_binding"]["environments"][0]["cwd"] = str(repository)
                result["thread_binding"]["environments"][0]["runtimeWorkspaceRoots"] = [str(repository)]
                return result

            with patch("dual_codex.security_recovery.run_codex_security_cancel_turn", side_effect=cancel) as call:
                result = self._run(root, provider)
            call.assert_called_once()
            self.assertEqual(call.call_args.kwargs["owner_thread_id"], OWNER_THREAD)
            self.assertEqual(call.call_args.kwargs["scan_id"], SCAN_ID)
            self.assertTrue(result["success"])
            self.assertTrue(result["scan_terminal"])
            self.assertEqual(result["classification"], "SECURITY_RECOVERY_CANCELLED_BY_OWNER_THREAD")
            self.assertTrue(result["approval"]["approval_granted"])
            self.assertEqual(result["approval"]["allowed_tool"], "codex-security.cancel_codex_security_scan")
            self.assertTrue(Path(result["provenance_path"]).is_file())
            self.assertNotEqual(Path(result["provenance_path"]).parent, repository)
            saved = json.loads(Path(result["provenance_path"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["authorized_scan_id"], SCAN_ID)
            self.assertEqual(saved["provider_outcome"]["tool_call_count"], 1)
            self.assertEqual(saved["mutation_attribution"]["status"], "complete")

    def test_claimed_provider_success_with_running_ledger_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = _init_repo(root)
            provider = _FakeSecurityProvider([_scan(repository)])
            owner_result = _owner_result()
            owner_result["thread_binding"]["cwd"] = str(repository)
            owner_result["thread_binding"]["runtimeWorkspaceRoots"] = [str(repository)]
            owner_result["thread_binding"]["environments"][0]["cwd"] = str(repository)
            owner_result["thread_binding"]["environments"][0]["runtimeWorkspaceRoots"] = [str(repository)]
            with patch("dual_codex.security_recovery.run_codex_security_cancel_turn", return_value=owner_result):
                result = self._run(root, provider)
            self.assertFalse(result["success"])
            self.assertFalse(result["scan_terminal"])
            self.assertEqual(result["classification"], "SECURITY_RECOVERY_SCAN_REMAINS_ACTIVE")

    def test_terminal_result_with_changed_ledger_identity_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = _init_repo(root)
            provider = _FakeSecurityProvider([_scan(repository)])

            def cancel(**_kwargs):
                provider.set_status("canceled")
                provider.scans[0]["targetRevision"] = "f" * 40
                result = _owner_result()
                result["thread_binding"]["cwd"] = str(repository)
                result["thread_binding"]["runtimeWorkspaceRoots"] = [str(repository)]
                result["thread_binding"]["environments"][0]["cwd"] = str(repository)
                result["thread_binding"]["environments"][0]["runtimeWorkspaceRoots"] = [str(repository)]
                return result

            with patch("dual_codex.security_recovery.run_codex_security_cancel_turn", side_effect=cancel):
                result = self._run(root, provider)
            self.assertFalse(result["success"])
            self.assertTrue(result["scan_terminal"])
            self.assertFalse(result["ledger_after_identity_matches"])
            self.assertEqual(result["classification"], "SECURITY_RECOVERY_LEDGER_IDENTITY_UNCONFIRMED")

    def test_scan_not_found_with_active_ledger_is_provider_context_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = _init_repo(root)
            provider = _FakeSecurityProvider([_scan(repository)])
            owner_result = _owner_result()
            owner_result["thread_binding"]["cwd"] = str(repository)
            owner_result["thread_binding"]["runtimeWorkspaceRoots"] = [str(repository)]
            owner_result["thread_binding"]["environments"][0]["cwd"] = str(repository)
            owner_result["thread_binding"]["environments"][0]["runtimeWorkspaceRoots"] = [str(repository)]
            owner_result["provider_tool_result"]["provider_error"] = True
            owner_result["provider_tool_result"]["provider_error_class"] = "scan_not_found"
            with patch("dual_codex.security_recovery.run_codex_security_cancel_turn", return_value=owner_result):
                result = self._run(root, provider)
            self.assertFalse(result["success"])
            self.assertEqual(result["ledger_after"]["status"], "running")
            self.assertEqual(result["classification"], "SECURITY_RECOVERY_PROVIDER_CONTEXT_MISMATCH")

    def test_timeout_cleanup_and_bounded_timeout_provenance_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = _init_repo(root)
            provider = _FakeSecurityProvider([_scan(repository)])
            owner_result = _owner_result()
            owner_result["thread_binding"]["cwd"] = str(repository)
            owner_result["thread_binding"]["runtimeWorkspaceRoots"] = [str(repository)]
            owner_result["thread_binding"]["environments"][0]["cwd"] = str(repository)
            owner_result["thread_binding"]["environments"][0]["runtimeWorkspaceRoots"] = [str(repository)]
            owner_result.update(
                {
                    "turn_state": "failed",
                    "failure_class": "HOST_TURN_DEADLINE",
                    "turn_cancel_requested": True,
                    "turn_cancel_confirmed": False,
                    "turn_provenance": {
                        "termination_classification": "HOST_TURN_DEADLINE",
                        "timeout_source": "profile_override",
                        "turn_timeout_seconds": 30,
                        "host_deadline_expired": True,
                        "app_server_process_alive_at_failure": True,
                        "provider_payload": "must not be persisted",
                    },
                }
            )
            with patch("dual_codex.security_recovery.run_codex_security_cancel_turn", return_value=owner_result):
                result = self._run(root, provider)
            execution = result["owner_thread_execution"]
            self.assertTrue(execution["turn_cancel_requested"])
            self.assertFalse(execution["turn_cancel_confirmed"])
            self.assertEqual(
                execution["turn_timeout_provenance"],
                {
                    "termination_classification": "HOST_TURN_DEADLINE",
                    "timeout_source": "profile_override",
                    "turn_timeout_seconds": 30,
                    "host_deadline_expired": True,
                    "app_server_process_alive_at_failure": True,
                },
            )

    def test_worktree_mutation_fails_even_when_provider_cancels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = _init_repo(root)
            provider = _FakeSecurityProvider([_scan(repository)])

            def cancel(**_kwargs):
                provider.set_status("canceled")
                (repository / "unexpected.txt").write_text("mutation\n", encoding="utf-8")
                result = _owner_result()
                result["thread_binding"]["cwd"] = str(repository)
                result["thread_binding"]["runtimeWorkspaceRoots"] = [str(repository)]
                result["thread_binding"]["environments"][0]["cwd"] = str(repository)
                result["thread_binding"]["environments"][0]["runtimeWorkspaceRoots"] = [str(repository)]
                return result

            with patch("dual_codex.security_recovery.run_codex_security_cancel_turn", side_effect=cancel):
                result = self._run(root, provider)
            self.assertFalse(result["success"])
            self.assertTrue(result["scan_terminal"])
            self.assertEqual(result["classification"], "SECURITY_RECOVERY_MUTATION_ATTRIBUTION_FAILED")
            self.assertGreater(result["mutation_attribution"]["created_path_count"], 0)

    def test_git_metadata_mutation_fails_even_when_provider_cancels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = _init_repo(root)
            provider = _FakeSecurityProvider([_scan(repository)])

            def cancel(**_kwargs):
                provider.set_status("canceled")
                config_file = repository / ".git" / "config"
                config_file.write_text(config_file.read_text(encoding="utf-8") + "\n# unexpected\n", encoding="utf-8")
                result = _owner_result()
                result["thread_binding"]["cwd"] = str(repository)
                result["thread_binding"]["runtimeWorkspaceRoots"] = [str(repository)]
                result["thread_binding"]["environments"][0]["cwd"] = str(repository)
                result["thread_binding"]["environments"][0]["runtimeWorkspaceRoots"] = [str(repository)]
                return result

            with patch("dual_codex.security_recovery.run_codex_security_cancel_turn", side_effect=cancel):
                result = self._run(root, provider)
            self.assertFalse(result["success"])
            self.assertTrue(result["mutation_attribution"]["repository_metadata_changed"])
            self.assertEqual(result["classification"], "SECURITY_RECOVERY_MUTATION_ATTRIBUTION_FAILED")

    def test_unknown_mutation_attribution_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = _init_repo(root)
            provider = _FakeSecurityProvider([_scan(repository)])
            owner_result = _owner_result()
            owner_result["thread_binding"]["cwd"] = str(repository)
            owner_result["thread_binding"]["runtimeWorkspaceRoots"] = [str(repository)]
            owner_result["thread_binding"]["environments"][0]["cwd"] = str(repository)
            owner_result["thread_binding"]["environments"][0]["runtimeWorkspaceRoots"] = [str(repository)]
            provider.set_status("canceled")
            with patch("dual_codex.security_recovery.run_codex_security_cancel_turn", return_value=owner_result), patch(
                "dual_codex.security_recovery.attribute_git_mutations",
                return_value={"status": "unknown", "run_touched_paths": [], "run_created_paths": [], "run_removed_paths": [], "unknown_paths": []},
            ):
                result = self._run(root, provider)
            self.assertFalse(result["success"])
            self.assertEqual(result["classification"], "SECURITY_RECOVERY_MUTATION_ATTRIBUTION_FAILED")

    def test_ownerless_exact_deep_admin_path_uses_provider_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = _init_repo(root)
            provider = _FakeSecurityProvider([_scan(repository, mode="deep")])
            with patch("dual_codex.security_recovery.run_codex_security_cancel_turn") as app_server:
                result = self._run(root, provider, mode="deep", owner_thread=None, ownerless_admin=True)
            app_server.assert_not_called()
            self.assertEqual(provider.admin_calls, [DEEP_SCAN_ID])
            self.assertTrue(result["success"])
            self.assertEqual(result["classification"], "SECURITY_RECOVERY_CANCELLED_BY_APP_ADMIN")
            self.assertEqual(result["approval"]["allowed_tool"], "codex-security.cancel_codex_security_scan_from_app")
            self.assertFalse(result["approval"]["approval_granted"])
            self.assertFalse(result["approval"]["approval_required"])

    def test_ownerless_admin_unsupported_is_explicit_and_does_not_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = _init_repo(root)
            provider = _FakeSecurityProvider(
                [_scan(repository, mode="deep")],
                admin_result={
                    "tool": "cancel_codex_security_scan_from_app",
                    "scan_id": DEEP_SCAN_ID,
                    "tool_available": False,
                    "tool_call_attempted": False,
                    "result_returned": False,
                    "provider_error": True,
                    "failure_class": "SECURITY_SCAN_OWNERLESS_ADMIN_UNSUPPORTED",
                },
            )
            with patch("dual_codex.security_recovery.run_codex_security_cancel_turn") as app_server:
                result = self._run(root, provider, mode="deep", owner_thread=None, ownerless_admin=True)
            app_server.assert_not_called()
            self.assertFalse(result["success"])
            self.assertFalse(result["tool_call_attempted"])
            self.assertEqual(result["classification"], "SECURITY_SCAN_OWNERLESS_ADMIN_UNSUPPORTED")
            self.assertEqual(result["ledger_after"]["status"], "running")
            self.assertEqual(result["mutation_attribution"]["status"], "complete")

    def test_ownerless_admin_requires_matching_terminal_provider_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = _init_repo(root)
            provider = _FakeSecurityProvider(
                [_scan(repository, mode="deep")],
                admin_result={
                    "tool": "cancel_codex_security_scan_from_app",
                    "scan_id": DEEP_SCAN_ID,
                    "tool_available": True,
                    "tool_call_attempted": True,
                    "result_returned": True,
                    "provider_error": False,
                    "result_scan_id_matches": False,
                    "result_status": "canceled",
                    "ledger_after_status": "canceled",
                },
            )
            result = self._run(root, provider, mode="deep", owner_thread=None, ownerless_admin=True)
            self.assertFalse(result["success"])
            self.assertTrue(result["scan_terminal"])
            self.assertEqual(result["classification"], "SECURITY_RECOVERY_PROVIDER_RESULT_UNCONFIRMED")


class SecurityCancelApprovalTests(unittest.TestCase):
    def _process(self, repository: Path, *, scan_id: str = SCAN_ID):
        process = object.__new__(_AppServerProcess)
        sent: list[dict] = []
        process._send = sent.append
        process._events = deque()
        process._event_journal = None
        process._event_publications = None
        process._event_context = {}
        process._active_thread_id = OWNER_THREAD
        process._active_thread_resumed = True
        process._active_turn_provenance = {"thread_id": OWNER_THREAD, "turn_id": "turn-1"}
        process._next_id = 40
        process._active_security_cancel_approval = {
            "operation": "security_scan_cancel",
            "approval_policy": "on-request",
            "binding_validated": True,
            "tool_catalog_validated": True,
            "thread_id": OWNER_THREAD,
            "repository": str(repository),
            "scan_id": scan_id,
            "approval_request_count": 0,
            "approval_granted": False,
            "approval_scope": "",
            "mcp_tool_call_count": 0,
            "matching_tool_call_count": 0,
            "mcp_tool_calls": [],
            "unexpected_item_seen": False,
            "unexpected_item_types": [],
            "turn_cancel_requested": False,
            "turn_cancel_confirmed": False,
        }
        return process, sent

    def _start_item(self, process: _AppServerProcess, *, tool: str = "cancel_codex_security_scan", scan_id: str = SCAN_ID, thread_id: str = OWNER_THREAD, item_id: str = "item-1") -> None:
        process._record_notification(
            {
                "jsonrpc": "2.0",
                "method": "item/started",
                "params": {
                    "threadId": thread_id,
                    "turnId": "turn-1",
                    "startedAtMs": 1,
                    "item": {
                        "id": item_id,
                        "type": "mcpToolCall",
                        "server": "codex-security",
                        "tool": tool,
                        "arguments": {"scanId": scan_id},
                        "status": "inProgress",
                    },
                },
            }
        )

    def _request(self, repository: Path, *, thread_id: str = OWNER_THREAD, turn_id: str = "turn-1", scan_id: str = SCAN_ID) -> dict:
        del repository  # The real elicitation does not carry cwd; host thread binding is validated before the turn.
        return {
            "id": 7,
            "method": "mcpServer/elicitation/request",
            "params": {
                "_meta": {
                    "codex_approval_kind": "mcp_tool_call",
                    "tool_description": "Cancel one Security scan.",
                    "tool_params": {"scanId": scan_id},
                    "tool_params_display": [{"name": "scanId", "value": scan_id}],
                    "tool_title": "Cancel Security scan",
                },
                "message": "Allow the Security tool call?",
                "mode": "form",
                "requestedSchema": {"type": "object", "properties": {}},
                "serverName": "codex-security",
                "threadId": thread_id,
                "turnId": turn_id,
            },
        }

    def test_exact_tool_and_exact_scan_id_are_approved_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, sent = self._process(repository)
            self._start_item(process)
            process._respond_to_server_request(self._request(repository))
            self.assertEqual(sent[-1]["result"], {"action": "accept", "content": None})
            self.assertTrue(process._active_security_cancel_approval["approval_granted"])
            self.assertEqual(process._active_security_cancel_approval["approval_scope"], "one_time")
            process._respond_to_server_request(self._request(repository))
            self.assertEqual(sent[-2]["result"], {"action": "decline", "content": None})
            self.assertEqual(sent[-1]["method"], "turn/cancel")
            self.assertEqual(process._active_security_cancel_approval["approval_request_count"], 2)

    def test_wrong_scan_id_and_wrong_tool_are_denied(self) -> None:
        for tool, scan_id in (
            ("cancel_codex_security_scan", "964f6fef-4dac-4401-8143-5814bb0acdf3"),
            ("start_codex_security_deep_scan", SCAN_ID),
        ):
            with self.subTest(tool=tool, scan_id=scan_id), tempfile.TemporaryDirectory() as directory:
                repository = Path(directory)
                process, sent = self._process(repository)
                self._start_item(process, tool=tool, scan_id=scan_id)
                process._respond_to_server_request(self._request(repository))
                self.assertEqual(sent[-1]["result"], {"action": "decline", "content": None})
                self.assertFalse(process._active_security_cancel_approval["approval_granted"])

    def test_wrong_thread_and_wrong_repository_binding_are_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, sent = self._process(repository)
            self._start_item(process, thread_id="other-thread")
            process._respond_to_server_request(self._request(repository))
            self.assertEqual(sent[-1]["result"], {"action": "decline", "content": None})
            self.assertFalse(process._active_security_cancel_approval["approval_granted"])

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, sent = self._process(repository)
            process._active_security_cancel_approval["binding_validated"] = False
            self._start_item(process)
            process._respond_to_server_request(self._request(repository))
            self.assertEqual(sent[-2]["result"], {"action": "decline", "content": None})
            self.assertEqual(sent[-1]["method"], "turn/cancel")
            self.assertFalse(process._active_security_cancel_approval["approval_granted"])

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, sent = self._process(repository)
            self._start_item(process)
            process._respond_to_server_request(self._request(repository, thread_id="other-thread"))
            self.assertEqual(sent[-2]["result"], {"action": "decline", "content": None})
            self.assertEqual(sent[-1]["method"], "turn/cancel")
            self.assertFalse(process._active_security_cancel_approval["approval_granted"])

    def test_extra_mcp_tool_call_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, sent = self._process(repository)
            self._start_item(process)
            self._start_item(process, tool="get_codex_security_scan", item_id="item-2")
            process._respond_to_server_request(self._request(repository))
            self.assertEqual(sent[-1]["result"], {"action": "decline", "content": None})
            self.assertFalse(process._active_security_cancel_approval["approval_granted"])

    def test_second_identical_mcp_call_immediately_invalidates_maintenance_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, sent = self._process(repository)
            self._start_item(process)
            process._respond_to_server_request(self._request(repository))
            self.assertEqual(sent[-1], {"id": 7, "result": {"action": "accept", "content": None}})
            self._start_item(process, item_id="item-2")
            context = process._active_security_cancel_approval
            self.assertTrue(context["unexpected_item_seen"])
            self.assertIn("multiple_mcp_tool_calls", context["unexpected_item_types"])
            self.assertEqual(sent[-1]["method"], "turn/cancel")

    def test_wrong_server_turn_arguments_and_malformed_requests_are_denied(self) -> None:
        mutations = (
            lambda request: request.update(jsonrpc="2.0"),
            lambda request: request.update(unexpected="field"),
            lambda request: request.pop("id"),
            lambda request: request.update(id=True),
            lambda request: request["params"].update(serverName="other-server"),
            lambda request: request["params"].update(turnId="other-turn"),
            lambda request: request["params"]["_meta"]["tool_params"].update(scanId="other-scan"),
            lambda request: request["params"].update(requestedSchema={"type": "object", "properties": {"approve": {"type": "boolean"}}}),
            lambda request: request["params"]["_meta"].update(persist=[{}]),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate), tempfile.TemporaryDirectory() as directory:
                repository = Path(directory)
                process, sent = self._process(repository)
                self._start_item(process)
                request = self._request(repository)
                mutate(request)
                process._respond_to_server_request(request)
                self.assertEqual(sent[-2]["result"], {"action": "decline", "content": None})
                self.assertFalse(process._active_security_cancel_approval["approval_granted"])
                self.assertEqual(sent[-1]["method"], "turn/cancel")

    def test_elicitation_before_matching_item_and_missing_scan_id_are_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, sent = self._process(repository)
            process._respond_to_server_request(self._request(repository))
            self.assertEqual(sent[-2]["result"], {"action": "decline", "content": None})
            self.assertFalse(process._active_security_cancel_approval["approval_granted"])

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, sent = self._process(repository)
            self._start_item(process)
            request = self._request(repository)
            request["params"]["_meta"]["tool_params"] = {}
            process._respond_to_server_request(request)
            self.assertEqual(sent[-2]["result"], {"action": "decline", "content": None})
            self.assertFalse(process._active_security_cancel_approval["approval_granted"])

    def test_completed_tool_request_cannot_be_replayed_or_approved_after_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, sent = self._process(repository)
            self._start_item(process)
            process._capture_security_cancel_event(
                "item/completed",
                {
                    "threadId": OWNER_THREAD,
                    "turnId": "turn-1",
                    "item": {"id": "item-1", "type": "mcpToolCall", "status": "completed", "result": {"isError": False}},
                },
            )
            process._respond_to_server_request(self._request(repository))
            self.assertEqual(sent[-2]["result"], {"action": "decline", "content": None})
            self.assertFalse(process._active_security_cancel_approval["approval_granted"])

    def test_dynamic_tool_request_is_denied_and_invalidates_maintenance_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, sent = self._process(repository)
            process._respond_to_server_request(
                {"jsonrpc": "2.0", "id": 11, "method": "item/tool/call", "params": {"tool": "unrelated"}}
            )
            self.assertFalse(sent[0]["result"]["success"])
            self.assertEqual(sent[-1]["method"], "turn/cancel")
            self.assertTrue(process._active_security_cancel_approval["unexpected_item_seen"])
            self.assertEqual(sent[-1]["params"], {"threadId": OWNER_THREAD, "turnId": "turn-1"})

    def test_every_unknown_or_action_item_type_invalidates_maintenance_turn(self) -> None:
        for item_type in ("commandExecution", "fileChange", "dynamicToolCall", "imageGeneration", "sleep", "unknownFutureTool"):
            with self.subTest(item_type=item_type), tempfile.TemporaryDirectory() as directory:
                repository = Path(directory)
                process, _sent = self._process(repository)
                process._capture_security_cancel_event(
                    "item/started",
                    {
                        "threadId": OWNER_THREAD,
                        "turnId": "turn-1",
                        "item": {"id": "unrelated", "type": item_type},
                    },
                )
                self.assertTrue(process._active_security_cancel_approval["unexpected_item_seen"])

    def test_non_approval_server_requests_invalidate_maintenance_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, sent = self._process(repository)
            process._respond_to_server_request(
                {"jsonrpc": "2.0", "id": 12, "method": "item/tool/requestUserInput", "params": {}}
            )
            self.assertTrue(process._active_security_cancel_approval["unexpected_item_seen"])
            self.assertIn("item/tool/requestUserInput", process._active_security_cancel_approval["unexpected_item_types"])
            self.assertEqual(sent[0]["result"], {"answers": {}})
            self.assertEqual(sent[-1]["method"], "turn/cancel")

    def test_unexpected_item_requests_exact_turn_cancel_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, sent = self._process(repository)
            notification = {
                "jsonrpc": "2.0",
                "method": "item/started",
                "params": {
                    "threadId": OWNER_THREAD,
                    "turnId": "turn-1",
                    "item": {"id": "item-native", "type": "imageGeneration"},
                },
            }
            process._record_notification(notification)
            process._record_notification(notification)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["method"], "turn/cancel")
            self.assertEqual(sent[0]["params"], {"threadId": OWNER_THREAD, "turnId": "turn-1"})
            self.assertTrue(process._active_security_cancel_approval["turn_cancel_requested"])
            self.assertFalse(process._active_security_cancel_approval["turn_cancel_confirmed"])

            request_id = process._active_security_cancel_approval["turn_cancel_request_id"]
            self.assertTrue(process._consume_security_recovery_turn_cancel_response({"id": request_id, "result": {}}))
            self.assertTrue(process._active_security_cancel_approval["turn_cancel_confirmed"])

    def test_normal_and_unrelated_approval_requests_remain_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, sent = self._process(repository)
            process._active_security_cancel_approval = None
            process._respond_to_server_request(
                {"jsonrpc": "2.0", "id": 8, "method": "item/commandExecution/requestApproval", "params": {}}
            )
            self.assertEqual(sent[-1]["result"], {"decision": "decline"})
            process._respond_to_server_request(self._request(repository))
            self.assertEqual(sent[-1], {"id": 7, "result": {"action": "decline", "content": None}})

    def _lifecycle_process(self, repository: Path, *, fail_turn: bool = False, scan_not_found: bool = False):
        process = object.__new__(_AppServerProcess)
        process._lock = threading.RLock()
        process.config = SimpleNamespace(app_server_thread_timeout=2, app_server_initialize_timeout=2)
        process.agent = SimpleNamespace(sandbox="workspace-write", model="", service_tier="")
        process._event_journal = None
        process._event_context = {}
        process._active_dispatch_provenance = None
        process._active_thread_id = ""
        process._active_thread_resumed = False
        process._active_turn_provenance = None
        process._active_security_cancel_approval = None
        process.security_recovery_mode = True
        process._events = deque()
        process._event_publications = None
        process.last_turn_provenance = {}
        process.last_thread_binding = {}
        process.last_thread_request = {}
        process.request_methods = []
        process._send = lambda _message: None
        process.process = SimpleNamespace(pid=1, poll=lambda: None)
        sent: list[dict] = []
        process._send = sent.append

        def request(method: str, params: dict | None, *, timeout: float):
            process.request_methods.append(method)
            if method == "config/read":
                return {
                    "jsonrpc": "2.0",
                    "id": len(process.request_methods),
                    "result": {
                        "config": {
                            "features": {
                                "apps": False,
                                "browser_use": False,
                                "browser_use_external": False,
                                "browser_use_full_cdp_access": False,
                                "computer_use": False,
                                "hooks": False,
                                "image_generation": False,
                                "multi_agent": False,
                                "sleep_tool": False,
                                "shell_tool": False,
                            },
                            "web_search": "disabled",
                        }
                    },
                }
            if method == "mcpServerStatus/list":
                return {
                    "jsonrpc": "2.0",
                    "id": len(process.request_methods),
                    "result": {
                        "data": [
                            {
                                "name": "codex-security",
                                "pluginId": "codex-security@openai-curated-remote",
                                "tools": {"cancel_codex_security_scan": {}},
                            },
                            {"name": "codex_apps", "pluginId": None, "tools": {}},
                        ],
                        "nextCursor": None,
                    },
                }
            if method == "thread/resume":
                repository_path = params["cwd"]
                roots = list(params["runtimeWorkspaceRoots"])
                thread_id = params["threadId"]
                return {
                    "jsonrpc": "2.0",
                    "id": len(process.request_methods),
                    "result": {
                        "thread": {
                            "id": thread_id,
                            "environments": [{"environmentId": "local", "cwd": repository_path, "runtimeWorkspaceRoots": roots}],
                        },
                        "cwd": repository_path,
                        "runtimeWorkspaceRoots": roots,
                    },
                }
            if method == "turn/cancel":
                return {"jsonrpc": "2.0", "id": len(process.request_methods), "result": {}}
            raise AssertionError(method)

        process.request = request

        def turn(
            thread_id: str,
            _prompt: str,
            cwd: Path,
            *,
            approval_policy: str,
            sandbox_policy: dict,
            persist_thread_mapping: bool,
        ):
            self.assertEqual(approval_policy, "on-request")
            self.assertEqual(sandbox_policy, {"type": "readOnly", "networkAccess": False})
            self.assertFalse(persist_thread_mapping)
            if fail_turn:
                process._active_turn_provenance = {
                    "thread_id": thread_id,
                    "turn_id": "turn-timeout",
                    "termination_classification": "IN_PROGRESS",
                }
                process.last_turn_provenance = dict(process._active_turn_provenance)
                raise AppServerError(
                    "Timed out waiting for App Server JSON-RPC data.",
                    failure_class="APP_SERVER_TRANSPORT_TIMEOUT",
                )
            process._active_turn_provenance = {
                "thread_id": thread_id,
                "turn_id": None,
                "termination_classification": "TURN_COMPLETED",
            }
            process._record_notification(
                {"jsonrpc": "2.0", "method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": "turn-1"}}}
            )
            process._record_notification(
                {
                    "jsonrpc": "2.0",
                    "method": "item/started",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "turn-1",
                        "item": {
                            "id": "item-1",
                            "type": "mcpToolCall",
                            "server": "codex-security",
                            "tool": "cancel_codex_security_scan",
                            "arguments": {"scanId": SCAN_ID},
                            "status": "inProgress",
                        },
                    },
                }
            )
            approval = self._request(cwd, thread_id=thread_id)
            approval["id"] = 5
            process._respond_to_server_request(approval)
            process._record_notification(
                {
                    "jsonrpc": "2.0",
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "turn-1",
                        "item": {
                            "id": "item-1",
                            "type": "mcpToolCall",
                            "status": "failed" if scan_not_found else "completed",
                            "result": {
                                "isError": False,
                                "content": [{"text": "Scan not found"}] if scan_not_found else [],
                            },
                        },
                    },
                }
            )
            return {"turn_id": "turn-1"}

        process._turn_unlocked = turn
        return process, sent

    def test_exact_resume_has_no_thread_start_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, _sent = self._lifecycle_process(repository)
            binding = process._resume_exact_security_thread_unlocked(OWNER_THREAD, repository, approval_policy="never")
            self.assertEqual(process.request_methods, ["thread/resume"])
            self.assertEqual(process._active_thread_id, OWNER_THREAD)
            self.assertEqual(binding["runtimeWorkspaceRoots"], [str(repository)])
            self.assertEqual(process.last_thread_request["approvalPolicy"], "never")

    def test_stale_and_binding_mismatch_fail_closed_without_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, _sent = self._lifecycle_process(repository)
            process.request = lambda method, _params, *, timeout: (
                process.request_methods.append(method) or {"jsonrpc": "2.0", "id": 1, "error": {"message": "thread not found"}}
            )
            with self.assertRaises(AppServerError) as error:
                process._resume_exact_security_thread_unlocked(OWNER_THREAD, repository, approval_policy="never")
            self.assertEqual(error.exception.failure_class, "SECURITY_RECOVERY_OWNER_THREAD_STALE")
            self.assertEqual(process.request_methods, ["thread/resume"])

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, _sent = self._lifecycle_process(repository)
            original_request = process.request

            def wrong_binding(method: str, params: dict | None, *, timeout: float):
                result = original_request(method, params, timeout=timeout)
                result["result"]["runtimeWorkspaceRoots"] = [str(repository / "other")]
                result["result"]["thread"]["environments"][0]["runtimeWorkspaceRoots"] = [str(repository / "other")]
                return result

            process.request = wrong_binding
            with self.assertRaises(AppServerError) as error:
                process._resume_exact_security_thread_unlocked(OWNER_THREAD, repository, approval_policy="never")
            self.assertEqual(error.exception.failure_class, "SECURITY_RECOVERY_OWNER_THREAD_BINDING_MISMATCH")
            self.assertEqual(process.request_methods, ["thread/resume"])

    def test_maintenance_approval_clears_on_success_and_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, sent = self._lifecycle_process(repository)
            outcome = process.run_security_cancel_turn(repository, OWNER_THREAD, SCAN_ID, "cancel exact scan")
            self.assertEqual(
                process.request_methods,
                [
                    "config/read",
                    "mcpServerStatus/list",
                    "thread/resume",
                    "mcpServerStatus/list",
                    "thread/resume",
                ],
            )
            self.assertNotIn("thread/start", process.request_methods)
            self.assertEqual(outcome["turn_id"], "turn-1")
            self.assertTrue(outcome["tool_catalog_validated"])
            self.assertTrue(outcome["approval_granted"])
            self.assertTrue(outcome["approval_policy_reset"])
            self.assertTrue(outcome["approval_state_cleared"])
            self.assertIsNone(process._active_security_cancel_approval)
            self.assertEqual(sent[-1], {"id": 5, "result": {"action": "accept", "content": None}})
            self.assertNotIn("_meta", sent[-1]["result"])

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, _sent = self._lifecycle_process(repository, fail_turn=True)
            outcome = process.run_security_cancel_turn(repository, OWNER_THREAD, SCAN_ID, "cancel exact scan")
            self.assertIn("turn/cancel", process.request_methods)
            self.assertTrue(outcome["turn_cancel_requested"])
            self.assertTrue(outcome["approval_policy_reset"])
            self.assertTrue(outcome["approval_state_cleared"])
            self.assertIsNone(process._active_security_cancel_approval)

    def test_provider_scan_not_found_is_reported_after_valid_one_time_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            process, sent = self._lifecycle_process(repository, scan_not_found=True)
            outcome = process.run_security_cancel_turn(repository, OWNER_THREAD, SCAN_ID, "cancel exact scan")
            self.assertEqual(outcome["turn_state"], "completed")
            self.assertTrue(outcome["approval_granted"])
            self.assertEqual(outcome["approval_scope"], "one_time")
            self.assertEqual(outcome["provider_tool_result"]["provider_error_class"], "scan_not_found")
            self.assertEqual(outcome["provider_tool_result"]["item_status"], "failed")
            self.assertEqual(outcome["failure_class"], "")
            self.assertEqual(sent[-1], {"id": 5, "result": {"action": "accept", "content": None}})


class SecurityCancelToolCatalogTests(unittest.TestCase):
    def test_only_exact_security_cancellation_tool_is_available(self) -> None:
        result = _validate_security_cancel_tool_catalog(
            {
                "result": {
                    "data": [
                        {
                            "name": "codex-security",
                            "pluginId": "codex-security@openai-curated-remote",
                            "tools": {"cancel_codex_security_scan": {}},
                        },
                        {"name": "codex_apps", "pluginId": None, "tools": {}},
                    ],
                    "nextCursor": None,
                }
            }
        )
        self.assertTrue(result["exact_allowlist_verified"])
        self.assertEqual(result["available_tool_count"], 1)

    def test_native_tool_policy_requires_all_other_surfaces_disabled(self) -> None:
        response = {
            "result": {
                "config": {
                    "features": {
                        key: False
                        for key in (
                            "apps",
                            "browser_use",
                            "browser_use_external",
                            "browser_use_full_cdp_access",
                            "computer_use",
                            "hooks",
                            "image_generation",
                            "multi_agent",
                            "sleep_tool",
                            "shell_tool",
                        )
                    },
                    "web_search": "disabled",
                }
            }
        }
        result = _validate_security_recovery_native_tool_policy(response)
        self.assertTrue(result["verified"])
        self.assertEqual(result["web_search"], "disabled")

        for change in (
            {"features": {"computer_use": True}},
            {"features": {}},
            {"web_search": "cached"},
        ):
            unsafe = copy.deepcopy(response)
            config = unsafe["result"]["config"]
            config.update(change)
            with self.subTest(change=change), self.assertRaises(AppServerError) as error:
                _validate_security_recovery_native_tool_policy(unsafe)
            self.assertEqual(error.exception.failure_class, "SECURITY_RECOVERY_NATIVE_TOOL_POLICY_UNSAFE")

    def test_another_or_second_tool_or_incomplete_catalog_fails_closed(self) -> None:
        unsafe_catalogs = [
            {
                "result": {
                    "data": [
                        {
                            "name": "codex-security",
                            "pluginId": "codex-security@openai-curated-remote",
                            "tools": {
                                "cancel_codex_security_scan": {},
                                "start_codex_security_deep_scan": {},
                            },
                        }
                    ],
                    "nextCursor": None,
                }
            },
            {
                "result": {
                    "data": [
                        {
                            "name": "other-server",
                            "pluginId": None,
                            "tools": {"write": {}},
                        },
                        {
                            "name": "codex-security",
                            "pluginId": "codex-security@openai-curated-remote",
                            "tools": {"cancel_codex_security_scan": {}},
                        },
                    ],
                    "nextCursor": None,
                }
            },
            {
                "result": {
                    "data": [
                        {
                            "name": "codex-security",
                            "pluginId": "codex-security@openai-curated-remote",
                            "tools": {"cancel_codex_security_scan": {}},
                        }
                    ],
                    "nextCursor": "more",
                }
            },
        ]
        for catalog in unsafe_catalogs:
            with self.subTest(catalog=catalog), self.assertRaises(AppServerError):
                _validate_security_cancel_tool_catalog(catalog)

    def test_maintenance_uses_dedicated_process_and_closes_policy(self) -> None:
        instances = []

        class FakeProcess:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.closed = False
                self.process = SimpleNamespace(poll=lambda: 0 if self.closed else None)
                instances.append(self)

            def run_security_cancel_turn(self, *_args):
                return {"thread_resumed": True, "approval_state_cleared": True}

            def close(self):
                self.closed = True

        agent = SimpleNamespace(backend="app_server", sandbox="workspace-write")
        with patch("dual_codex.app_server._AppServerProcess", FakeProcess), patch(
            "dual_codex.app_server._get_process"
        ) as normal_process:
            result = run_codex_security_cancel_turn(
                config=SimpleNamespace(),
                agent=agent,
                repository=Path.cwd(),
                owner_thread_id=OWNER_THREAD,
                scan_id=SCAN_ID,
            )
        normal_process.assert_not_called()
        self.assertTrue(result["temporary_tool_policy_cleared"])
        self.assertTrue(instances[0].kwargs["security_recovery_mode"])

        class FailingProcess(FakeProcess):
            def run_security_cancel_turn(self, *_args):
                raise AppServerError("safe test failure", failure_class="APP_SERVER_TEST_FAILURE")

        with patch("dual_codex.app_server._AppServerProcess", FailingProcess):
            failed = run_codex_security_cancel_turn(
                config=SimpleNamespace(),
                agent=agent,
                repository=Path.cwd(),
                owner_thread_id=OWNER_THREAD,
                scan_id=SCAN_ID,
            )
        self.assertEqual(failed["failure_class"], "APP_SERVER_TEST_FAILURE")
        self.assertTrue(failed["temporary_tool_policy_cleared"])


if __name__ == "__main__":
    unittest.main()
