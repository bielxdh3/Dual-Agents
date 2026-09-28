from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from dual_codex.security_scan import PLUGIN_ID, _installed_plugin, _version_key


class SecurityPluginVersionTests(unittest.TestCase):
    def test_semver_precedence_handles_prerelease_build_and_numeric_components(self) -> None:
        self.assertGreater(_version_key("1.10.0"), _version_key("1.9.99"))
        self.assertGreater(_version_key("1.0.0"), _version_key("1.0.0-rc.9"))
        self.assertGreater(_version_key("1.0.0-rc.10"), _version_key("1.0.0-rc.9"))
        self.assertLess(_version_key("1.0.0-rc.1"), _version_key("1.0.0-rc.beta"))
        self.assertEqual(_version_key("1.0.0+build.1"), _version_key("1.0.0+build.2"))

    def test_invalid_or_unbounded_semver_is_rejected(self) -> None:
        for version in ("1.0", "01.0.0", "1.0.0-01", "1.0.0-", "1.0.0+", "1.0.0" + "x" * 64):
            with self.subTest(version=version):
                self.assertIsNone(_version_key(version))

    def test_installed_plugin_selection_uses_semver_and_ignores_invalid_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary)
            plugin_cache = codex_home / "plugins" / "cache" / "openai-curated-remote" / "codex-security"
            plugin_cache.mkdir(parents=True)
            (plugin_cache / ".codex-remote-plugin-install.json").write_text("{}", encoding="utf-8")
            for version in ("1.0.0-rc.10", "1.0.0+build.1", "1.0.0", "not-semver"):
                plugin_root = plugin_cache / version
                (plugin_root / ".codex-plugin").mkdir(parents=True)
                (plugin_root / ".codex-plugin" / "plugin.json").write_text(
                    json.dumps({"name": "codex-security", "version": version}), encoding="utf-8"
                )
                (plugin_root / ".mcp.json").write_text(
                    json.dumps({"mcpServers": {"codex-security": {}}}), encoding="utf-8"
                )
                (plugin_root / "mcp").mkdir()
                (plugin_root / "mcp" / "server.mjs").write_text("", encoding="utf-8")

            plugin_root, plugin_id, version = _installed_plugin(codex_home)

            self.assertEqual(plugin_id, PLUGIN_ID)
            self.assertEqual(version, "1.0.0")
            self.assertEqual(plugin_root.name, "1.0.0")


if __name__ == "__main__":
    unittest.main()
