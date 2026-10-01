from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from dual_codex.repo_trust import RepositoryTrustError, _validate_trust_target, provision_repository_trust


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path.resolve()


def _profile(path: Path) -> Path:
    path.mkdir(parents=True)
    return path


def _config(path: Path) -> dict:
    return tomllib.loads((path / "config.toml").read_text(encoding="utf-8-sig"))


class RepositoryTrustTests(unittest.TestCase):
    def test_drive_root_and_user_profile_are_never_trusted(self) -> None:
        drive_root = Path(Path.home().anchor)
        with self.assertRaisesRegex(RepositoryTrustError, "drive or filesystem root"):
            _validate_trust_target(drive_root)

        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp).resolve()
            with patch.object(Path, "home", return_value=profile):
                with self.assertRaisesRegex(RepositoryTrustError, "user profile directory"):
                    _validate_trust_target(profile)

    def test_exact_git_root_is_trusted_in_the_configured_actor_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _git_repo(root / "target repo")
            profile = _profile(root / "architect profile")

            self.assertTrue(provision_repository_trust(profile, repository))

            self.assertEqual(
                _config(profile)["projects"][str(repository)]["trust_level"],
                "trusted",
            )

    def test_trust_update_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _git_repo(root / "repo")
            profile = _profile(root / "profile")

            self.assertTrue(provision_repository_trust(profile, repository))
            before = (profile / "config.toml").read_bytes()
            self.assertFalse(provision_repository_trust(profile, repository))
            self.assertEqual((profile / "config.toml").read_bytes(), before)

    def test_unrelated_profile_and_project_config_survive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _git_repo(root / "target")
            other = _git_repo(root / "other")
            profile = _profile(root / "profile")
            config_path = profile / "config.toml"
            config_path.write_text(
                f'model = "gpt-6-sol"\n\n[features]\nexperimental = true\n\n[projects.\'{other}\']\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )

            provision_repository_trust(profile, repository)

            parsed = _config(profile)
            self.assertEqual(parsed["model"], "gpt-6-sol")
            self.assertEqual(parsed["features"], {"experimental": True})
            self.assertEqual(parsed["projects"][str(other)], {"trust_level": "trusted"})
            self.assertEqual(parsed["projects"][str(repository)], {"trust_level": "trusted"})

    def test_only_exact_root_is_added_never_its_parent_or_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parent = root / "parent"
            repository = _git_repo(parent / "target")
            sibling = _git_repo(parent / "sibling")
            profile = _profile(root / "profile")

            provision_repository_trust(profile, repository)

            projects = _config(profile)["projects"]
            self.assertEqual(list(projects), [str(repository)])
            self.assertNotIn(str(parent), projects)
            self.assertNotIn(str(sibling), projects)

    def test_explicit_untrusted_decision_is_not_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _git_repo(root / "repo")
            profile = _profile(root / "profile")
            config_path = profile / "config.toml"
            config_path.write_text(
                f"[projects.'{repository}']\ntrust_level = \"untrusted\"\n",
                encoding="utf-8",
            )
            before = config_path.read_bytes()

            with self.assertRaisesRegex(RepositoryTrustError, "explicitly marks.*untrusted"):
                provision_repository_trust(profile, repository)

            self.assertEqual(config_path.read_bytes(), before)

    def test_non_git_target_fails_closed_without_creating_profile_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "not a repo"
            target.mkdir()
            profile = _profile(root / "profile")

            with self.assertRaisesRegex(RepositoryTrustError, "not a valid Git repository|not the Git top-level"):
                provision_repository_trust(profile, target)

            self.assertFalse((profile / "config.toml").exists())

    def test_repository_path_must_be_the_exact_git_top_level(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _git_repo(root / "repo")
            child = repository / "child"
            child.mkdir()
            profile = _profile(root / "profile")

            with self.assertRaisesRegex(RepositoryTrustError, "not the Git top-level"):
                provision_repository_trust(profile, child)

            self.assertFalse((profile / "config.toml").exists())

    def test_user_default_codex_home_is_not_inferred_or_touched(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _git_repo(root / "repo")
            user_home = root / "user"
            default_codex_home = user_home / ".codex"
            default_codex_home.mkdir(parents=True)
            default_config = default_codex_home / "config.toml"
            default_config.write_text('model = "user-profile"\n', encoding="utf-8")
            actor_profile = _profile(root / "actor-profile")

            with patch.object(Path, "home", return_value=user_home):
                provision_repository_trust(actor_profile, repository)

            self.assertEqual(default_config.read_text(encoding="utf-8"), 'model = "user-profile"\n')
            self.assertTrue((actor_profile / "config.toml").exists())

    def test_bom_and_crlf_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _git_repo(root / "repo")
            profile = _profile(root / "profile")
            config_path = profile / "config.toml"
            config_path.write_bytes(b"\xef\xbb\xbfmodel = \"gpt-6-sol\"\r\n")

            provision_repository_trust(profile, repository)

            updated = config_path.read_bytes()
            self.assertTrue(updated.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"\r\n[projects.", updated)
            self.assertNotIn(b"\n[projects.", updated.replace(b"\r\n", b""))
            self.assertEqual(_config(profile)["model"], "gpt-6-sol")

    def test_malformed_or_ambiguous_project_trust_configuration_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _git_repo(root / "repo")
            profile = _profile(root / "profile")
            config_path = profile / "config.toml"
            config_path.write_text(
                f"[projects.'{repository}']\ntrust_level = \"trusted\"\n"
                f"[projects.'{str(repository).upper()}']\ntrust_level = \"trusted\"\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RepositoryTrustError, "ambiguous entries"):
                provision_repository_trust(profile, repository)

            config_path.write_text("[projects\n", encoding="utf-8")
            with self.assertRaisesRegex(RepositoryTrustError, "config.toml is malformed"):
                provision_repository_trust(profile, repository)

    def test_existing_target_table_without_trust_level_is_updated_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _git_repo(root / "repo")
            profile = _profile(root / "profile")
            config_path = profile / "config.toml"
            config_path.write_text(
                f"[projects.'{repository}']\ncustom_setting = true\n\n[history]\npersistence = \"save-all\"\n",
                encoding="utf-8",
            )

            provision_repository_trust(profile, repository)

            parsed = _config(profile)
            self.assertEqual(parsed["projects"][str(repository)], {"custom_setting": True, "trust_level": "trusted"})
            self.assertEqual(parsed["history"]["persistence"], "save-all")


if __name__ == "__main__":
    unittest.main()
