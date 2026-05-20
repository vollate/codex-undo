from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "codex_undo.py"
spec = importlib.util.spec_from_file_location("codex_undo", MODULE_PATH)
assert spec and spec.loader
codex_undo = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = codex_undo
spec.loader.exec_module(codex_undo)


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


class CodexUndoTests(unittest.TestCase):
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

        snapshot = codex_undo.snapshot(self.repo)
        (self.repo / "link-to-target").symlink_to(target_dir, target_is_directory=True)

        codex_undo.undo(
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

        snapshot = codex_undo.snapshot(self.repo)
        file_path.write_text("agent edit\n")
        git(self.repo, "add", "file.txt")

        codex_undo.undo(
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

        snapshot = codex_undo.snapshot(self.repo, include_ignored=True)
        cache_file.write_text("new\n")
        (build_dir / "generated.txt").write_text("generated\n")

        codex_undo.undo(
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
        snapshot = codex_undo.snapshot(self.repo)
        new_log = self.repo / "new.log"
        new_log.write_text("new\n")

        codex_undo.undo(
            self.repo,
            snapshot["snapshot_name"],
            no_safety_snapshot=True,
            clean_ignored=True,
        )

        self.assertTrue(old_log.exists())
        self.assertFalse(new_log.exists())

    def test_snapshot_refuses_dirty_submodule(self) -> None:
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
                str(source),
                "vendor/sub",
            )
            commit(self.repo)

        (self.repo / "vendor" / "sub" / "sub.txt").write_text("dirty\n")

        with self.assertRaisesRegex(codex_undo.UndoError, "dirty submodules"):
            codex_undo.snapshot(self.repo)


if __name__ == "__main__":
    unittest.main()
