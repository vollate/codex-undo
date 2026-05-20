---
name: codex-undo
description: This skill provides Git-backed safety snapshots and restoration for Codex file edits. It should be used before Codex modifies files in a Git worktree, when a caller asks to create an undo point, or when the caller invokes /undo with a snapshot name to restore the repository to a prior state.
---

# Codex Undo

## Overview

Create a Git-backed snapshot before file edits and restore that snapshot on request. Use the bundled `scripts/codex_undo.py` script from this skill directory. Store snapshot commits under `refs/codex-undo/` without touching the user's working tree or index during snapshot creation.

New snapshots preserve both working tree content and the staged/unstaged index split. Older snapshots without metadata restore the saved working tree but may not preserve the original staging state.

## Before File Edits

Before modifying, creating, deleting, formatting, or generating any file in a Git worktree:

1. Resolve the absolute path to this skill's bundled script.
2. Run the snapshot command from the target repository or pass `--repo`:

```bash
python3 <skill-root>/scripts/codex_undo.py snapshot --repo <worktree>
```

1. Read `snapshot_name` from the output.
1. Tell the caller the exact snapshot name before or alongside the edit summary.
1. Keep that name in task context so `/undo <snapshot-name>` can restore this pre-edit state.

Default snapshots include tracked files and untracked non-ignored files. If the planned edit intentionally targets ignored files, add `--include-ignored` and mention that the snapshot may be larger or may capture files normally excluded by Git.

If planned edits target files inside a Git submodule, run this skill inside that submodule worktree. Parent repository snapshots refuse dirty submodules because parent Git trees cannot capture uncommitted submodule contents.

Do not create a snapshot when only reading files, running diagnostics, answering questions, or using a tool that does not mutate files.

## Undo Request

When the caller says `/undo <snapshot-name>` or otherwise asks to undo to a named snapshot:

```bash
python3 <skill-root>/scripts/codex_undo.py undo <snapshot-name> --repo <worktree>
```

The undo command restores the repository working tree to the saved snapshot and restores the saved index tree when snapshot metadata is available. It also creates a new safety snapshot of the current state before restoring, unless `--no-safety-snapshot` is passed. Report both the restored snapshot and the safety snapshot name.

Treat undo as a destructive restore requested by the caller. If there are unrelated changes after the snapshot, they will be rolled back; the safety snapshot is the recovery point for those changes. Avoid `--no-safety-snapshot` except in controlled tests or when the caller explicitly accepts unrecoverable loss of current changes.

## Ignored Files

Default snapshots do not store ignored file contents. Ignored files that already existed may remain changed after undo unless the snapshot was created with `--include-ignored`.

Use these rules:

- Add `--include-ignored` when the planned edit intentionally touches ignored paths.
- Use `undo --clean-ignored` only when restoring a default snapshot and explicitly cleaning ignored paths created after the snapshot is desired.
- Explain that `--clean-ignored` removes new ignored paths but cannot restore previous contents of ignored files that were not included in the snapshot.

## Commands

Use `scripts/codex_undo.py`:

- `snapshot`: create a unique stored snapshot and print `snapshot_name`.
- `snapshot --include-ignored`: snapshot ignored file contents as well as tracked and untracked non-ignored files.
- `undo <snapshot-name>`: restore the worktree and, when metadata is available, the index to a stored snapshot.
- `undo <snapshot-name> --clean-ignored`: also remove ignored paths that were not present at snapshot time.
- `list`: list stored snapshots.
- `show <snapshot-name>`: show metadata for a stored snapshot.

If a command fails because the directory is not a Git worktree, tell the caller this skill requires Git and do not edit files until a snapshot can be created or the caller explicitly waives undo protection.
