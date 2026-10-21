# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in working-tree and local-object cache for Git LFS (Unix)."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

POINTER = re.compile(rb"\Aversion https://git-lfs.github.com/spec/v1\noid sha256:([0-9a-f]{64})\nsize ([0-9]+)\n?\Z")
MANAGED_DIRS = ("files/library", "files/images")
NORMAL_AGE = 60 * 86400
LOW_SPACE_AGE = 7 * 86400
LOW_SPACE_BYTES = 10 * 1024**3


class CacheError(Exception):
    pass


def run(*args, cwd=None, input=None, check=True, env=None, stdout=subprocess.PIPE):
    proc = subprocess.run(args, cwd=cwd, input=input, stdout=stdout,
                          stderr=subprocess.PIPE, env=env)
    if check and proc.returncode:
        raise CacheError(f"{' '.join(map(str, args))}: {proc.stderr.decode(errors='replace').strip()}")
    return proc


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pointer(oid, size):
    return f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {size}\n".encode()


def patterns(excluded):
    # gitignore syntax, anchored and literal, including spaces and metacharacters.
    def escape(path):
        if "\n" in path or "\r" in path:
            raise CacheError("sparse checkout cannot represent newline paths")
        return "".join("\\" + c if c in "\\*?[] " else c for c in path)
    return "/*\n" + "".join(f"!/{escape(p)}\n" for p in sorted(excluded))


