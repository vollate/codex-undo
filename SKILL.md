---
name: undo
description: Git-backed safety snapshots and undo for Codex file edits. Use before Codex modifies any file in a Git worktree so the caller receives a unique snapshot name, and use when the user invokes /undo with a snapshot name or asks to restore the repository to the state before a prior Codex action.
---

# Undo

## Overview

Create a Git snapshot before file edits and restore that snapshot on request. The snapshot script stores immutable commit objects under `refs/codex-undo/` without touching the user's working tree or index.

## Before File Edits

Before modifying, creating, deleting, formatting, or generating any file in a Git worktree:

1. Run the snapshot command from the target repository or pass `--repo`:

```bash
python3 /path/to/undo/scripts/codex_undo.py snapshot --repo /path/to/worktree
```

2. Read `snapshot_name` from the output.
3. Tell the caller the exact snapshot name before or alongside the edit summary.
4. Keep that name in task context so `/undo <snapshot-name>` can restore this pre-edit state.

Default snapshots include tracked files and untracked non-ignored files. If the planned edit intentionally targets ignored files, add `--include-ignored` and mention that the snapshot may be larger.

Do not create a snapshot when only reading files, running diagnostics, answering questions, or using a tool that does not mutate files.

## Undo Request

When the user says `/undo <snapshot-name>` or otherwise asks to undo to a named snapshot:

```bash
python3 /path/to/undo/scripts/codex_undo.py undo <snapshot-name> --repo /path/to/worktree
```

The undo command resets the repository index and working tree to the saved snapshot tree. It also creates a new safety snapshot of the current state before restoring, unless `--no-safety-snapshot` is passed. Report both the restored snapshot and the safety snapshot name.

Treat undo as a destructive restore requested by the user. If there are unrelated user changes after the snapshot, they will be rolled back; the safety snapshot is the recovery point for those changes.

## Commands

Use `scripts/codex_undo.py`:

- `snapshot`: create a unique stored snapshot and print `snapshot_name`.
- `undo <snapshot-name>`: restore the index and worktree to a stored snapshot.
- `list`: list stored snapshots.
- `show <snapshot-name>`: show metadata for a stored snapshot.

If a command fails because the directory is not a Git worktree, tell the user this skill requires Git and do not edit files until a snapshot can be created or the user explicitly waives undo protection.
