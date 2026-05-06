#!/usr/bin/env python3
"""Create and restore Git-backed Codex undo snapshots."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


REF_PREFIX = "refs/codex-undo"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ZERO_OID = "0" * 40


class UndoError(RuntimeError):
    pass


def run_git(
    repo: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=full_env,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "git command failed"
        raise UndoError(f"git {' '.join(args)}: {detail}")
    return proc


def repo_root(path: str | None) -> Path:
    start = Path(path or ".").resolve()
    proc = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise UndoError(f"{start} is not a Git worktree")
    return Path(proc.stdout.strip()).resolve()


def current_head(repo: Path) -> str | None:
    proc = run_git(repo, ["rev-parse", "--verify", "HEAD"], check=False)
    if proc.returncode == 0:
        return proc.stdout.strip()
    return None


def sanitize_label(label: str | None) -> str:
    if not label:
        return ""
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", label.strip())
    value = value.strip(".-_")
    return value[:40]


def make_snapshot_name(label: str | None = None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(4)
    clean_label = sanitize_label(label)
    if clean_label:
        return f"codex-{stamp}-{clean_label}-{suffix}"
    return f"codex-{stamp}-{suffix}"


def make_unique_ref(repo: Path, label: str | None = None) -> tuple[str, str]:
    for _attempt in range(10):
        name = make_snapshot_name(label)
        ref = ref_for_name(name)
        proc = run_git(repo, ["rev-parse", "--verify", ref], check=False)
        if proc.returncode != 0:
            return name, ref
    raise UndoError("could not allocate a unique snapshot name")


def ref_for_name(name: str) -> str:
    if name.startswith(f"{REF_PREFIX}/"):
        name = name[len(REF_PREFIX) + 1 :]
    if not NAME_RE.fullmatch(name):
        raise UndoError(f"invalid snapshot name: {name!r}")
    return f"{REF_PREFIX}/{name}"


def snapshot(repo: Path, *, label: str | None = None, include_ignored: bool = False) -> dict[str, str]:
    repo = repo.resolve()
    head = current_head(repo)
    fd, index_path = tempfile.mkstemp(prefix="codex-undo-index-")
    os.close(fd)
    os.unlink(index_path)
    env = {
        "GIT_INDEX_FILE": index_path,
        "GIT_AUTHOR_NAME": "Codex Undo",
        "GIT_AUTHOR_EMAIL": "codex-undo@example.invalid",
        "GIT_COMMITTER_NAME": "Codex Undo",
        "GIT_COMMITTER_EMAIL": "codex-undo@example.invalid",
    }
    try:
        if head:
            run_git(repo, ["read-tree", head], env=env)
        else:
            run_git(repo, ["read-tree", "--empty"], env=env)

        add_args = ["add", "-A"]
        if include_ignored:
            add_args.append("-f")
        add_args.extend(["--", "."])
        run_git(repo, add_args, env=env)

        tree = run_git(repo, ["write-tree"], env=env).stdout.strip()
        name, ref = make_unique_ref(repo, label)
        message = "\n".join(
            [
                f"codex undo snapshot {name}",
                "",
                f"repository: {repo}",
                f"created_utc: {datetime.now(timezone.utc).isoformat()}",
                f"base_head: {head or '(unborn)'}",
                f"include_ignored: {str(include_ignored).lower()}",
            ]
        )
        commit_args = ["commit-tree", tree]
        if head:
            commit_args.extend(["-p", head])
        commit = run_git(repo, commit_args, env=env, input_text=message).stdout.strip()
        run_git(repo, ["update-ref", ref, commit, ZERO_OID])
        return {
            "snapshot_name": name,
            "snapshot_ref": ref,
            "snapshot_commit": commit,
            "repository": str(repo),
        }
    finally:
        try:
            os.unlink(index_path)
        except FileNotFoundError:
            pass


def resolve_snapshot(repo: Path, name: str) -> tuple[str, str]:
    ref = ref_for_name(name)
    proc = run_git(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"], check=False)
    if proc.returncode != 0:
        raise UndoError(f"snapshot not found: {name}")
    return ref, proc.stdout.strip()


def zsplit(raw: str) -> list[str]:
    return [item for item in raw.split("\0") if item]


def remove_untracked(repo: Path) -> None:
    proc = run_git(repo, ["ls-files", "-z", "--others", "--exclude-standard"])
    paths = sorted(zsplit(proc.stdout), key=lambda value: value.count("/"), reverse=True)
    touched_dirs: set[Path] = set()
    for rel in paths:
        target = (repo / rel).resolve()
        try:
            target.relative_to(repo)
        except ValueError:
            raise UndoError(f"refusing to remove path outside repository: {target}")
        if target.is_symlink() or target.is_file():
            target.unlink()
            touched_dirs.add(target.parent)
        elif target.is_dir():
            shutil.rmtree(target)
            touched_dirs.add(target.parent)

    for directory in sorted(touched_dirs, key=lambda path: len(path.parts), reverse=True):
        current = directory
        while current != repo and current.exists():
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent


def undo(repo: Path, name: str, *, no_safety_snapshot: bool = False) -> dict[str, str | None]:
    ref, commit = resolve_snapshot(repo, name)
    safety = None
    if not no_safety_snapshot:
        safety = snapshot(repo, label="before-undo")

    remove_untracked(repo)
    run_git(repo, ["read-tree", "--reset", "-u", commit])
    return {
        "restored_snapshot_name": ref.rsplit("/", 1)[-1],
        "restored_snapshot_ref": ref,
        "restored_snapshot_commit": commit,
        "safety_snapshot_name": safety["snapshot_name"] if safety else None,
        "safety_snapshot_ref": safety["snapshot_ref"] if safety else None,
        "repository": str(repo),
    }


def list_snapshots(repo: Path) -> list[dict[str, str]]:
    proc = run_git(
        repo,
        [
            "for-each-ref",
            "--sort=-creatordate",
            "--format=%(refname) %(objectname) %(creatordate:iso8601) %(subject)",
            REF_PREFIX,
        ],
    )
    rows = []
    for line in proc.stdout.splitlines():
        ref, commit, rest = line.split(" ", 2)
        rows.append(
            {
                "snapshot_name": ref.rsplit("/", 1)[-1],
                "snapshot_ref": ref,
                "snapshot_commit": commit,
                "summary": rest,
            }
        )
    return rows


def show_snapshot(repo: Path, name: str) -> str:
    _ref, commit = resolve_snapshot(repo, name)
    return run_git(repo, ["show", "--stat", "--summary", "--no-renames", commit]).stdout


def print_result(result: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if isinstance(result, list):
        for item in result:
            print(
                f"{item['snapshot_name']} {item['snapshot_commit'][:12]} {item['summary']}"
            )
        return
    if isinstance(result, dict):
        for key, value in result.items():
            if value is not None:
                print(f"{key}: {value}")
        return
    print(result)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", help="Git worktree path. Defaults to the current directory.")
    common.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    subparsers = parser.add_subparsers(dest="command", required=True)

    snap = subparsers.add_parser(
        "snapshot", help="Create a Codex undo snapshot.", parents=[common]
    )
    snap.add_argument("--label", help="Optional short label to include in the snapshot name.")
    snap.add_argument(
        "--include-ignored",
        action="store_true",
        help="Include ignored files. Use only when planned edits target ignored paths.",
    )

    restore = subparsers.add_parser(
        "undo", help="Restore a saved Codex undo snapshot.", parents=[common]
    )
    restore.add_argument("snapshot_name", help="Snapshot name returned by the snapshot command.")
    restore.add_argument(
        "--no-safety-snapshot",
        action="store_true",
        help="Do not snapshot the current state before restoring.",
    )

    subparsers.add_parser("list", help="List Codex undo snapshots.", parents=[common])
    show = subparsers.add_parser(
        "show", help="Show one Codex undo snapshot.", parents=[common]
    )
    show.add_argument("snapshot_name", help="Snapshot name to inspect.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        repo = repo_root(args.repo)
        if args.command == "snapshot":
            result = snapshot(repo, label=args.label, include_ignored=args.include_ignored)
        elif args.command == "undo":
            result = undo(repo, args.snapshot_name, no_safety_snapshot=args.no_safety_snapshot)
        elif args.command == "list":
            result = list_snapshots(repo)
        elif args.command == "show":
            result = show_snapshot(repo, args.snapshot_name)
        else:
            parser.error(f"unknown command: {args.command}")
        print_result(result, args.json)
        return 0
    except UndoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
