# undo-snapshot

A Pi coding agent skill and Pi package that creates Git-backed safety snapshots before file edits and restores them on request. The skill follows the Agent Skills format, so it can also be used by other compatible coding-agent harnesses.

## Install in Pi

Install a local checkout globally:

```bash
pi install /absolute/path/to/undo-snapshot
```

Or install directly from a Git repository:

```bash
pi install https://github.com/<owner>/<repo>
```

Add `-l` to either command for a project-local installation. Pi discovers `SKILL.md` through the package manifest.

For a manual global installation, copy the directory into Pi's skill directory:

```bash
mkdir -p "$HOME/.pi/agent/skills"
cp -R /path/to/undo-snapshot "$HOME/.pi/agent/skills/undo-snapshot"
```

Run `/reload` in Pi after a manual installation, or restart Pi.

## Use in Pi

Create a snapshot explicitly:

```text
/skill:undo-snapshot snapshot
```

You can also ask Pi to use the skill before a file-changing task:

```text
Use undo-snapshot before editing files, then implement the requested change.
```

Pi reports a recovery handle such as:

```text
undo-20260507T083000Z-1a2b3c4d
```

Restore that state with:

```text
/skill:undo-snapshot undo undo-20260507T083000Z-1a2b3c4d
```

Pi exposes skills as `/skill:<name>` commands; this package does not register a global `/undo` command.

Other supported skill actions are:

```text
/skill:undo-snapshot list
/skill:undo-snapshot show <snapshot-name>
```

## Command-line use

From a checkout of this repository, create a snapshot:

```bash
python3 scripts/undo_snapshot.py snapshot --repo /path/to/worktree
```

Restore a snapshot:

```bash
python3 scripts/undo_snapshot.py undo <snapshot-name> --repo /path/to/worktree
```

List or inspect snapshots:

```bash
python3 scripts/undo_snapshot.py list --repo /path/to/worktree
python3 scripts/undo_snapshot.py show <snapshot-name> --repo /path/to/worktree
```

## What gets restored

`snapshot` stores a commit under `refs/undo-snapshot/` without changing the worktree or index. Snapshots preserve both working-tree content and the staged/unstaged split. Existing snapshots under the legacy `refs/codex-undo/` namespace remain listable and restorable.

By default, snapshots include tracked files and untracked non-ignored files. Use `--include-ignored` when planned edits intentionally touch ignored files:

```bash
python3 scripts/undo_snapshot.py snapshot --include-ignored --repo /path/to/worktree
```

For snapshots that did not include ignored contents, `undo --clean-ignored` removes ignored paths that were not present at snapshot time. It cannot restore content changes to ignored files that already existed.

Submodules are supported recursively. A parent snapshot descends into each initialized submodule and records the child snapshot mapping in its metadata. Undo restores the parent first and then each initialized submodule. Uninitialized submodules are skipped, and recursion is limited to eight levels.

Use `--no-recurse-submodules` only when submodule content is irrelevant:

```bash
python3 scripts/undo_snapshot.py snapshot --no-recurse-submodules --repo /path/to/worktree
```

## Other coding agents

`SKILL.md` uses the Agent Skills format. To use this repository with another compatible harness, install or copy the repository into that harness's skill location. The bundled script has no Pi or model-provider runtime dependency; it requires only Python 3.10 or newer and Git.

## Development

Run the regression tests:

```bash
python3 -m unittest discover -s tests
```
