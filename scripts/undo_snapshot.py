#!/usr/bin/env python3
"""Create and restore Git-backed undo snapshots for coding agents."""

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


REF_PREFIX = "refs/undo-snapshot"
LEGACY_REF_PREFIXES = ("refs/codex-undo",)
REF_PREFIXES = (REF_PREFIX, *LEGACY_REF_PREFIXES)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ZERO_OID = "0" * 40
METADATA_MARKER = "undo-snapshot-metadata:"
METADATA_MARKERS = (METADATA_MARKER, "codex-undo-metadata:")
MAX_SUBMODULE_DEPTH = 8


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


def list_submodule_paths(repo: Path) -> list[str]:
    proc = run_git(
        repo,
        ["config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
        check=False,
    )
    if proc.returncode != 0:
        return []
    paths = []
    for line in proc.stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            paths.append(parts[1])
    return paths


def validate_submodule_path(repo: Path, rel: str) -> Path:
    repo = repo.resolve()
    path = Path(rel)
    if not rel or path == Path(".") or path.is_absolute() or ".." in path.parts:
        raise UndoError(f"refusing unsafe submodule path: {rel!r}")

    target = repo / path
    try:
        resolved_target = target.resolve()
        resolved_target.relative_to(repo)
    except (OSError, RuntimeError, ValueError) as exc:
        raise UndoError(f"refusing submodule path outside repository: {rel!r}") from exc
    return target


def is_gitlink(repo: Path, rel: str) -> bool:
    normalized = rel.rstrip("/")
    commands = [
        ["ls-files", "--stage", "-z", "--", normalized],
        ["ls-tree", "-z", "HEAD", "--", normalized],
    ]
    for args in commands:
        proc = run_git(repo, args, check=False)
        if proc.returncode != 0:
            continue
        for entry in proc.stdout.split("\0"):
            header, separator, entry_path = entry.partition("\t")
            if not separator or entry_path.rstrip("/") != normalized:
                continue
            if header.split(" ", 1)[0] == "160000":
                return True
    return False


def is_git_worktree(path: Path) -> bool:
    proc = run_git(path, ["rev-parse", "--show-toplevel"], check=False)
    if proc.returncode != 0:
        return False
    try:
        return Path(proc.stdout.strip()).resolve() == path.resolve()
    except (OSError, RuntimeError):
        return False


def initialized_submodule_paths(repo: Path) -> list[str]:
    initialized = []
    for rel in list_submodule_paths(repo):
        normalized = rel.rstrip("/")
        path = validate_submodule_path(repo, normalized)
        if not is_gitlink(repo, normalized):
            continue
        if not path.is_dir() or not is_git_worktree(path):
            continue
        initialized.append(normalized)
    return initialized


def is_submodule_path(rel: str, submodule_paths: set[str]) -> bool:
    normalized = rel.rstrip("/")
    if normalized in submodule_paths:
        return True
    return any(normalized.startswith(f"{sub}/") for sub in submodule_paths)


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
        return f"undo-{stamp}-{clean_label}-{suffix}"
    return f"undo-{stamp}-{suffix}"


def make_unique_ref(repo: Path, label: str | None = None) -> tuple[str, str]:
    for _ in range(10):
        name = make_snapshot_name(label)
        ref = ref_for_name(name)
        proc = run_git(repo, ["rev-parse", "--verify", ref], check=False)
        if proc.returncode != 0:
            return name, ref
    raise UndoError("could not allocate a unique snapshot name")


def normalize_snapshot_name(name: str) -> tuple[str, str | None]:
    matched_prefix = None
    for prefix in REF_PREFIXES:
        if name.startswith(f"{prefix}/"):
            matched_prefix = prefix
            name = name[len(prefix) + 1 :]
            break
    if not NAME_RE.fullmatch(name):
        raise UndoError(f"invalid snapshot name: {name!r}")
    return name, matched_prefix


def ref_for_name(name: str) -> str:
    normalized, _ = normalize_snapshot_name(name)
    return f"{REF_PREFIX}/{normalized}"


def snapshot(
    repo: Path,
    *,
    label: str | None = None,
    include_ignored: bool = False,
    depth: int = 0,
    recurse_submodules: bool = True,
) -> dict[str, str]:
    repo = repo.resolve()
    if depth > MAX_SUBMODULE_DEPTH:
        raise UndoError(
            f"submodule recursion depth {depth} exceeds limit {MAX_SUBMODULE_DEPTH}"
        )

    submodule_snapshots: dict[str, dict[str, str]] = {}
    if recurse_submodules:
        for sub_rel in initialized_submodule_paths(repo):
            try:
                child = snapshot(
                    repo / sub_rel,
                    include_ignored=include_ignored,
                    depth=depth + 1,
                    recurse_submodules=True,
                )
            except UndoError as exc:
                raise UndoError(f"failed to snapshot submodule {sub_rel!r}: {exc}") from exc
            submodule_snapshots[sub_rel] = {
                "snapshot_name": child["snapshot_name"],
                "snapshot_ref": child["snapshot_ref"],
                "snapshot_commit": child["snapshot_commit"],
            }

    head = current_head(repo)
    try:
        index_tree = run_git(repo, ["write-tree"]).stdout.strip()
    except UndoError as exc:
        raise UndoError(
            "cannot snapshot the index; resolve unmerged index entries first"
        ) from exc
    ignored_paths = list_untracked(repo, ignored=True)

    fd, index_path = tempfile.mkstemp(prefix="undo-snapshot-index-")
    os.close(fd)
    os.unlink(index_path)
    env = {
        "GIT_INDEX_FILE": index_path,
        "GIT_AUTHOR_NAME": "Undo Snapshot",
        "GIT_AUTHOR_EMAIL": "undo-snapshot@example.invalid",
        "GIT_COMMITTER_NAME": "Undo Snapshot",
        "GIT_COMMITTER_EMAIL": "undo-snapshot@example.invalid",
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
        metadata = {
            "version": 3,
            "index_tree": index_tree,
            "worktree_tree": tree,
            "include_ignored": include_ignored,
            "ignored_paths": ignored_paths,
            "submodules": submodule_snapshots,
        }
        message = "\n".join(
            [
                f"undo snapshot {name}",
                "",
                f"repository: {repo}",
                f"created_utc: {datetime.now(timezone.utc).isoformat()}",
                f"base_head: {head or '(unborn)'}",
                f"include_ignored: {str(include_ignored).lower()}",
                "",
                METADATA_MARKER,
                json.dumps(metadata, sort_keys=True),
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
    normalized, matched_prefix = normalize_snapshot_name(name)
    prefixes = (matched_prefix,) if matched_prefix else REF_PREFIXES
    for prefix in prefixes:
        ref = f"{prefix}/{normalized}"
        proc = run_git(
            repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"], check=False
        )
        if proc.returncode == 0:
            return ref, proc.stdout.strip()
    raise UndoError(f"snapshot not found: {name}")


def zsplit(raw: str) -> list[str]:
    return [item for item in raw.split("\0") if item]


def list_untracked(repo: Path, *, ignored: bool = False) -> list[str]:
    args = ["ls-files", "-z", "--others"]
    if ignored:
        args.extend(["--ignored", "--exclude-standard"])
    else:
        args.append("--exclude-standard")
    return zsplit(run_git(repo, args).stdout)


def validate_git_path(repo: Path, rel: str) -> Path:
    repo = repo.resolve()
    path = Path(rel)
    if path.is_absolute() or ".." in path.parts:
        raise UndoError(f"refusing unsafe repository path: {rel!r}")
    target = repo / path
    try:
        target.parent.resolve().relative_to(repo)
    except ValueError:
        raise UndoError(f"refusing to remove path outside repository: {target}")
    return target


def is_preserved_ignored_path(rel: str, preserved: set[str]) -> bool:
    if rel in preserved:
        return True
    return any(
        saved.endswith("/") and rel.startswith(saved)
        for saved in preserved
    )


def remove_paths(repo: Path, paths: list[str]) -> None:
    ordered = sorted(set(paths), key=lambda value: value.count("/"), reverse=True)
    touched_dirs: set[Path] = set()
    for rel in ordered:
        target = validate_git_path(repo, rel)
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


def remove_untracked(
    repo: Path,
    *,
    preserve_ignored_paths: set[str] | None = None,
    preserve_paths: set[str] | None = None,
) -> None:
    paths = list_untracked(repo)
    if preserve_ignored_paths is not None:
        paths.extend(
            rel
            for rel in list_untracked(repo, ignored=True)
            if not is_preserved_ignored_path(rel, preserve_ignored_paths)
        )
    if preserve_paths:
        paths = [
            rel
            for rel in paths
            if not is_submodule_path(rel, preserve_paths)
        ]
    remove_paths(repo, paths)


def read_snapshot_metadata(repo: Path, commit: str) -> dict[str, object]:
    message = run_git(repo, ["log", "-1", "--format=%B", commit]).stdout
    lines = message.splitlines()
    for index, line in enumerate(lines):
        if line in METADATA_MARKERS:
            raw = "\n".join(lines[index + 1 :]).strip()
            if not raw:
                return {}
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise UndoError(f"invalid snapshot metadata for {commit}: {exc}") from exc
            if not isinstance(data, dict):
                raise UndoError(f"invalid snapshot metadata for {commit}: expected object")
            return data
    return {}


def undo(
    repo: Path,
    name: str,
    *,
    no_safety_snapshot: bool = False,
    clean_ignored: bool = False,
    depth: int = 0,
) -> dict[str, str | list[str] | None]:
    if depth > MAX_SUBMODULE_DEPTH:
        raise UndoError(
            f"submodule recursion depth {depth} exceeds limit {MAX_SUBMODULE_DEPTH}"
        )

    repo = repo.resolve()
    ref, commit = resolve_snapshot(repo, name)
    metadata = read_snapshot_metadata(repo, commit)
    include_ignored = metadata.get("include_ignored") is True
    ignored_paths = metadata.get("ignored_paths")
    preserve_ignored_paths: set[str] | None = None
    if include_ignored:
        preserve_ignored_paths = set()
    elif clean_ignored:
        if not isinstance(ignored_paths, list):
            raise UndoError(
                "snapshot lacks ignored-path metadata; refusing to clean ignored files"
            )
        preserve_ignored_paths = {item for item in ignored_paths if isinstance(item, str)}

    # Submodule metadata is empty for v2 snapshots, which keeps undo backward compatible.
    submodule_snapshots = metadata.get("submodules")
    submodule_map: dict[str, dict[str, str]] = {}
    if isinstance(submodule_snapshots, dict):
        for key, value in submodule_snapshots.items():
            if isinstance(key, str) and isinstance(value, dict):
                child_name = value.get("snapshot_name")
                if isinstance(child_name, str) and child_name:
                    normalized = key.rstrip("/")
                    try:
                        validate_submodule_path(repo, normalized)
                    except UndoError as exc:
                        raise UndoError(
                            f"invalid submodule path in snapshot metadata: {key!r}"
                        ) from exc
                    submodule_map[normalized] = value

    safety = None
    if not no_safety_snapshot:
        safety = snapshot(repo, label="before-undo")

    # Never let parent cleanup delete submodule directories; their contents are
    # restored separately from their own snapshots.
    submodule_dirs = set(initialized_submodule_paths(repo))
    remove_untracked(
        repo,
        preserve_ignored_paths=preserve_ignored_paths,
        preserve_paths=submodule_dirs,
    )
    run_git(repo, ["read-tree", "--reset", "-u", commit])

    index_tree = metadata.get("index_tree")
    restored_index_tree = None
    if isinstance(index_tree, str) and index_tree:
        run_git(repo, ["read-tree", "--reset", index_tree])
        restored_index_tree = index_tree

    # Restore submodules after the parent tree so the final submodule state is
    # decided by each submodule snapshot rather than by the parent gitlink.
    restored_submodules = []
    for sub_rel, child in submodule_map.items():
        sub_path = validate_submodule_path(repo, sub_rel)
        if (
            not is_gitlink(repo, sub_rel)
            or not sub_path.is_dir()
            or not is_git_worktree(sub_path)
        ):
            continue
        try:
            # The root safety snapshot already captured submodules recursively.
            undo(
                sub_path,
                child["snapshot_name"],
                no_safety_snapshot=True,
                clean_ignored=clean_ignored,
                depth=depth + 1,
            )
            restored_submodules.append(sub_rel)
        except UndoError as exc:
            raise UndoError(f"failed to undo submodule {sub_rel!r}: {exc}") from exc

    return {
        "restored_snapshot_name": ref.rsplit("/", 1)[-1],
        "restored_snapshot_ref": ref,
        "restored_snapshot_commit": commit,
        "restored_index_tree": restored_index_tree,
        "restored_submodules": sorted(restored_submodules),
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
            *REF_PREFIXES,
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
    commit = resolve_snapshot(repo, name)[1]
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
        "snapshot", help="Create a Git-backed undo snapshot.", parents=[common]
    )
    snap.add_argument("--label", help="Optional short label to include in the snapshot name.")
    snap.add_argument(
        "--include-ignored",
        action="store_true",
        help="Include ignored files. Use only when planned edits target ignored paths.",
    )
    snap.add_argument(
        "--no-recurse-submodules",
        action="store_true",
        help=(
            "Do not recurse into submodules. Dirty submodule contents are then not "
            "captured by the parent snapshot."
        ),
    )

    restore = subparsers.add_parser(
        "undo", help="Restore a saved undo snapshot.", parents=[common]
    )
    restore.add_argument("snapshot_name", help="Snapshot name returned by the snapshot command.")
    restore.add_argument(
        "--no-safety-snapshot",
        action="store_true",
        help=(
            "Do not snapshot the current state before restoring. Dangerous: current "
            "tracked and untracked changes may be unrecoverable."
        ),
    )
    restore.add_argument(
        "--clean-ignored",
        action="store_true",
        help=(
            "For snapshots that did not include ignored file contents, remove ignored "
            "paths that were not present when the snapshot was created. Ignored files "
            "that already existed may still have modified contents."
        ),
    )

    subparsers.add_parser("list", help="List undo snapshots.", parents=[common])
    show = subparsers.add_parser(
        "show", help="Show one undo snapshot.", parents=[common]
    )
    show.add_argument("snapshot_name", help="Snapshot name to inspect.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        repo = repo_root(args.repo)
        if args.command == "snapshot":
            result = snapshot(
                repo,
                label=args.label,
                include_ignored=args.include_ignored,
                recurse_submodules=not args.no_recurse_submodules,
            )
        elif args.command == "undo":
            result = undo(
                repo,
                args.snapshot_name,
                no_safety_snapshot=args.no_safety_snapshot,
                clean_ignored=args.clean_ignored,
            )
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
