from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dual_codex.paths import (
    safe_atomic_write_bytes,
    safe_create_temp_file,
    safe_ensure_directory_tree,
    safe_open_regular_file,
    safe_read_bytes,
    safe_unlink_if_identity,
)


class SafePathHelperTests(unittest.TestCase):
    def test_safe_open_regular_file_uses_verified_parent_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = root / "actual"
            outside = root / "outside"
            actual.mkdir()
            outside.mkdir()
            redirected = root / "redirected"
            try:
                redirected.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlink creation unavailable: {type(exc).__name__}")

            with self.assertRaises(OSError):
                safe_open_regular_file(
                    redirected / "lock.json",
                    flags=os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            self.assertFalse((outside / "lock.json").exists())

            descriptor = safe_open_regular_file(
                actual / "lock.json",
                flags=os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            try:
                os.write(descriptor, b"owner")
            finally:
                os.close(descriptor)
            with self.assertRaises(FileExistsError):
                safe_open_regular_file(
                    actual / "lock.json",
                    flags=os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )

    def test_safe_ensure_directory_tree_does_not_create_through_symlink_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            redirected = root / "redirected"
            try:
                redirected.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlink creation unavailable: {type(exc).__name__}")

            with self.assertRaises(OSError):
                safe_ensure_directory_tree(redirected / "created")
            self.assertFalse((outside / "created").exists())

    def test_temp_file_creation_stays_on_opened_parent_when_ancestor_is_swapped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "repo"
            external = root / "outside"
            parent.mkdir()
            external.mkdir()
            redirect_state: list[tuple[bool, Path]] = []

            def swap(_operation: str, _target: Path) -> None:
                redirect_state.append(self._redirect_parent_during_operation(root, external, parent))

            try:
                with patch("dual_codex.paths._path_operation_checkpoint", side_effect=swap):
                    descriptor, created_path = safe_create_temp_file(
                        parent,
                        prefix="bootstrap-",
                        suffix=".md",
                    )
                    try:
                        os.write(descriptor, b"trusted host content")
                    finally:
                        os.close(descriptor)
                self.assertTrue(redirect_state)
                actual_parent = redirect_state[0][1] if redirect_state[0][0] else parent
                self.assertEqual((actual_parent / created_path.name).read_bytes(), b"trusted host content")
                self.assertFalse((external / created_path.name).exists())
            finally:
                if redirect_state:
                    self._remove_redirect(parent, redirect_state[0][1])

    def _redirect_parent_during_operation(self, root: Path, external: Path, parent: Path):
        parked = root / "repo-parked"
        try:
            os.replace(parent, parked)
        except OSError:
            return False, parked
        try:
            parent.symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError):
            os.replace(parked, parent)
            return False, parked
        return True, parked

    def _remove_redirect(self, parent: Path, parked: Path) -> None:
        if parent.is_symlink():
            parent.unlink()
        if parked.exists():
            os.replace(parked, parent)

    def test_read_stays_on_opened_parent_when_ancestor_is_swapped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "repo"
            external = root / "outside"
            parent.mkdir()
            external.mkdir()
            (parent / "input.txt").write_text("expected", encoding="utf-8")
            (external / "input.txt").write_text("redirected", encoding="utf-8")
            redirect_state: list[tuple[bool, Path]] = []

            def swap(_operation: str, _target: Path) -> None:
                redirect_state.append(self._redirect_parent_during_operation(root, external, parent))

            try:
                with patch("dual_codex.paths._path_operation_checkpoint", side_effect=swap):
                    content = safe_read_bytes(parent / "input.txt")
                self.assertEqual(content, b"expected")
                self.assertEqual((external / "input.txt").read_text(encoding="utf-8"), "redirected")
                self.assertTrue(redirect_state)
            finally:
                if redirect_state:
                    self._remove_redirect(parent, redirect_state[0][1])

    def test_atomic_write_stays_on_opened_parent_when_ancestor_is_swapped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "repo"
            external = root / "outside"
            parent.mkdir()
            external.mkdir()
            (external / "state.json").write_text("outside state", encoding="utf-8")
            redirect_state: list[tuple[bool, Path]] = []

            def swap(_operation: str, _target: Path) -> None:
                redirect_state.append(self._redirect_parent_during_operation(root, external, parent))

            try:
                with patch("dual_codex.paths._path_operation_checkpoint", side_effect=swap):
                    safe_atomic_write_bytes(parent / "state.json", b"new state")
                self.assertEqual((external / "state.json").read_bytes(), b"outside state")
                self.assertTrue(redirect_state)
                actual_parent = redirect_state[0][1] if redirect_state[0][0] else parent
                self.assertEqual((actual_parent / "state.json").read_bytes(), b"new state")
            finally:
                if redirect_state:
                    self._remove_redirect(parent, redirect_state[0][1])

    def test_unlink_stays_on_opened_parent_when_ancestor_is_swapped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "repo"
            external = root / "outside"
            parent.mkdir()
            external.mkdir()
            target = parent / "artifact.txt"
            target.write_text("owned artifact", encoding="utf-8")
            (external / target.name).write_text("outside owner", encoding="utf-8")
            expected = target.lstat()
            redirect_state: list[tuple[bool, Path]] = []

            def swap(_operation: str, _target: Path) -> None:
                redirect_state.append(self._redirect_parent_during_operation(root, external, parent))

            try:
                with patch("dual_codex.paths._path_operation_checkpoint", side_effect=swap):
                    self.assertTrue(safe_unlink_if_identity(target, expected))
                self.assertEqual((external / target.name).read_text(encoding="utf-8"), "outside owner")
                actual_parent = redirect_state[0][1] if redirect_state[0][0] else parent
                self.assertFalse((actual_parent / target.name).exists())
            finally:
                if redirect_state:
                    self._remove_redirect(parent, redirect_state[0][1])

    @unittest.skipUnless(os.name == "nt", "handle-bound leaf replacement protection is Windows-specific")
    def test_unlink_handle_blocks_leaf_replacement_after_identity_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            target = parent / "artifact.txt"
            replacement = parent / "replacement.txt"
            target.write_text("expected owner", encoding="utf-8")
            replacement.write_text("replacement owner", encoding="utf-8")
            expected = target.lstat()
            replacement_was_blocked: list[bool] = []

            def replace_leaf(_operation: str, _path: Path) -> None:
                try:
                    os.replace(replacement, target)
                except OSError:
                    replacement_was_blocked.append(True)
                else:
                    replacement_was_blocked.append(False)

            with patch("dual_codex.paths._path_operation_checkpoint", side_effect=replace_leaf):
                self.assertTrue(safe_unlink_if_identity(target, expected))
            self.assertEqual(replacement_was_blocked, [True])
            self.assertFalse(target.exists())
            self.assertEqual(replacement.read_text(encoding="utf-8"), "replacement owner")

    def test_atomic_write_refuses_leaf_created_after_absence_check_on_windows(self) -> None:
        if os.name != "nt":
            self.skipTest("conditional Windows handle rename is platform-specific")
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "state.json"

            def create_leaf(_operation: str, _path: Path) -> None:
                target.write_bytes(b"other writer")

            with patch("dual_codex.paths._path_operation_checkpoint", side_effect=create_leaf):
                with self.assertRaises(OSError):
                    safe_atomic_write_bytes(target, b"our update")
            self.assertEqual(target.read_bytes(), b"other writer")

    def test_static_symlink_leaf_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "link.txt"
            external = root / "outside.txt"
            external.write_text("outside", encoding="utf-8")
            try:
                target.symlink_to(external)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")
            with self.assertRaises(OSError):
                safe_read_bytes(target)
            with self.assertRaises(OSError):
                safe_atomic_write_bytes(target, b"blocked")
            self.assertEqual(external.read_text(encoding="utf-8"), "outside")

    def test_write_failure_cleans_up_its_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            target = parent / "state.json"
            target.write_bytes(b"prior state")
            safe_atomic_write_bytes(target, b"published state")
            self.assertEqual(target.read_bytes(), b"published state")
            with patch("dual_codex.paths.os.fsync", side_effect=OSError("injected flush failure")):
                with self.assertRaisesRegex(OSError, "injected flush failure"):
                    safe_atomic_write_bytes(target, b"not published")
            self.assertEqual(target.read_bytes(), b"published state")
            self.assertEqual(list(parent.glob(".state.json.tmp-*")), [])

    @unittest.skipUnless(os.name == "nt", "native reparse attributes are Windows-specific")
    def test_reparse_leaf_attributes_are_rejected_by_read_write_and_unlink_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            target = parent / "control.json"
            target.write_bytes(b"regular on disk")
            expected = target.lstat()
            actual_lstat = Path.lstat
            reparse_info = SimpleNamespace(
                st_mode=stat.S_IFREG | 0o600,
                st_dev=expected.st_dev,
                st_ino=expected.st_ino,
                st_size=expected.st_size,
                st_mtime_ns=expected.st_mtime_ns,
                st_file_attributes=0x400,
            )

            def report_reparse(path: Path):
                if path == target:
                    return reparse_info
                return actual_lstat(path)

            with patch.object(Path, "lstat", new=report_reparse):
                with self.assertRaises(OSError):
                    safe_read_bytes(target)
                with self.assertRaises(OSError):
                    safe_atomic_write_bytes(target, b"blocked")
                self.assertFalse(safe_unlink_if_identity(target, expected))
            self.assertEqual(target.read_bytes(), b"regular on disk")

    @unittest.skipUnless(os.name == "nt", "native reparse attributes are Windows-specific")
    def test_reparse_parent_attributes_are_rejected_by_read_and_write_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "repo"
            parent.mkdir()
            target = parent / "control.json"
            target.write_bytes(b"regular on disk")
            actual_lstat = Path.lstat
            parent_info = SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700,
                st_dev=os.stat(parent).st_dev,
                st_ino=os.stat(parent).st_ino,
                st_size=0,
                st_mtime_ns=0,
                st_file_attributes=0x400,
            )

            def report_reparse(path: Path):
                if path == parent:
                    return parent_info
                return actual_lstat(path)

            with patch.object(Path, "lstat", new=report_reparse):
                with self.assertRaises(OSError):
                    safe_read_bytes(target)
                with self.assertRaises(OSError):
                    safe_atomic_write_bytes(target, b"blocked")
            self.assertEqual(target.read_bytes(), b"regular on disk")


if __name__ == "__main__":
    unittest.main()
