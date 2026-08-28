---
name: undo-snapshot
description: Creates and restores Git-backed safety snapshots for coding-agent file edits. Use before modifying files in a Git worktree, when the user requests a checkpoint or undo point, or when /skill:undo-snapshot is invoked with snapshot, undo, list, or show arguments.
compatibility: Requires Git and Python 3.10 or newer. Designed for Pi and compatible with Agent Skills harnesses.
---

# Undo Snapshot

## Overview

Create a Git-backed snapshot before file edits and restore that snapshot on request. Resolve this skill directory from the loaded `SKILL.md`, then use its bundled `scripts/undo_snapshot.py` script with an absolute path. Snapshot commits are stored under `refs/undo-snapshot/` without changing the user's worktree or index during snapshot creation.

Snapshots preserve working-tree content and the staged/unstaged index split. Legacy snapshots under `refs/codex-undo/` remain listable and restorable.

## Pi Skill Arguments

Pi appends arguments from `/skill:undo-snapshot` to this skill. Interpret them as follows:

- `snapshot`: create and report a snapshot.
- `undo <snapshot-name>`: restore the named snapshot.
- `list`: list snapshots in the current repository.
- `show <snapshot-name>`: show one snapshot.
- No arguments: if this skill was loaded for a file-changing task, create one snapshot before the first mutation.

Pi does not create a global `/undo` command from a skill. Use `/skill:undo-snapshot undo <snapshot-name>` or ask in prose to restore a named snapshot.

## Before File Edits

Before modifying, creating, deleting, formatting, or generating files in a Git worktree:

1. Resolve the absolute path to `scripts/undo_snapshot.py` from this skill directory.
2. Run one snapshot command before the first mutation:

```bash
python3 <skill-root>/scripts/undo_snapshot.py snapshot --repo <worktree>
```

3. Read `snapshot_name` from the output.
4. Tell the user the exact snapshot name before or alongside the edit summary.
5. Keep the name in task context so it can be restored later.

Do not create a snapshot for read-only work, diagnostics, explanations, or commands that cannot mutate files. Do not create a separate snapshot before every tool call in one task; one pre-mutation snapshot is the recovery point.

Default snapshots include tracked files and untracked non-ignored files. If planned edits intentionally target ignored files, add `--include-ignored` and warn that the snapshot may be larger.

Submodules are supported recursively. Each initialized submodule receives its own nested snapshot, recorded in the parent metadata. Uninitialized submodules are skipped. Nested recursion is limited to eight levels. If edits target only one submodule, snapshotting that submodule worktree directly creates a smaller checkpoint.

## Undo Request

When the user invokes `undo <snapshot-name>` through the skill or otherwise asks to restore a named snapshot, run:

```bash
python3 <skill-root>/scripts/undo_snapshot.py undo <snapshot-name> --repo <worktree>
```

Undo restores the saved worktree and index state. It first creates a safety snapshot of the current state unless `--no-safety-snapshot` is passed. Report both the restored snapshot and the safety snapshot name.

Treat undo as a destructive restore explicitly requested by the user. Unrelated changes made after the target snapshot are rolled back, with the safety snapshot serving as their recovery point. Avoid `--no-safety-snapshot` except in controlled tests or when the user explicitly accepts unrecoverable loss.

## Ignored Files

Default snapshots do not store ignored file contents. Ignored files that already existed may remain changed after undo unless the snapshot used `--include-ignored`.

- Add `--include-ignored` when planned edits intentionally touch ignored paths.
- Use `undo --clean-ignored` only when removal of ignored paths created after the snapshot is desired.
- Explain that `--clean-ignored` cannot restore previous contents of ignored files omitted from the snapshot.

## Commands

Use `scripts/undo_snapshot.py`:

- `snapshot`: create a unique stored snapshot and print `snapshot_name`.
- `snapshot --include-ignored`: also capture ignored file contents.
- `snapshot --no-recurse-submodules`: skip nested submodule snapshots.
- `undo <snapshot-name>`: restore the saved worktree and index.
- `undo <snapshot-name> --clean-ignored`: also remove ignored paths created after snapshot time.
- `list`: list current and legacy snapshots.
- `show <snapshot-name>`: show one snapshot.

If the target is not a Git worktree, tell the user that the skill requires Git. Do not edit files until a snapshot succeeds or the user explicitly waives undo protection.
