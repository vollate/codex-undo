from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "undo_snapshot.py"
spec = importlib.util.spec_from_file_location("undo_snapshot", MODULE_PATH)
assert spec and spec.loader
undo_snapshot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = undo_snapshot
spec.loader.exec_module(undo_snapshot)


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return proc.stdout


def commit(repo: Path, message: str = "commit") -> None:
    git(
        repo,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        message,
    )


class UndoSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_untracked_symlink_removal_does_not_delete_target(self) -> None:
        target_dir = self.repo / "target"
        target_dir.mkdir()
        (target_dir / "keep.txt").write_text("keep\n")
        git(self.repo, "add", "target/keep.txt")
        commit(self.repo)

        snapshot = undo_snapshot.snapshot(self.repo)
        (self.repo / "link-to-target").symlink_to(target_dir, target_is_directory=True)

        undo_snapshot.undo(
            self.repo,
            snapshot["snapshot_name"],
            no_safety_snapshot=True,
        )

        self.assertTrue((target_dir / "keep.txt").is_file())
        self.assertFalse((self.repo / "link-to-target").exists())

    def test_snapshot_restores_staged_and_unstaged_split(self) -> None:
        file_path = self.repo / "file.txt"
        file_path.write_text("base\n")
        git(self.repo, "add", "file.txt")
        commit(self.repo)

        file_path.write_text("staged\n")
        git(self.repo, "add", "file.txt")
        file_path.write_text("unstaged\n")

        snapshot = undo_snapshot.snapshot(self.repo)
        file_path.write_text("agent edit\n")
        git(self.repo, "add", "file.txt")

        undo_snapshot.undo(
            self.repo,
            snapshot["snapshot_name"],
            no_safety_snapshot=True,
        )

        self.assertEqual(file_path.read_text(), "unstaged\n")
        self.assertEqual(git(self.repo, "status", "--short"), "MM file.txt\n")

    def test_include_ignored_snapshot_restores_ignored_files(self) -> None:
        (self.repo / ".gitignore").write_text("build/\n")
        git(self.repo, "add", ".gitignore")
        commit(self.repo)

        build_dir = self.repo / "build"
        build_dir.mkdir()
        cache_file = build_dir / "cache.txt"
        cache_file.write_text("old\n")

        snapshot = undo_snapshot.snapshot(self.repo, include_ignored=True)
        cache_file.write_text("new\n")
        (build_dir / "generated.txt").write_text("generated\n")

        undo_snapshot.undo(
            self.repo,
            snapshot["snapshot_name"],
            no_safety_snapshot=True,
        )

        self.assertEqual(cache_file.read_text(), "old\n")
        self.assertFalse((build_dir / "generated.txt").exists())
        self.assertEqual(git(self.repo, "status", "--short"), "")
        self.assertEqual(git(self.repo, "status", "--ignored", "--short"), "!! build/\n")

    def test_clean_ignored_removes_new_ignored_paths_only(self) -> None:
        (self.repo / ".gitignore").write_text("*.log\n")
        git(self.repo, "add", ".gitignore")
        commit(self.repo)

        old_log = self.repo / "old.log"
        old_log.write_text("old\n")
        snapshot = undo_snapshot.snapshot(self.repo)
        new_log = self.repo / "new.log"
        new_log.write_text("new\n")

        undo_snapshot.undo(
            self.repo,
            snapshot["snapshot_name"],
            no_safety_snapshot=True,
            clean_ignored=True,
        )

        self.assertTrue(old_log.exists())
        self.assertFalse(new_log.exists())

    def _add_submodule(self, path: str = "vendor/sub") -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name)
        git(source, "init")
        (source / "sub.txt").write_text("base\n")
        git(source, "add", "sub.txt")
        commit(source)

        git(
            self.repo,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(source),
            path,
        )
        commit(self.repo)
        return self.repo / path

    def test_snapshot_and_undo_preserves_dirty_submodule_content(self) -> None:
        sub_path = self._add_submodule()
        sub_file = sub_path / "sub.txt"
        sub_file.write_text("dirty\n")

        snapshot = undo_snapshot.snapshot(self.repo)
        sub_file.write_text("agent edit\n")
        (sub_path / "extra.txt").write_text("new untracked\n")

        undo_snapshot.undo(
            self.repo,
            snapshot["snapshot_name"],
            no_safety_snapshot=True,
        )

        self.assertEqual(sub_file.read_text(), "dirty\n")
        self.assertFalse((sub_path / "extra.txt").exists())

    def test_recursive_undo_reuses_root_safety_snapshot(self) -> None:
        sub_path = self._add_submodule()
        snapshot = undo_snapshot.snapshot(self.repo)
        refs_before = set(
            git(
                sub_path,
                "for-each-ref",
                "--format=%(refname)",
                "refs/undo-snapshot",
            ).splitlines()
        )
        (sub_path / "sub.txt").write_text("agent edit\n")

        undo_snapshot.undo(self.repo, snapshot["snapshot_name"])

        refs_after = set(
            git(
                sub_path,
                "for-each-ref",
                "--format=%(refname)",
                "refs/undo-snapshot",
            ).splitlines()
        )
        self.assertEqual(len(refs_after - refs_before), 1)

    def test_parent_snapshot_records_submodule_mapping(self) -> None:
        sub_path = self._add_submodule()
        (sub_path / "sub.txt").write_text("dirty\n")

        snapshot = undo_snapshot.snapshot(self.repo)
        metadata = undo_snapshot.read_snapshot_metadata(
            self.repo, snapshot["snapshot_commit"]
        )

        self.assertEqual(metadata.get("version"), 3)
        submodules = metadata.get("submodules")
        self.assertIsInstance(submodules, dict)
        self.assertIn("vendor/sub", submodules)
        self.assertTrue(submodules["vendor/sub"]["snapshot_name"])

    def test_nested_submodule_recursive_snapshot(self) -> None:
        inner_path = self._add_submodule("vendor/sub")
        inner_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(inner_tmp.cleanup)
        inner_source = Path(inner_tmp.name)
        git(inner_source, "init")
        (inner_source / "inner.txt").write_text("base\n")
        git(inner_source, "add", "inner.txt")
        commit(inner_source)

        git(
            inner_path,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(inner_source),
            "nested/inner",
        )
        commit(inner_path)

        nested_file = inner_path / "nested" / "inner" / "inner.txt"
        nested_file.write_text("nested dirty\n")

        snapshot = undo_snapshot.snapshot(self.repo)
        nested_file.write_text("agent edit\n")

        undo_snapshot.undo(
            self.repo,
            snapshot["snapshot_name"],
            no_safety_snapshot=True,
        )

        self.assertEqual(nested_file.read_text(), "nested dirty\n")

    def test_uninitialized_submodule_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as source_tmp:
            source = Path(source_tmp)
            git(source, "init")
            (source / "sub.txt").write_text("base\n")
            git(source, "add", "sub.txt")
            commit(source)
            git(
                self.repo,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "--name",
                "ghost",
                str(source),
                "vendor/ghost",
            )
            commit(self.repo)

        git(self.repo, "submodule", "deinit", "-f", "vendor/ghost")
        self.assertFalse((self.repo / "vendor" / "ghost" / "sub.txt").exists())

        snapshot = undo_snapshot.snapshot(self.repo)
        self.assertTrue(snapshot["snapshot_name"])

    def test_non_submodule_directory_declared_in_gitmodules_is_skipped(self) -> None:
        (self.repo / ".gitmodules").write_text(
            '[submodule "ghost"]\n\tpath = ghost\n\turl = ../missing\n'
        )
        ghost = self.repo / "ghost"
        ghost.mkdir()
        (ghost / "untracked.txt").write_text("plain directory\n")
        git(self.repo, "add", ".gitmodules")
        commit(self.repo)

        snapshot = undo_snapshot.snapshot(self.repo)
        metadata = undo_snapshot.read_snapshot_metadata(
            self.repo, snapshot["snapshot_commit"]
        )

        self.assertEqual(metadata.get("submodules"), {})

    def test_snapshot_rejects_submodule_path_outside_repository(self) -> None:
        victim_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(victim_tmp.cleanup)
        victim = Path(victim_tmp.name)
        git(victim, "init")
        (victim / "victim.txt").write_text("keep\n")
        git(victim, "add", "victim.txt")
        commit(victim)
        relative_victim = os.path.relpath(victim, self.repo)
        (self.repo / ".gitmodules").write_text(
            f'[submodule "escape"]\n\tpath = {relative_victim}\n\turl = ignored\n'
        )
        git(self.repo, "add", ".gitmodules")
        commit(self.repo)

        with self.assertRaisesRegex(undo_snapshot.UndoError, "unsafe submodule path"):
            undo_snapshot.snapshot(self.repo)

        victim_refs = git(
            victim,
            "for-each-ref",
            "--format=%(refname)",
            "refs/undo-snapshot",
        )
        self.assertEqual(victim_refs, "")

    def test_undo_skips_submodule_deinitialized_after_snapshot(self) -> None:
        sub_path = self._add_submodule()
        (sub_path / "sub.txt").write_text("dirty\n")
        snapshot = undo_snapshot.snapshot(self.repo)
        git(self.repo, "submodule", "deinit", "-f", "vendor/sub")

        result = undo_snapshot.undo(
            self.repo,
            snapshot["snapshot_name"],
            no_safety_snapshot=True,
        )

        self.assertEqual(result["restored_submodules"], [])

    def test_undo_tolerates_snapshot_without_submodules_field(self) -> None:
        """A snapshot whose metadata has no `submodules` key (v2) must still restore."""
        file_path = self.repo / "file.txt"
        file_path.write_text("base\n")
        git(self.repo, "add", "file.txt")
        commit(self.repo)

        snapshot = undo_snapshot.snapshot(self.repo)
        file_path.write_text("agent edit\n")

        with unittest.mock.patch.object(
            undo_snapshot, "read_snapshot_metadata", return_value={}
        ):
            undo_snapshot.undo(
                self.repo,
                snapshot["snapshot_name"],
                no_safety_snapshot=True,
            )

        self.assertEqual(file_path.read_text(), "base\n")

    def test_legacy_codex_ref_remains_restorable(self) -> None:
        file_path = self.repo / "file.txt"
        file_path.write_text("base\n")
        git(self.repo, "add", "file.txt")
        commit(self.repo)
        snapshot = undo_snapshot.snapshot(self.repo)
        legacy_ref = f"refs/codex-undo/{snapshot['snapshot_name']}"
        git(self.repo, "update-ref", legacy_ref, snapshot["snapshot_commit"])
        git(self.repo, "update-ref", "-d", snapshot["snapshot_ref"])
        listed_refs = {
            item["snapshot_ref"] for item in undo_snapshot.list_snapshots(self.repo)
        }
        self.assertIn(legacy_ref, listed_refs)
        file_path.write_text("agent edit\n")

        result = undo_snapshot.undo(
            self.repo,
            snapshot["snapshot_name"],
            no_safety_snapshot=True,
        )

        self.assertEqual(result["restored_snapshot_ref"], legacy_ref)
        self.assertEqual(file_path.read_text(), "base\n")

    def test_no_recurse_submodules_skips_dirty_content(self) -> None:
        sub_path = self._add_submodule()
        sub_file = sub_path / "sub.txt"
        sub_file.write_text("dirty\n")

        snapshot = undo_snapshot.snapshot(self.repo, recurse_submodules=False)
        metadata = undo_snapshot.read_snapshot_metadata(
            self.repo, snapshot["snapshot_commit"]
        )
        self.assertEqual(metadata.get("submodules"), {})


if __name__ == "__main__":
    unittest.main()