class Cache:
    def __init__(self, repo, state_dir=None):
        self.root = Path(run("git", "rev-parse", "--show-toplevel", cwd=repo).stdout.decode().strip()).resolve()
        self.gitdir = Path(run("git", "rev-parse", "--absolute-git-dir", cwd=self.root).stdout.decode().strip()).resolve()
        env = self.git("lfs", "env").stdout.decode()
        match = re.search(r"^LocalMediaDir=(.*)$", env, re.M)
        if not match:
            raise CacheError("Git LFS has no local media directory")
        self.media = Path(match.group(1)).resolve()
        self.sparse_file = Path(os.fsdecode(self.git("rev-parse", "--git-path", "info/sparse-checkout").stdout.strip()))
        if not self.sparse_file.is_absolute():
            self.sparse_file = self.root / self.sparse_file
        base = Path(state_dir or os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        self.state_path = base / "lfs-s3-cache" / (hashlib.sha256(os.fsencode(self.root)).hexdigest()[:20] + ".json")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = open(str(self.state_path) + ".lock", "a+")
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        if self.state and (self.state.get("repo") != str(self.root) or self.state.get("version") != 1):
            self.close()
            raise CacheError("unsupported cache state or repository identity; inspect state before continuing")

    def close(self):
        self.lock.close()

    def git(self, *args, repo_env=False, **kwargs):
        prefix = ("git",)
        if repo_env and (self.root / ".envrc").is_file():
            if not shutil.which("direnv"):
                raise CacheError("repository uses .envrc; install direnv for remote downloads")
            # Use the already-approved repository environment, including from
            # Emacs/mcron. Never run `direnv allow` or persist credentials.
            prefix = ("direnv", "exec", str(self.root), "git")
        return run(*prefix, *args, cwd=self.root, **kwargs)

    def save(self):
        fd, name = tempfile.mkstemp(dir=self.state_path.parent, prefix=".state-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self.state, stream, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.state_path)
            directory = os.open(self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def catalog(self, managed=True):
        """Read index pointers in batches; never load large non-LFS blobs."""
        args = ("--", *MANAGED_DIRS) if managed else ()
        entries = self.git("ls-files", "--stage", "-z", *args).stdout.split(b"\0")
        blobs = {}
        for entry in filter(None, entries):
            meta, raw = entry.split(b"\t", 1)
            mode, oid, stage = meta.split()
            if stage != b"0":
                raise CacheError("unmerged index; resolve conflicts first")
            if mode == b"100644" or mode == b"100755":
                blobs[os.fsdecode(raw)] = oid
        if not blobs:
            return {}
        sizes = self.git("cat-file", "--batch-check=%(objectname) %(objectsize)",
                         input=b"\n".join(set(blobs.values())) + b"\n").stdout.splitlines()
        small = {oid for oid, size in (row.split() for row in sizes) if int(size) <= 1024}
        selected = {path: oid for path, oid in blobs.items() if oid in small}
        data = self.git("cat-file", "--batch", input=b"\n".join(selected.values()) + b"\n").stdout if selected else b""
        result = {}
        offset = 0
        for path in selected:
            end = data.index(b"\n", offset)
            size = int(data[offset:end].split()[2])
            blob = data[end + 1:end + 1 + size]
            offset = end + size + 2
            match = POINTER.fullmatch(blob)
            if match:
                result[path] = (match[1].decode(), int(match[2]))
        if managed and result:
            attrs = self.git("check-attr", "--cached", "-z", "--stdin", "filter",
                             input=b"\0".join(os.fsencode(p) for p in result) + b"\0").stdout.split(b"\0")
            lfs = {os.fsdecode(attrs[i]) for i in range(0, len(attrs) - 1, 3)
                   if attrs[i + 2] == b"lfs"}
            result = {p: value for p, value in result.items() if p in lfs}
        return result

    def relative(self, name, catalog):
        # Do not resolve symlinks: a tracked path must retain its index identity.
        full = Path(os.path.abspath(self.root / name))
        try:
            rel = full.relative_to(self.root).as_posix()
        except ValueError:
            raise CacheError("path is outside this repository") from None
        if rel not in catalog:
            raise CacheError(f"not a tracked LFS pointer in managed directories: {rel}")
        self.safe_path(rel)
        return rel

    def safe_path(self, rel):
        target = self.root / rel
        if any(p.is_symlink() for p in (target, *target.parents) if p != self.root.parent):
            raise CacheError(f"symlink in managed path: {rel}")
        return target

    def object_path(self, oid):
        obj = self.media / oid[:2] / oid[2:4] / oid
        if any(p.is_symlink() for p in (obj, obj.parent, obj.parent.parent)):
            raise CacheError("symlink in local LFS object storage")
        return obj

    @staticmethod
    def valid(path, oid, size):
        return not path.is_symlink() and path.is_file() and path.stat().st_size == size and sha256(path) == oid

    def guard_sparse(self, initializing=False):
        worktrees = self.git("worktree", "list", "--porcelain").stdout.splitlines()
        if sum(line.startswith(b"worktree ") for line in worktrees) != 1:
            raise CacheError("multiple worktrees share this Git LFS store")
        if self.media != self.gitdir / "lfs/objects" or (self.gitdir / "lfs").is_symlink() or (self.gitdir / "lfs/objects").is_symlink():
            raise CacheError("nonstandard or shared Git LFS storage")
        sparse = self.git("config", "--bool", "core.sparseCheckout", check=False).stdout.strip() == b"true"
        if initializing and not self.state.get("active"):
            if sparse or self.sparse_file.exists():
                raise CacheError("existing sparse checkout is not managed by lfs-s3-cache")
            flags = self.git("ls-files", "-t", "-z").stdout.split(b"\0")
            if any(f.startswith(b"S ") for f in flags):
                raise CacheError("unmanaged skip-worktree entries")
            return
        if not sparse or self.git("config", "--bool", "core.sparseCheckoutCone", check=False).stdout.strip() == b"true":
            raise CacheError("cache sparse checkout disabled or changed")
        allowed = [patterns(self.state["excluded"])]
        if self.state.get("pending"):
            allowed.append(patterns(self.state["pending"]["after"]))
            allowed.append(self.state["pending"].get("before_patterns", allowed[0]))
        if not self.sparse_file.is_file() or self.sparse_file.read_text() not in allowed:
            raise CacheError("sparse checkout patterns changed outside lfs-s3-cache")

    def apply_sparse(self, excluded):
        self.git("sparse-checkout", "set", "--no-cone", "--stdin",
                 input=patterns(excluded).encode(), env=dict(os.environ, GIT_LFS_SKIP_SMUDGE="1"))

    def require_active(self):
        if not self.state.get("active"):
            raise CacheError("run 'lfs-s3-cache activate' first")
        self.guard_sparse()

    def reconcile(self, catalog):
        if not self.state.get("active"):
            return
        self.require_active()
        pending = self.state.get("pending")
        excluded = set(self.state["excluded"])
        if pending:
            # A crash never authorizes removing an existing document. An
            # interrupted sparse expansion may have left only a pointer;
            # roll that path back to cold rather than presenting it as warm.
            for rel in set(pending["after"]) ^ excluded:
                target = self.safe_path(rel)
                if (rel in excluded and target.is_file() and rel in catalog
                        and target.stat().st_size <= 1024
                        and target.read_bytes() == pointer(*catalog[rel])):
                    continue
                if target.exists():
                    excluded.discard(rel)
                else:
                    excluded.add(rel)
        excluded.intersection_update(catalog)
        records = self.state["files"]
        now = time.time()
        updated = {p: records[p] if p in records and records[p]["oid"] == oid else
                   {"oid": oid, "opened": now} for p, (oid, _) in catalog.items()}
        pins = sorted(set(self.state["pins"]).intersection(catalog))
        if pending or sorted(excluded) != self.state["excluded"]:
            self.transition(excluded)
            self.finish_transition()
        if records != updated or self.state["pins"] != pins:
            self.state["files"] = updated
            self.state["pins"] = pins
            self.save()

    def transition(self, excluded):
        self.state["pending"] = {"after": sorted(excluded),
                                 "before_patterns": self.sparse_file.read_text()}
        self.save()
        self.apply_sparse(excluded)

    def finish_transition(self):
        self.state["excluded"] = self.state.pop("pending")["after"]
        self.save()

    def activate(self, catalog):
        if self.state.get("active"):
            self.reconcile(catalog)
            return
        self.guard_sparse(initializing=True)
        now = time.time()
        # Initialize patterns first: an interrupted activation is refused, never guessed.
        self.apply_sparse([])
        self.state = {"version": 1, "repo": str(self.root), "active": True, "activated": now,
                      "files": {p: {"oid": v[0], "opened": now} for p, v in catalog.items()},
                      "pins": [], "excluded": []}
        self.save()

    @contextmanager
    def download(self, rel, oid, size):
        """Smudge exactly one index pointer into an empty, private LFS store.

        No include globs, HEAD assumptions, cached-object success, or store deletion.
        The existing transfer agent handles the original remote format unchanged.
        """
        with tempfile.TemporaryDirectory(prefix="lfs-s3-cache-", dir=self.gitdir) as directory:
            env = dict(os.environ)
            env.pop("GIT_LFS_SKIP_SMUDGE", None)
            env.pop("GIT_LFS_SKIP_DOWNLOAD_ERRORS", None)
            self.git("-c", f"lfs.storage={directory}", "-c", "remote.lfsdefault=origin",
                     "-c", "lfs.fetchinclude=", "-c", "lfs.fetchexclude=",
                     "-c", "lfs.skipdownloaderrors=false", "lfs", "smudge", "--", rel,
                     input=pointer(oid, size), env=env, stdout=subprocess.DEVNULL,
                     repo_env=True)
            obj = Path(directory) / "objects" / oid[:2] / oid[2:4] / oid
            if not self.valid(obj, oid, size):
                raise CacheError(f"remote did not provide verified object for {rel}")
            yield obj

    def hydrate(self, rel, catalog):
        self.require_active()
        oid, size = catalog[rel]
        target = self.safe_path(rel)
        if self.valid(target, oid, size):
            if rel in self.state["excluded"]:
                self.transition(set(self.state["excluded"]) - {rel})
                self.finish_transition()
            self.state["files"][rel]["opened"] = time.time()
            self.save()
            return target
        # A warm pointer is safe to replace; modified documents and untracked blockers are not.
        if target.exists() and (target.stat().st_size > 1024 or target.read_bytes() != pointer(oid, size)):
            raise CacheError(f"refusing to overwrite modified file: {rel}")
        obj = self.object_path(oid)
        if not self.valid(obj, oid, size):
            with self.download(rel, oid, size) as verified:
                obj.parent.mkdir(parents=True, exist_ok=True)
                fd, name = tempfile.mkstemp(dir=obj.parent, prefix=".hydrate-")
                try:
                    with os.fdopen(fd, "wb") as stream, open(verified, "rb") as source:
                        shutil.copyfileobj(source, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(name, obj)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
        if self.catalog() != catalog:
            raise CacheError("index changed during hydration; retry")
        excluded = set(self.state["excluded"]) - {rel}
        self.transition(excluded)
        # Let Git perform the indexed smudge and cache the expanded file's stat
        # data. Writing bytes ourselves leaves a false modified status on Git.
        if target.exists() and (target.stat().st_size > 1024 or target.read_bytes() != pointer(oid, size)):
            raise CacheError(f"file changed during hydration: {rel}")
        if target.exists():
            target.unlink()  # verified pointer only; the full local object is safe
        env = dict(os.environ)
        env.pop("GIT_LFS_SKIP_SMUDGE", None)
        self.git("-c", "lfs.fetchinclude=", "-c", "lfs.fetchexclude=",
                 "checkout-index", "--force", "--index", "--stdin", "-z",
                 input=os.fsencode(rel) + b"\0", env=env)
        if not self.valid(target, oid, size):
            raise CacheError(f"hydration verification failed: {rel}")
        self.state["files"][rel]["opened"] = time.time()
        self.finish_transition()
        return target

    def pin(self, rel, catalog, pinned=True):
        self.require_active()
        if pinned:
            # Pin implies permanently local, even if currently cold.
            self.hydrate(rel, catalog)
        pins = set(self.state["pins"])
        (pins.add if pinned else pins.discard)(rel)
        self.state["pins"] = sorted(pins)
        self.save()

    def remote_ref(self):
        upstream = self.git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
        ref = upstream.stdout.decode().strip()
        if upstream.returncode or not ref.startswith("origin/"):
            raise CacheError("GC requires a configured origin tracking branch")
        return ref

    def clean_status(self):
        return self.git("status", "--porcelain=v1", "-z", "--untracked-files=no").stdout

    def clean_candidate(self, rel, oid, size):
        """Require agreement of HEAD, index, and working copy independently.

        Index membership alone is not evidence that a file was committed.
        Do not trust status alone: index flags can hide working-tree changes.
        """
        literal = ":(literal)" + rel
        head = self.git("ls-tree", "-z", "HEAD", "--", literal, check=False)
        indexed = self.git("ls-files", "--stage", "-z", "--", literal).stdout
        heads = head.stdout.rstrip(b"\0").split(b"\0") if head.stdout else []
        entries = indexed.rstrip(b"\0").split(b"\0") if indexed else []
        if head.returncode or len(heads) != 1 or len(entries) != 1:
            return False
        head_meta, head_path = heads[0].split(b"\t", 1)
        index_meta, index_path = entries[0].split(b"\t", 1)
        mode, kind, blob = head_meta.split()
        if (head_path != os.fsencode(rel) or index_path != head_path
                or kind != b"blob" or mode not in (b"100644", b"100755")
                or index_meta.split() != [mode, blob, b"0"]):
            return False
        committed = POINTER.fullmatch(self.git("cat-file", "blob", os.fsdecode(blob)).stdout)
        if not committed or (committed[1].decode(), int(committed[2])) != (oid, size):
            return False
        flags = self.git("ls-files", "-v", "-z", "--", literal).stdout
        if flags != b"H " + os.fsencode(rel) + b"\0":
            return False  # refuse assume-unchanged and unmanaged skip-worktree
        if self.git("status", "--porcelain=v1", "--", literal).stdout:
            return False
        target = self.safe_path(rel)
        if not self.valid(target, oid, size):
            return False
        return bool(target.stat().st_mode & 0o111) == (mode == b"100755")

    def gc(self, catalog, apply=False, only=None, pilot=False, max_files=None):
        self.require_active()
        free = shutil.disk_usage(self.root).free
        age = LOW_SPACE_AGE if free < LOW_SPACE_BYTES else NORMAL_AGE
        now = time.time()
        ref = self.remote_ref()
        print(f"inactivity threshold: {age // 86400} days; free: {free} bytes", flush=True)
        count = 0
        # Protect any retained indexed LFS path, including outside managed directories.
        all_pointers = self.catalog(managed=False)
        for rel, (oid, size) in catalog.items():
            if only is not None and rel != only:
                continue
            if rel in self.state["excluded"] or rel in self.state["pins"]:
                continue
            if not pilot and now - self.state["files"][rel]["opened"] < age:
                continue
            target = self.safe_path(rel)
            before = self.clean_status()
            head = self.git("rev-parse", "--verify", "HEAD").stdout
            if not self.clean_candidate(rel, oid, size):
                print(f"skip uncommitted, dirty, or missing: {rel}")
                continue
            blob = self.git("show", f"{ref}:{rel}", check=False)
            if blob.returncode or blob.stdout != pointer(oid, size):
                print(f"skip absent/different origin pointer: {rel}")
                continue
            patterns([rel])  # refuse unsupported names even in a dry run
            print(f"{'evict' if apply else 'candidate'} {rel} ({size} bytes)", flush=True)
            count += 1
            if not apply:
                if max_files and count >= max_files:
                    break
                continue
            with self.download(rel, oid, size):
                # Recheck index, contents, and status after the potentially long download.
                if (self.catalog() != catalog or self.clean_status() != before
                        or self.git("rev-parse", "--verify", "HEAD").stdout != head
                        or self.remote_ref() != ref
                        or self.git("show", f"{ref}:{rel}", check=False).stdout != blob.stdout
                        or not self.clean_candidate(rel, oid, size)):
                    raise CacheError(f"repository changed during verification: {rel}")
                excluded = set(self.state["excluded"]) | {rel}
                self.transition(excluded)
                if target.exists() or self.clean_status() != before:
                    raise CacheError(f"eviction did not preserve Git status: {rel}; local object retained")
                self.finish_transition()
                retained = any(p not in excluded and value[0] == oid for p, value in all_pointers.items())
                obj = self.object_path(oid)
                if not retained and self.valid(obj, oid, size):
                    obj.unlink()
            if max_files and count >= max_files:
                break
        print(f"{'evicted' if apply else 'candidates'}: {count}")

    def status(self, catalog, as_json=False):
        excluded = set(self.state.get("excluded", []))
        untracked = self.git("ls-files", "--others", "--exclude-standard", "-z", "--", *MANAGED_DIRS).stdout.split(b"\0")
        paths = [self.root / os.fsdecode(p) for p in untracked if p]
        result = {"repo": str(self.root), "active": bool(self.state.get("active")),
                  "tracked": len(catalog), "cold": len(excluded), "pinned": len(self.state.get("pins", [])),
                  "untracked": len(paths), "untracked_bytes": sum(p.lstat().st_size for p in paths if p.exists()),
                  "free_bytes": shutil.disk_usage(self.root).free,
                  "files": [{"path": p, "oid": oid, "size": size, "cold": p in excluded}
                            for p, (oid, size) in catalog.items()]}
        if as_json:
            print(json.dumps(result))
        else:
            print(f"active: {result['active']}")
            print(f"tracked LFS: {len(catalog)}; cold: {len(excluded)}; pinned: {result['pinned']}")
            print(f"untracked (never evicted): {len(paths)} files, {result['untracked_bytes']} bytes")
            print(f"free: {result['free_bytes']} bytes")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--state-dir", help="override XDG state directory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status").add_argument("--json", action="store_true")
    for name in ("activate", "sync"):
        sub.add_parser(name)
    for name in ("hydrate", "pin", "unpin"):
        sub.add_parser(name).add_argument("path")
    gc = sub.add_parser("gc")
    mode = gc.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    gc.add_argument("--path", help="consider only this tracked file")
    gc.add_argument("--pilot", action="store_true", help="ignore age for one --path with --apply; retain all safety checks")
    gc.add_argument("--max-files", type=int, help="bound candidates/evictions per run")
    args = parser.parse_args(argv)
    if args.command == "gc" and args.pilot and not (args.apply and args.path):
        parser.error("--pilot requires --apply and --path")
    if args.command == "gc" and args.max_files is not None and args.max_files < 1:
        parser.error("--max-files must be positive")
    cache = None
    try:
        cache = Cache(args.repo, args.state_dir)
        catalog = cache.catalog()
        cache.reconcile(catalog)
        if args.command == "status":
            cache.status(catalog, args.json)
        elif args.command == "activate":
            cache.activate(catalog)
        elif args.command == "sync":
            cache.require_active()
        elif args.command in ("hydrate", "pin", "unpin"):
            rel = cache.relative(args.path, catalog)
            if args.command == "hydrate":
                print(cache.hydrate(rel, catalog))
            else:
                cache.pin(rel, catalog, args.command == "pin")
        elif args.command == "gc":
            only = cache.relative(args.path, catalog) if args.path else None
            cache.gc(catalog, args.apply, only, args.pilot, args.max_files)
    except (CacheError, OSError, ValueError) as exc:
        print(f"lfs-s3-cache: {exc}", file=sys.stderr)
        return 1
    finally:
        if cache is not None:
            cache.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
