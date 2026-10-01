from __future__ import annotations

import http.client
import json
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from unittest.mock import patch
from urllib.error import HTTPError

from dual_codex.config import ConfigError, load_config
from dual_codex.claude_code import _save_session
from dual_codex.dashboard import (
    CAPABILITY_SCRIPT,
    HTML,
    LIVE_EXECUTOR_SCRIPT,
    SCRIPT,
    STYLES,
    DashboardError,
    DashboardServer,
    DashboardService,
    _live_event_name,
)
from dual_codex.live_events import LiveEventJournal
from dual_codex.paths import same_path
from dual_codex.providers import ProviderCapabilities, provider_default_label
from dual_codex.registry import assign_role


def _mutation_headers(server: DashboardServer, *, origin: str | None = None, content_type: str = "application/json") -> dict[str, str]:
    with urlopen(server.url, timeout=3) as response:
        cookies = [value.split(";", 1)[0] for value in response.headers.get_all("Set-Cookie", [])]
    cookie_values = dict(value.split("=", 1) for value in cookies)
    token = cookie_values["dual_codex_csrf"]
    return {
        "Content-Type": content_type,
        "Origin": origin or server.url.rstrip("/"),
        "Cookie": "; ".join(cookies),
        "X-Dual-Codex-CSRF": token,
    }


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "repo").mkdir()
        self.config_path = root / "config.toml"
        self.config_path.write_text(
            """[orchestrator]
repository = "repo"
runs_dir = "runs"
codex_command = "missing-codex-for-dashboard-test"

[accounts.primary]
label = "Primary"
codex_home = "profiles/primary"
model = ""
reasoning_effort = "high"
backend = "windows"

[accounts.secondary]
label = "Secondary"
codex_home = "profiles/secondary"
model = ""
reasoning_effort = "high"
backend = "app_server"

[roles]
orchestrator = "primary"
architect = "primary"
reviewer = "secondary"
executor = "secondary"
""",
            encoding="utf-8",
        )
        self.config = load_config(self.config_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_old_config_gets_empty_service_tier_and_settings_persist(self) -> None:
        service = DashboardService(self.config)
        saved = service.save_settings(
            "primary",
            {"model": "gpt-test", "reasoning_effort": "medium", "service_tier": "fast", "scope": "future_turns"},
        )
        self.assertFalse(saved["current_thread_changed"])
        reloaded = load_config(self.config_path)
        self.assertEqual(reloaded.accounts["primary"].model, "gpt-test")
        self.assertEqual(reloaded.accounts["primary"].reasoning_effort, "medium")
        self.assertEqual(reloaded.accounts["primary"].service_tier, "fast")
        self.assertNotIn(b"\xef\xbb\xbf", self.config_path.read_bytes()[:3])

    def test_model_save_invalidates_cache_and_does_not_depend_on_fallback(self) -> None:
        service = DashboardService(self.config)
        with patch("dual_codex.dashboard.login_status", return_value="NOT LOGGED IN"):
            service.collect_account("secondary", force=True)
            self.assertFalse(self.config.fallback_enabled)
            saved = service.save_settings(
                "secondary",
                {"model": "gpt-6-luna", "reasoning_effort": "high", "scope": "future_turns"},
            )
            self.assertEqual(saved["saved"]["model"], "gpt-6-luna")
            self.assertEqual(service.collect_account("secondary")["configured"]["model"], "gpt-6-luna")
        self.assertEqual(load_config(self.config_path).accounts["secondary"].model, "gpt-6-luna")

    def test_inflight_old_account_read_cannot_repopulate_invalidated_cache(self) -> None:
        account = replace(self.config.accounts["secondary"], backend="app_server", model="")
        config = replace(self.config, accounts={**self.config.accounts, "secondary": account})
        service = DashboardService(config)
        first_model_list = threading.Event()
        release_first = threading.Event()
        call_lock = threading.Lock()
        model_lists = 0
        stale_reads: list[dict[str, object]] = []

        def app_server(*, method: str, **_kwargs: object) -> dict[str, object]:
            nonlocal model_lists
            if method == "model/list":
                with call_lock:
                    model_lists += 1
                    call_number = model_lists
                if call_number == 1:
                    first_model_list.set()
                    self.assertTrue(release_first.wait(timeout=5))
                    model_id = "gpt-5.6-luna"
                else:
                    model_id = "gpt-6-luna"
                return {"data": [{"id": model_id, "displayName": model_id, "isDefault": True, "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]}
            return {"data": []}

        with patch("dual_codex.dashboard.app_server_call", side_effect=app_server), patch(
            "dual_codex.dashboard.app_server_events", return_value=[]
        ), patch("dual_codex.dashboard.login_status", return_value="OK"):
            reader = threading.Thread(target=lambda: stale_reads.append(service.collect_account("secondary", force=True)))
            reader.start()
            self.assertTrue(first_model_list.wait(timeout=5))
            saved = service.save_settings(
                "secondary",
                {"model": "gpt-6-luna", "reasoning_effort": "high", "scope": "future_turns"},
            )
            self.assertEqual(saved["saved"]["model"], "gpt-6-luna")
            release_first.set()
            reader.join(timeout=5)
            self.assertFalse(reader.is_alive())
            self.assertEqual(stale_reads[0]["configured"]["model"], "")
            self.assertEqual(service.collect_account("secondary")["configured"]["model"], "gpt-6-luna")
        self.assertEqual(service._cache["secondary"][2]["configured"]["model"], "gpt-6-luna")

    def test_primary_and_fallback_roles_persist_in_one_mutation(self) -> None:
        service = DashboardService(self.config)
        result = service.set_roles(
            {"account": "primary", "roles": ["architect"], "fallback_roles": ["reviewer"]}
        )
        self.assertEqual(result["fallback_roles"], ["reviewer"])
        service.set_roles({"account": "primary", "roles": ["orchestrator", "architect"]})
        service.update_profile("primary", {"fallback_roles": ["reviewer"]})
        persisted = load_config(self.config_path)
        self.assertEqual(persisted.accounts["primary"].fallback_roles, ("reviewer",))
        self.assertEqual(persisted.roles["orchestrator"], "primary")
        self.assertEqual(persisted.roles["architect"], "primary")
        service.save_settings("primary", {"model": "after-roles", "scope": "future_turns"})
        persisted = load_config(self.config_path)
        self.assertEqual(persisted.accounts["primary"].fallback_roles, ("reviewer",))
        self.assertEqual(persisted.roles["architect"], "primary")

    def test_late_frontend_response_and_dirty_draft_are_ignored(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the dashboard state regression test")
        source = CAPABILITY_SCRIPT + """
const state = globalThis.dualCodexDashboardState;
const mixedDraft = {dirtySettings: true, dirtyRoles: true, dirty: true};
state.setDraftSectionDirty(mixedDraft, 'settings', false);
const rolesRemainDirty = state.shouldPreserveDashboardDraft(mixedDraft);
state.setDraftSectionDirty(mixedDraft, 'roles', false);
console.log(JSON.stringify({
  late: state.responseIsCurrent(1, 2, 1, 2),
  current: state.responseIsCurrent(2, 2, 2, 1),
  staleRevision: state.responseIsCurrent(3, 3, 1, 2),
  mixedRevisionSnapshot: state.snapshotIsCurrent(3, 3, [3, 2], 3),
  currentSnapshot: state.snapshotIsCurrent(3, 3, [3, 3], 3),
  unrelatedMutation: state.latestMutationCanApply(1, 1),
  lateMutation: state.latestMutationCanApply(1, 2),
  dirty: state.shouldPreserveDashboardDraft({dirty: true}),
  clean: state.shouldPreserveDashboardDraft({dirty: false}),
  replaceDirtyCard: state.shouldReplaceDashboardCard({dirty: true}),
  replaceCleanCard: state.shouldReplaceDashboardCard({dirty: false}),
  rolesRemainDirty,
  cleanAfterBothSaves: state.shouldReplaceDashboardCard(mixedDraft),
}));
"""
        result = subprocess.run([node, "-"], input=source, text=True, capture_output=True, check=True)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["late"])
        self.assertTrue(payload["current"])
        self.assertFalse(payload["staleRevision"])
        self.assertFalse(payload["mixedRevisionSnapshot"])
        self.assertTrue(payload["currentSnapshot"])
        self.assertTrue(payload["unrelatedMutation"])
        self.assertFalse(payload["lateMutation"])
        self.assertTrue(payload["dirty"])
        self.assertFalse(payload["clean"])
        self.assertFalse(payload["replaceDirtyCard"])
        self.assertTrue(payload["replaceCleanCard"])
        self.assertTrue(payload["rolesRemainDirty"])
        self.assertTrue(payload["cleanAfterBothSaves"])

    def test_installed_app_server_catalog_accepts_new_model_id(self) -> None:
        account = replace(self.config.accounts["secondary"], backend="app_server", model="")
        config = replace(self.config, accounts={**self.config.accounts, "secondary": account})
        service = DashboardService(config)
        service._cache["secondary"] = (
            time.monotonic(),
            service._revision,
            {"models": [{"id": "gpt-5.6-luna"}], "capabilities": {"effort_levels": ["high"]}},
        )

        def call(*, method, **_kwargs):
            if method == "model/list":
                return {
                    "data": [{
                        "id": "gpt-6-luna",
                        "displayName": "GPT-6 Luna",
                        "isDefault": True,
                        "defaultReasoningEffort": "high",
                        "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                    }]
                }
            return {"data": []} if method == "thread/list" else {}

        with patch("dual_codex.dashboard.app_server_call", side_effect=call), patch(
            "dual_codex.dashboard.app_server_events", return_value=[]
        ), patch("dual_codex.dashboard.login_status", return_value="OK"):
            service.save_settings(
                "secondary",
                {"model": "gpt-6-luna", "reasoning_effort": "high", "scope": "future_turns"},
            )
        saved = load_config(self.config_path)
        self.assertEqual(saved.roles["executor"], "secondary")
        self.assertEqual(saved.accounts["secondary"].model, "gpt-6-luna")

    def test_app_server_capabilities_advertise_assignable_roles(self) -> None:
        account = replace(
            self.config.accounts["secondary"],
            backend="app_server",
            provider_type="codex",
            adapter_type="codex_cli",
        )
        config = replace(self.config, accounts={**self.config.accounts, "secondary": account})
        service = DashboardService(config)
        with patch.object(service, "_auth_raw", return_value="OK"), patch.object(
            service, "_call", return_value=({}, None)
        ), patch.object(service, "_thread", return_value=(None, None)), patch.object(
            service, "_token_usage", return_value=None
        ), patch("dual_codex.dashboard.app_server_events", return_value=[]):
            payload = service.collect_account("secondary", force=True)

        self.assertEqual(
            payload["capabilities"]["supported_roles"],
            ["orchestrator", "architect", "reviewer", "executor"],
        )

    def test_disabled_profiles_keep_declared_roles_for_dashboard_options(self) -> None:
        app_server = replace(self.config.accounts["secondary"], enabled=False)
        claude = replace(
            self.config.accounts["primary"],
            backend="claude_code",
            provider_type="anthropic",
            adapter_type="claude_code",
            enabled=False,
        )
        config = replace(
            self.config,
            accounts={**self.config.accounts, "secondary": app_server, "primary": claude},
            roles={},
        )
        service = DashboardService(config)
        with patch.object(service, "_auth_raw", return_value="OK"), patch(
            "dual_codex.dashboard.provider_capabilities"
        ) as runtime_probe:
            app_payload = service.collect_account("secondary", force=True)
            claude_payload = service.collect_account("primary", force=True)

        runtime_probe.assert_not_called()
        self.assertEqual(app_payload["roles"], [])
        self.assertEqual(
            app_payload["capabilities"]["supported_roles"],
            ["orchestrator", "architect", "reviewer", "executor"],
        )
        self.assertEqual(claude_payload["roles"], [])
        self.assertEqual(claude_payload["capabilities"]["supported_roles"], ["reviewer"])

        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the dashboard role options regression test")
        source = CAPABILITY_SCRIPT + f"""
const helper = globalThis.dualCodexDashboardCapabilities;
const roles = ['orchestrator', 'architect', 'reviewer', 'executor'];
console.log(JSON.stringify({{
  appServer: helper.dashboardRoleOptions(roles, [], {json.dumps(app_payload['capabilities']['supported_roles'])}),
  claude: helper.dashboardRoleOptions(roles, [], {json.dumps(claude_payload['capabilities']['supported_roles'])}),
}}));
"""
        result = subprocess.run([node, "-"], input=source, text=True, capture_output=True, check=True)
        options = json.loads(result.stdout)
        self.assertEqual(
            [option["role"] for option in options["appServer"]],
            ["orchestrator", "architect", "reviewer", "executor"],
        )
        self.assertTrue(all(option["supported"] for option in options["appServer"]))
        self.assertEqual(options["claude"], [{"role": "reviewer", "supported": True, "assigned": False}])
        with self.assertRaisesRegex(ConfigError, "does not support primary role\\(s\\): architect"):
            assign_role(config, "architect", "primary")

    def test_profile_management_crud_is_metadata_only_and_immediate(self) -> None:
        service = DashboardService(self.config)
        home = Path(self.temp.name) / "profiles" / "codex-secondary"
        created = service.create_profile(
            {
                "label": "Codex Secundário",
                "backend": "windows",
                "codex_home": str(home),
                "model": "gpt-5.6-luna",
                "reasoning_effort": "high",
                "enabled": True,
            }
        )
        self.assertEqual(created["profile"]["display_name"], "Codex Secundário")
        self.assertIn("codex-secundario", load_config(self.config_path).accounts)
        self.assertTrue((home / "config.toml").exists())
        self.assertEqual(
            service.update_profile("codex-secundario", {"label": "Codex B", "enabled": False})["profile"]["display_name"],
            "Codex B",
        )
        self.assertFalse(load_config(self.config_path).accounts["codex-secundario"].enabled)
        with self.assertRaises(DashboardError):
            service.create_profile(
                {"label": "Collision", "backend": "windows", "codex_home": str(home / ".." / "codex-secondary")}
            )
        removed = service.remove_profile("codex-secundario", {"confirm": True})
        self.assertTrue(removed["metadata_removed"])
        self.assertFalse(removed["provider_state_deleted"])
        self.assertTrue(home.exists())
        self.assertNotIn("codex-secundario", load_config(self.config_path).accounts)

    def test_profile_auth_action_uses_selected_codex_home_without_exposing_output(self) -> None:
        service = DashboardService(self.config)
        home = Path(self.temp.name) / "profiles" / "codex-b"
        service.create_profile({"label": "Codex B", "name": "codex-b", "backend": "windows", "codex_home": str(home)})
        observed: list[Path] = []

        def fake_login(config, name, **_kwargs):
            observed.append(config.accounts[name].codex_home)

        with patch("dual_codex.dashboard.login_account", side_effect=fake_login), patch(
            "dual_codex.dashboard.login_status", return_value="OK"
        ):
            started = service.auth_action("codex-b", {"action": "authenticate"})
            service._auth_jobs["codex-b"].join(timeout=2)
            status = service.auth_status("codex-b")
        self.assertEqual(started["status"], "Authentication in progress")
        self.assertEqual(len(observed), 1)
        self.assertTrue(same_path(observed[0], home), (observed[0], home))
        self.assertEqual(status["status"], "Authenticated")
        self.assertNotIn("token", json.dumps(status).lower())

    def test_current_thread_scope_is_explicitly_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DashboardService(self.config).save_settings("primary", {"scope": "current_thread"})

    def test_role_assignment_api_uses_registry_validation(self) -> None:
        service = DashboardService(self.config)
        with self.assertRaisesRegex(ValueError, "does not support primary role"):
            service.assign({"role": "executor", "account": "primary"})
        self.assertEqual(load_config(self.config_path).roles["executor"], "secondary")
        result = service.assign({"role": "executor", "account": "secondary"})
        self.assertEqual(result["account"], "secondary")

    def test_role_set_updates_complete_map_and_transfers_roles(self) -> None:
        result = DashboardService(self.config).set_roles(
            {"account": "primary", "roles": ["orchestrator", "architect", "reviewer"]}
        )
        self.assertEqual(result["message"], "Roles updated")
        self.assertEqual(
            result["roles"],
            {"orchestrator": "primary", "architect": "primary", "reviewer": "primary", "executor": "secondary"},
        )
        reloaded = load_config(self.config_path)
        self.assertEqual(
            reloaded.roles,
            {"orchestrator": "primary", "architect": "primary", "reviewer": "primary", "executor": "secondary"},
        )

    def test_role_set_rejects_invalid_input_without_persisting(self) -> None:
        service = DashboardService(self.config)
        original = self.config_path.read_bytes()
        for body in (
            {"account": "missing", "roles": []},
            {"account": "primary", "roles": ["unknown"]},
            {"account": "primary", "roles": ["architect", "architect"]},
            {"account": "primary", "roles": "architect"},
        ):
            with self.subTest(body=body):
                with self.assertRaises((DashboardError, ValueError)):
                    service.set_roles(body)
                self.assertEqual(self.config_path.read_bytes(), original)

    def test_role_set_api_returns_resulting_role_map(self) -> None:
        server = DashboardServer(self.config)
        thread = server.serve_in_thread()
        try:
            request = Request(
                server.url + "api/roles/set",
                data=json.dumps({"account": "primary", "roles": ["orchestrator", "architect", "reviewer"]}).encode(),
                headers=_mutation_headers(server),
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["message"], "Roles updated")
            self.assertEqual(payload["roles"]["reviewer"], "primary")
            self.assertEqual(load_config(self.config_path).roles["executor"], "secondary")
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()
            thread.join(timeout=3)

    def test_model_linked_frontend_capabilities_reconcile_live_selection(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the capability helper regression test")
        source = CAPABILITY_SCRIPT + """
const capabilityHelper = globalThis.dualCodexDashboardCapabilities;
const models = [
  {id: 'model-a', is_default: true, default_reasoning: 'medium', reasoning_efforts: ['low', 'medium'], default_service_tier: 'fast', service_tiers: [{id: 'fast', name: 'Fast'}]},
  {id: 'model-b', is_default: false, default_reasoning: 'max', reasoning_efforts: ['high', 'max'], default_service_tier: 'premium', service_tiers: [{id: 'premium', name: 'Premium'}]},
  {id: 'fixed', is_default: false, fixed_mode: 'Thinking', reasoning_efforts: [], service_tiers: []},
];
console.log(JSON.stringify({
  changed: capabilityHelper.reconcileCapabilitySelection(models, 'model-b', 'medium', 'fast'),
  inherit: capabilityHelper.reconcileCapabilitySelection(models, '', 'ultra', 'fast'),
  fixed: capabilityHelper.reconcileCapabilitySelection(models, 'fixed', 'high', '', {effort_levels: ['low', 'medium', 'high']}),
  providerDefault: capabilityHelper.reconcileCapabilitySelection(models, '', 'high', '', {effort_levels: []}),
  missingDefault: capabilityHelper.reconcileCapabilitySelection(models.map(model => ({...model, is_default: false})), '', 'high', 'fast'),
  apiRoles: capabilityHelper.dashboardRoleOptions(['orchestrator', 'architect', 'reviewer', 'executor'], [], ['orchestrator', 'reviewer']),
  staleArchitect: capabilityHelper.dashboardRoleOptions(['orchestrator', 'architect', 'reviewer', 'executor'], ['architect'], ['orchestrator', 'reviewer']),
}));
"""
        result = subprocess.run([node, "-"], input=source, text=True, capture_output=True, check=True)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["changed"]["selected_model"], "model-b")
        self.assertEqual(payload["changed"]["reasoning_value"], "max")
        self.assertEqual(payload["changed"]["service_tier_value"], "")
        self.assertIn("Reasoning updated", payload["changed"]["message"])
        self.assertIn("Service tier reset", payload["changed"]["message"])
        self.assertEqual(payload["inherit"]["selected_model"], "model-a")
        self.assertEqual(payload["inherit"]["reasoning_value"], "medium")
        self.assertFalse(payload["inherit"]["reasoning_disabled"])
        self.assertEqual(payload["fixed"]["reasoning_efforts"], [])
        self.assertEqual(payload["fixed"]["fixed_mode"], "Thinking")
        self.assertTrue(payload["fixed"]["reasoning_disabled"])
        self.assertEqual(payload["fixed"]["reasoning_value"], "")
        self.assertTrue(payload["providerDefault"]["reasoning_disabled"])
        self.assertIsNone(payload["missingDefault"]["selected_model"])
        self.assertTrue(payload["missingDefault"]["reasoning_disabled"])
        self.assertNotIn("architect", [row["role"] for row in payload["apiRoles"]])
        stale = next(row for row in payload["staleArchitect"] if row["role"] == "architect")
        self.assertFalse(stale["supported"])
        self.assertTrue(stale["assigned"])
        self.assertIn("target.dataset.roleSupported === 'false'", SCRIPT)

    def test_backend_rejects_invalid_model_reasoning_and_tier_combinations(self) -> None:
        service = DashboardService(self.config)
        catalog = {
            "models": [
                {"id": "model-a", "is_default": True, "reasoning_efforts": ["low", "medium"], "service_tiers": [{"id": "fast"}]},
                {"id": "model-b", "is_default": False, "reasoning_efforts": ["high"], "service_tiers": [{"id": "premium"}]},
            ]
        }
        with patch.object(service, "collect_account", return_value=catalog):
            with self.assertRaises(DashboardError):
                service.save_settings(
                    "primary",
                    {"model": "model-b", "reasoning_effort": "medium", "service_tier": "premium"},
                )
            with self.assertRaises(DashboardError):
                service.save_settings(
                    "primary",
                    {"model": "model-b", "reasoning_effort": "high", "service_tier": "fast"},
                )

    def test_backend_preview_refreshes_provider_capabilities(self) -> None:
        account = self.config.accounts["primary"]
        self.config.accounts["primary"] = replace(
            account,
            base_url="https://api.example.test/v1",
            auth_reference="env:TEST_PROVIDER_KEY",
            available_models=("api-model",),
            supported_reasoning_efforts=("low",),
        )
        service = DashboardService(self.config)
        preview = service.models("primary", "api")
        self.assertEqual(preview["provider"], "api")
        self.assertEqual([row["id"] for row in preview["models"]], ["api-model"])
        self.assertEqual(preview["capabilities"]["effort_levels"], ["low"])

        server = DashboardServer(self.config)
        thread = server.serve_in_thread()
        try:
            with urlopen(server.url + "api/accounts/primary/models?backend=api", timeout=3) as response:
                payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["provider"], "api")
            self.assertEqual(payload["models"][0]["id"], "api-model")
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()
            thread.join(timeout=3)

    def test_frontend_has_provider_specific_default_labels(self) -> None:
        self.assertEqual(provider_default_label("codex", "app_server"), "Inherit Codex default")
        self.assertEqual(provider_default_label("gemini", "antigravity"), "Inherit Antigravity default")
        self.assertIn("Provider default", SCRIPT)
        self.assertIn("This model does not expose configurable effort", SCRIPT)
        self.assertIn("No active turn", SCRIPT)
        self.assertIn("function applyClaudeUx", SCRIPT)
        self.assertIn("provider === 'anthropic'", SCRIPT)
        self.assertIn("claude_code|Anthropic Claude", SCRIPT)
        self.assertIn("Anthropic Claude</option>", HTML)
        self.assertIn("PROFILES / ACCOUNTS", HTML)
        self.assertIn("Add profile", HTML)
        self.assertIn("data-profile-auth", SCRIPT)
        self.assertIn("Remove metadata", SCRIPT)
        self.assertIn("shouldPreserveDashboardDraft", SCRIPT)
        self.assertIn("updateCardTelemetry(existing, account)", SCRIPT)
        self.assertIn("function reportRefreshError(error)", SCRIPT)
        self.assertNotIn("$('#accounts').innerHTML", SCRIPT)
        self.assertIn("<details class=\"advanced\">", SCRIPT)
        self.assertNotIn("dashboardFetch", SCRIPT)
        self.assertNotIn("window.alert", SCRIPT)

    def test_claude_model_effort_persistence_reconciles_no_effort_models(self) -> None:
        account = replace(
            self.config.accounts["primary"],
            backend="claude_code",
            provider_type="anthropic",
            adapter_type="claude_code",
            model="sonnet",
            reasoning_effort="high",
            service_tier="",
        )
        self.config.accounts["primary"] = account
        catalog = [
            {"id": "sonnet", "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"], "default_reasoning": "high", "service_tiers": []},
            {"id": "opus", "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"], "default_reasoning": "high", "service_tiers": []},
            {"id": "haiku", "reasoning_efforts": [], "default_reasoning": None, "service_tiers": []},
            {"id": "fable", "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"], "default_reasoning": "high", "service_tiers": []},
        ]
        service = DashboardService(self.config)
        current = {"models": catalog, "capabilities": {"effort_levels": ["low", "medium", "high", "xhigh", "max"]}}
        with patch.object(service, "collect_account", return_value=current):
            service.save_settings("primary", {"model": "haiku", "scope": "future_turns"})
        saved = load_config(self.config_path).accounts["primary"]
        self.assertEqual(saved.model, "haiku")
        self.assertEqual(saved.reasoning_effort, "")
        with patch.object(service, "collect_account", return_value={"models": catalog, "capabilities": current["capabilities"]}):
            with self.assertRaises(DashboardError):
                service.save_settings("primary", {"model": "haiku", "reasoning_effort": "high", "scope": "future_turns"})
            service.save_settings("primary", {"model": "sonnet", "reasoning_effort": "high", "scope": "future_turns"})
        self.assertEqual(load_config(self.config_path).accounts["primary"].reasoning_effort, "high")

    def test_antigravity_dashboard_exposes_one_logical_row_per_catalog_family(self) -> None:
        account = replace(
            self.config.accounts["primary"],
            backend="antigravity",
            provider_type="gemini",
            adapter_type="antigravity_cli",
            model="",
            reasoning_effort="high",
        )
        config = replace(self.config, accounts={**self.config.accounts, "primary": account})
        service = DashboardService(config)
        catalog = "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\ngemini-3.8-flash-low\tGemini 3.8 Flash (Low)\nclaude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
        with patch("dual_codex.providers.subprocess.run") as run:
            run.return_value = type("Result", (), {"stdout": catalog, "stderr": "", "returncode": 0})()
            with patch("dual_codex.providers.antigravity_status", return_value="OK"):
                payload = service.models("primary")
        self.assertEqual([row["id"] for row in payload["models"]], ["gemini-3.8-flash", "claude-sonnet-4-6"])
        self.assertEqual(payload["models"][0]["reasoning_efforts"], ["low", "high"])
        self.assertEqual(payload["models"][1]["fixed_mode"], "Thinking")

    def test_antigravity_fixed_model_save_persists_runtime_mapping(self) -> None:
        account = replace(
            self.config.accounts["primary"],
            backend="antigravity",
            provider_type="gemini",
            adapter_type="antigravity_cli",
            model="gemini-3.8-flash",
            reasoning_effort="high",
        )
        config = replace(self.config, accounts={**self.config.accounts, "primary": account})
        service = DashboardService(config)
        catalog = "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\ngemini-3.8-flash-low\tGemini 3.8 Flash (Low)\nclaude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
        with patch("dual_codex.providers.subprocess.run") as run:
            run.return_value = type("Result", (), {"stdout": catalog, "stderr": "", "returncode": 0})()
            with patch("dual_codex.providers.antigravity_status", return_value="OK"):
                service.save_settings("primary", {"model": "claude-sonnet-4-6", "scope": "future_turns"})
        saved = load_config(self.config_path).accounts["primary"]
        self.assertEqual(saved.model, "claude-sonnet-4-6")
        self.assertEqual(saved.runtime_model, "claude-sonnet-4-6")
        self.assertEqual(saved.fixed_mode, "Thinking")
        self.assertEqual(saved.reasoning_effort, "")

    def test_server_smoke_and_security_boundary(self) -> None:
        server = DashboardServer(self.config)
        thread = server.serve_in_thread()
        try:
            with urlopen(server.url, timeout=3) as response:
                html = response.read().decode("utf-8")
                response_cookies = response.headers.get_all("Set-Cookie", [])
            self.assertEqual(response.status, 200)
            self.assertIn("Dual Agents", html)
            self.assertTrue(any(value.startswith("dual_codex_csrf=") for value in response_cookies))
            self.assertTrue(any(value.startswith("dual_codex_session=") for value in response_cookies))
            with urlopen(server.url + "api/status", timeout=3) as response:
                status = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(status["schema_version"], 1)
            with urlopen(server.url + "api/accounts", timeout=3) as response:
                accounts = json.loads(response.read())
            self.assertEqual(accounts["accounts"][0]["name"], "primary")
            rendered_accounts = json.dumps(accounts).lower()
            self.assertNotIn("auth.json", rendered_accounts)
            self.assertNotIn("placeholder-secret", rendered_accounts)

            connection = http.client.HTTPConnection("127.0.0.1", server.httpd.server_address[1], timeout=3)
            connection.request("GET", "/api/accounts/primary/settings")
            self.assertEqual(connection.getresponse().status, 405)
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", server.httpd.server_address[1], timeout=3)
            connection.putrequest("GET", "/api/status", skip_host=True)
            connection.putheader("Host", "evil.example")
            connection.endheaders()
            self.assertEqual(connection.getresponse().status, 403)
            connection.close()
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()
            thread.join(timeout=3)

    def test_profile_management_http_routes_persist_safe_metadata(self) -> None:
        server = DashboardServer(self.config)
        thread = server.serve_in_thread()
        try:
            request = Request(
                server.url + "api/accounts",
                data=json.dumps(
                    {
                        "name": "codex-b",
                        "label": "Codex B",
                        "backend": "windows",
                        "codex_home": str(Path(self.temp.name) / "profiles" / "codex-b"),
                        "enabled": True,
                    }
                ).encode(),
                headers=_mutation_headers(server),
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                created = json.loads(response.read())
            self.assertEqual(response.status, 201)
            self.assertEqual(created["profile"]["id"], "codex-b")

            with urlopen(server.url + "api/accounts/codex-b/auth", timeout=3) as response:
                auth = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertIn("status", auth)
            self.assertNotIn("auth.json", json.dumps(auth).lower())

            request = Request(
                server.url + "api/accounts/codex-b",
                data=b'{"label":"Codex B renamed"}',
                headers=_mutation_headers(server),
                method="PATCH",
            )
            with urlopen(request, timeout=3) as response:
                updated = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(updated["profile"]["display_name"], "Codex B renamed")

            request = Request(
                server.url + "api/accounts/codex-b",
                data=b'{"confirm":true}',
                headers=_mutation_headers(server),
                method="DELETE",
            )
            with urlopen(request, timeout=3) as response:
                removed = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertTrue(removed["metadata_removed"])
            self.assertFalse(removed["provider_state_deleted"])
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()
            thread.join(timeout=3)

    def test_patch_rejects_unknown_account_and_arbitrary_path(self) -> None:
        server = DashboardServer(self.config)
        thread = server.serve_in_thread()
        try:
            request = Request(
                server.url + "api/accounts/does-not-exist/settings",
                data=b'{"model":"x"}',
                headers=_mutation_headers(server),
                method="PATCH",
            )
            with self.assertRaises(Exception):
                urlopen(request, timeout=3)
            request = Request(
                server.url + "api/accounts/primary/settings",
                data=b'{"command":"dir"}',
                headers=_mutation_headers(server),
                method="PATCH",
            )
            with self.assertRaises(Exception):
                urlopen(request, timeout=3)
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()
            thread.join(timeout=3)

    def test_cross_origin_null_post_and_wrong_content_type_are_rejected(self) -> None:
        server = DashboardServer(self.config)
        thread = server.serve_in_thread()
        try:
            request = Request(
                server.url + "api/fallback",
                data=b'{"enabled":true}',
                headers=_mutation_headers(server, origin="null"),
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3)
            self.assertEqual(raised.exception.code, 403)
            raised.exception.close()
            self.assertFalse(load_config(self.config_path).fallback_enabled)

            request = Request(
                server.url + "api/fallback",
                data=b'{"enabled":true}',
                headers=_mutation_headers(server, content_type="text/plain"),
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3)
            self.assertEqual(raised.exception.code, 403)
            raised.exception.close()
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()
            thread.join(timeout=3)

    def test_csrf_token_is_bound_to_one_dashboard_session(self) -> None:
        server = DashboardServer(self.config)
        thread = server.serve_in_thread()
        try:
            first = _mutation_headers(server)
            second = _mutation_headers(server)
            first_cookies = dict(
                cookie.strip().split("=", 1) for cookie in first["Cookie"].split(";")
            )
            second_cookies = dict(
                cookie.strip().split("=", 1) for cookie in second["Cookie"].split(";")
            )
            self.assertNotEqual(first_cookies["dual_codex_session"], second_cookies["dual_codex_session"])
            self.assertNotEqual(first_cookies["dual_codex_csrf"], second_cookies["dual_codex_csrf"])

            first["Cookie"] = (
                f"dual_codex_session={first_cookies['dual_codex_session']}; "
                f"dual_codex_csrf={second_cookies['dual_codex_csrf']}"
            )
            request = Request(
                server.url + "api/fallback",
                data=b'{"enabled":true}',
                headers=first,
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3)
            self.assertEqual(raised.exception.code, 403)
            raised.exception.close()
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()
            thread.join(timeout=3)

    def test_root_replaces_a_malformed_dashboard_session_cookie(self) -> None:
        server = DashboardServer(self.config)
        thread = server.serve_in_thread()
        try:
            request = Request(server.url, headers={"Cookie": "dual_codex_session=stale"})
            with urlopen(request, timeout=3) as response:
                cookies = [value.split(";", 1)[0] for value in response.headers.get_all("Set-Cookie", [])]
            cookie_values = dict(value.split("=", 1) for value in cookies)
            self.assertRegex(cookie_values["dual_codex_session"], r"^[A-Za-z0-9_-]{43}$")
            self.assertRegex(cookie_values["dual_codex_csrf"], r"^[a-f0-9]{64}$")

            mutation = Request(
                server.url + "api/fallback",
                data=b'{"enabled":true}',
                headers={
                    "Content-Type": "application/json",
                    "Origin": server.url.rstrip("/"),
                    "Cookie": "; ".join(cookies),
                    "X-Dual-Codex-CSRF": cookie_values["dual_codex_csrf"],
                },
                method="POST",
            )
            with urlopen(mutation, timeout=3) as response:
                self.assertEqual(response.status, 200)
            self.assertTrue(load_config(self.config_path).fallback_enabled)
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()
            thread.join(timeout=3)

    def test_mocked_app_server_telemetry_keeps_dynamic_buckets_and_capabilities(self) -> None:
        self.config.accounts["primary"] = replace(self.config.accounts["primary"], backend="app_server")

        def call(*, method, **_kwargs):
            return {
                "model/list": {"data": [{"id": "model-a", "displayName": "Model A", "description": "", "isDefault": True, "hidden": False, "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}], "serviceTiers": [{"id": "fast", "name": "Fast", "description": ""}]}]},
                "account/read": {"account": {"type": "chatgpt", "email": "user@example.com", "planType": "plus"}},
                "account/rateLimits/read": {"rateLimitsByLimitId": {"five_hour": {"limitName": "Five hour", "primary": {"usedPercent": 42, "resetsAt": 123}}, "weekly": {"secondary": {"usedPercent": 7}}}},
                "account/usage/read": {"summary": {"lifetimeTokens": 123}, "dailyUsageBuckets": [{"startDate": "2026-08-08", "tokens": 12}]},
                "thread/list": {"data": []},
            }.get(method, {})

        with patch("dual_codex.dashboard.app_server_call", side_effect=call), patch("dual_codex.dashboard.app_server_events", return_value=[]), patch("dual_codex.dashboard.login_status", return_value="OK"):
            account = DashboardService(self.config).collect_account("primary", force=True)
        self.assertEqual(account["effective"]["model"], "model-a")
        self.assertEqual([row["id"] for row in account["rate_limits"]], ["five_hour", "weekly"])
        self.assertEqual(account["usage"]["summary"]["lifetimeTokens"], 123)
        self.assertTrue(account["capabilities"]["service_tier"])
        self.assertEqual(account["runtime_state"], "Idle")
        self.assertNotIn("email", json.dumps(account).lower())

    def test_claude_dispatchable_requires_auth_runtime_and_scoped_session(self) -> None:
        primary = replace(
            self.config.accounts["primary"],
            backend="claude_code",
            provider_type="anthropic",
            adapter_type="claude_code",
            model="sonnet",
        )
        config = replace(
            self.config,
            accounts={**self.config.accounts, "primary": primary},
            roles={**self.config.roles, "reviewer": "primary"},
        )
        capabilities = ProviderCapabilities(
            provider="anthropic",
            provider_label="Anthropic Claude",
            adapter="claude_code",
            runtime_status="Authenticated",
            authenticated=True,
        )
        service = DashboardService(config)
        with patch("dual_codex.dashboard.provider_capabilities", return_value=capabilities), patch(
            "dual_codex.dashboard.login_status", return_value="OK"
        ):
            unbound = service.collect_account("primary", force=True)
            self.assertEqual(
                unbound["availability"],
                {
                    "configured": True,
                    "authenticated": True,
                    "runtime_initialized": True,
                    "thread_bound": False,
                    "dispatchable": False,
                    "active": False,
                },
            )
            _save_session(
                config,
                config.agent_for_role("architect"),
                config.repository,
                "architect",
                "scoped-claude-session",
            )
            bound = service.collect_account("primary", force=True)

        self.assertTrue(bound["availability"]["runtime_initialized"])
        self.assertTrue(bound["availability"]["thread_bound"])
        self.assertTrue(bound["availability"]["dispatchable"])

    def test_live_executor_snapshot_is_path_derived_and_bounded(self) -> None:
        service = DashboardService(self.config)
        idle = service.live_executor_snapshot()
        self.assertEqual(idle["state"], "IDLE")
        self.assertEqual(idle["cursor"], 0)

        journal = LiveEventJournal(
            self.config.runs_dir,
            account="secondary",
            role="executor",
            repository=self.config.repository,
            run_id="run-1",
            request_id="request-1",
            max_records=20,
            max_record_bytes=2048,
        )
        journal.append_notification(
            "turn/started",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "turn": {
                    "id": "turn-1",
                    "model": "model-live",
                    "reasoningEffort": "high",
                    "serviceTier": "fast",
                },
            },
        )
        journal.append_notification(
            "item/commandExecution/outputDelta",
            {"threadId": "thread-1", "turnId": "turn-1", "output": "safe output"},
        )
        journal.append_notification(
            "item/fileChange/updated",
            {"threadId": "thread-1", "turnId": "turn-1", "diff": "diff --git a/a b/a"},
        )
        journal.append_notification(
            "item/agentMessage/delta",
            {"threadId": "thread-1", "turnId": "turn-1", "delta": "safe message"},
        )
        journal.append_notification(
            "thread/tokenUsage/updated",
            {"threadId": "thread-1", "tokenUsage": {"total": 42}},
        )
        journal.append_notification(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        )

        snapshot = service.live_executor_snapshot()
        self.assertEqual(snapshot["state"], "COMPLETE")
        self.assertEqual(snapshot["request_id"], "request-1")
        self.assertEqual(snapshot["run_id"], "run-1")
        self.assertEqual(snapshot["thread_id"], "thread-1")
        self.assertEqual(snapshot["turn_id"], "turn-1")
        self.assertEqual(snapshot["model"], "model-live")
        self.assertEqual(snapshot["reasoning_effort"], "high")
        self.assertEqual(snapshot["service_tier"], "fast")
        self.assertEqual(snapshot["token_usage"]["total"], 42)
        self.assertTrue(snapshot["activity"]["commands"])
        self.assertTrue(snapshot["activity"]["outputs"])
        self.assertTrue(snapshot["activity"]["file_changes"])
        self.assertTrue(snapshot["activity"]["diffs"])
        self.assertTrue(snapshot["activity"]["messages"])
        self.assertNotIn("path", snapshot)

    def test_live_executor_snapshot_keeps_run_times_and_nested_token_totals(self) -> None:
        long_repo = self.config.repository.parent / "long-repo"
        long_repo.mkdir()
        long_config = replace(self.config, repository=long_repo)
        journal = LiveEventJournal(
            long_config.runs_dir,
            account="secondary",
            role="executor",
            repository=long_repo,
            run_id="run-long",
            request_id="request-long",
            max_records=300,
            max_record_bytes=2048,
        )
        started = journal.append_notification(
            "turn/started",
            {"threadId": "thread-long", "turnId": "turn-long", "turn": {"id": "turn-long"}},
        )[0]
        journal.append_notification(
            "thread/tokenUsage/updated",
            {
                "threadId": "thread-long",
                "tokenUsage": {"total": {"totalTokens": 1234}, "last": {"totalTokens": 56}},
            },
        )
        for index in range(130):
            journal.append(kind="notification", state="observed", method=f"future/{index}")
        completed = journal.append_notification(
            "turn/completed",
            {"threadId": "thread-long", "turn": {"id": "turn-long", "status": "completed"}},
        )[0]

        snapshot = DashboardService(long_config).live_executor_snapshot()
        self.assertEqual(snapshot["state"], "COMPLETE")
        self.assertEqual(len(snapshot["events"]), 128)
        self.assertEqual(snapshot["started_at"], started.timestamp)
        self.assertEqual(snapshot["completed_at"], completed.timestamp)
        self.assertEqual(snapshot["token_usage"]["total"]["totalTokens"], 1234)
        self.assertEqual(snapshot["token_usage"]["last"]["totalTokens"], 56)

    def test_live_executor_sse_replays_from_cursor_and_keeps_loopback_boundary(self) -> None:
        journal = LiveEventJournal(
            self.config.runs_dir,
            account="secondary",
            role="executor",
            repository=self.config.repository,
            run_id="run-1",
            request_id="request-1",
            max_records=20,
            max_record_bytes=2048,
        )
        journal.append(kind="notification", state="one", method="future/one")
        journal.append(kind="notification", state="two", method="future/two")
        server = DashboardServer(self.config)
        thread = server.serve_in_thread()
        try:
            request = Request(
                server.url + "api/live-executor/events?path=outside.jsonl",
                headers={"Last-Event-ID": "1"},
            )
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"].split(";", 1)[0], "text/event-stream")
                events: list[dict[str, str]] = []
                current: dict[str, str] = {}
                while len(events) < 2:
                    line = response.readline().decode("utf-8")
                    self.assertTrue(line)
                    if line == "\n":
                        events.append(current)
                        current = {}
                    elif line.startswith("event: "):
                        current["event"] = line[7:].strip()
                    elif line.startswith("id: "):
                        current["id"] = line[4:].strip()
                self.assertEqual(events[0], {"event": "snapshot", "id": "1"})
                self.assertEqual(events[1], {"event": "live", "id": "2"})

            connection = http.client.HTTPConnection("127.0.0.1", server.httpd.server_address[1], timeout=3)
            connection.putrequest("GET", "/api/live-executor/events", skip_host=True)
            connection.putheader("Host", "evil.example")
            connection.endheaders()
            self.assertEqual(connection.getresponse().status, 403)
            connection.close()
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()
            thread.join(timeout=3)

    def test_run_terminal_events_map_to_terminal_sse_names(self) -> None:
        self.assertEqual(_live_event_name({"kind": "run", "state": "completed"}), "complete")
        self.assertEqual(_live_event_name({"kind": "run", "state": "failed"}), "failed")
        self.assertEqual(_live_event_name({"kind": "run", "state": "cancelled"}), "failed")

    def test_live_executor_sse_reconciles_stale_snapshot_without_new_event(self) -> None:
        class Reader:
            def __init__(self) -> None:
                self.calls = 0

            def snapshot(self) -> dict:
                self.calls += 1
                if self.calls == 1:
                    return {"state": "WORKING", "cursor": 1, "run_id": "run-1", "events": []}
                return {
                    "state": "STALE",
                    "cursor": 1,
                    "run_id": "run-1",
                    "ended_at": "2026-08-08T22:00:00Z",
                    "elapsed_seconds": 12,
                    "stale_reason": "active marker has no live writer and no recent activity",
                    "events": [],
                }

            def events_after(self, _cursor: int) -> list[dict]:
                return []

        reader = Reader()
        server = DashboardServer(self.config)
        thread = server.serve_in_thread()
        try:
            with patch.object(DashboardService, "live_executor_reader", return_value=reader), patch(
                "dual_codex.dashboard.LIVE_RECONCILIATION_SECONDS", 0.01
            ):
                response = urlopen(server.url + "api/live-executor/events", timeout=3)
                try:
                    blocks: list[dict[str, str]] = []
                    current: dict[str, str] = {}
                    while len(blocks) < 2:
                        line = response.readline().decode("utf-8")
                        self.assertTrue(line)
                        if line == "\n":
                            blocks.append(current)
                            current = {}
                        elif line.startswith("event: "):
                            current["event"] = line[7:].strip()
                        elif line.startswith("data: "):
                            current["data"] = line[6:].strip()
                    self.assertEqual(blocks[0]["event"], "snapshot")
                    self.assertEqual(json.loads(blocks[1]["data"])["state"], "STALE")
                    self.assertIn("stale_reason", blocks[1]["data"])
                finally:
                    response.close()
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()
            thread.join(timeout=3)

    def test_live_executor_reconnect_starts_with_reconciled_terminal_snapshot(self) -> None:
        journal = LiveEventJournal(
            self.config.runs_dir,
            account="secondary",
            role="executor",
            repository=self.config.repository,
            run_id="run-terminal",
            request_id="request-terminal",
            max_records=20,
            max_record_bytes=2048,
        )
        journal.append(kind="run", state="started", method="run/started")
        journal.append(
            kind="run",
            state="completed",
            method="run/completed",
            detail={"ended_at": "2026-08-08T22:00:00Z"},
        )
        server = DashboardServer(self.config)
        thread = server.serve_in_thread()
        try:
            response = urlopen(server.url + "api/live-executor/events", timeout=3)
            try:
                block: dict[str, str] = {}
                while "data" not in block:
                    line = response.readline().decode("utf-8")
                    self.assertTrue(line)
                    if line.startswith("event: "):
                        block["event"] = line[7:].strip()
                    elif line.startswith("data: "):
                        block["data"] = line[6:].strip()
                self.assertEqual(block["event"], "complete")
                self.assertEqual(json.loads(block["data"])["state"], "COMPLETE")
            finally:
                response.close()
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()
            thread.join(timeout=3)

    def test_live_executor_ui_is_bounded_cli_like_and_text_safe(self) -> None:
        for marker in (
            "EXECUTOR LIVE",
            "Follow Live",
            "Pause",
            "Clear View",
            "CURRENT PLAN",
            "Live Diff",
        ):
            self.assertIn(marker, HTML)
        self.assertIn("ui-monospace", STYLES)
        self.assertIn("EventSource", LIVE_EXECUTOR_SCRIPT)
        self.assertIn("EXECUTOR_MAX_ROWS=128", LIVE_EXECUTOR_SCRIPT)
        self.assertIn("textContent", LIVE_EXECUTOR_SCRIPT)
        self.assertIn("replaceChildren", LIVE_EXECUTOR_SCRIPT)
        self.assertNotIn("executor-feed.innerHTML", LIVE_EXECUTOR_SCRIPT)
        self.assertIn("totalTokens", SCRIPT)
        self.assertIn("started_at", SCRIPT)
        self.assertIn("completed_at", SCRIPT)
        self.assertIn("ended_at", SCRIPT)
        self.assertIn("elapsed_seconds", SCRIPT)
        self.assertIn("stale_reason", SCRIPT)
        self.assertIn("executorRecordLateEvent", SCRIPT)

    def test_live_executor_script_has_node_syntax_and_server_time_contract(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the frontend syntax regression test")
        result = subprocess.run([node, "--check"], input=SCRIPT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("executorLive.clearedAt=executorLive.cursor", SCRIPT)
        self.assertIn("executorLive.endedAt=null", SCRIPT)
        self.assertIn("snapshot.elapsed_seconds", SCRIPT)


if __name__ == "__main__":
    unittest.main()
