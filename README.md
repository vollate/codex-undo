# codex-undo

A Codex skill that creates Git-backed safety snapshots before Codex edits files and restores those snapshots on request.

## Install

Install into Codex's skills directory:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R /path/to/codex-undo "${CODEX_HOME:-$HOME/.codex}/skills/codex-undo"
```

Then restart Codex so it loads the new skill.

If this skill is hosted on GitHub, it can also be installed from inside Codex with `$skill-installer` using the repository URL, for example:

```text
$skill-installer install https://github.com/<owner>/<repo>/tree/main
```

## Use in Codex

Ask Codex to use the skill before file-changing work, for example:

```text
Use codex-undo before editing files, then implement the requested change.
```

Codex should create a snapshot and report a name like:

```text
codex-20260507T083000Z-1a2b3c4d
```

To restore that state later, ask Codex:

```text
/undo codex-20260507T083000Z-1a2b3c4d
```

Keep the snapshot name. It is the recovery handle for the pre-edit state.

## Use from the command line

Create a snapshot:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/codex-undo/scripts/codex_undo.py" snapshot --repo /path/to/worktree
```

Restore a snapshot:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/codex-undo/scripts/codex_undo.py" undo <snapshot-name> --repo /path/to/worktree
```

List snapshots:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/codex-undo/scripts/codex_undo.py" list --repo /path/to/worktree
```

Show snapshot details:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/codex-undo/scripts/codex_undo.py" show <snapshot-name> --repo /path/to/worktree
```

## What gets restored

`snapshot` stores a commit under `refs/codex-undo/` without changing the worktree or index. New snapshots preserve both working tree content and the original staged/unstaged split.

By default, snapshots include tracked files and untracked non-ignored files.

Use `--include-ignored` when the planned edit intentionally touches ignored files:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/codex-undo/scripts/codex_undo.py" snapshot --include-ignored --repo /path/to/worktree
```

For snapshots that did not include ignored contents, `undo --clean-ignored` can remove ignored paths that were not present at snapshot time, but it cannot restore content changes to ignored files that already existed.

Parent repository snapshots refuse dirty submodules because parent Git trees cannot capture uncommitted submodule contents. Run the snapshot command inside the submodule worktree before editing submodule files.

## Development

Run the regression tests:

```bash
python3 -m unittest discover -s tests
```
