# lfs-s3-cache

An opt-in local cache for indexed Git LFS documents under `files/library/`
and `files/images/`. It uses the existing Git LFS transfer agent and remote
objects unchanged. Python 3.10+, Git, Git LFS, and Unix file locking are required.
Use immediately retrievable storage (OVH Standard), rather than an archival tier.
When the repository contains `.envrc`, remote downloads run through `direnv exec`
using its existing approval. This lets Emacs and mcron use the same OVH settings
as an interactive shell. Install direnv and approve that environment yourself;
the cache never authorizes an environment or stores credentials in its state.

## Install and activate

The Guix module exports `lfs-s3-cache` and the optional `emacs-lfs-s3-cache`.
Build and install from this checkout:

```sh
guix build -f guix.scm
guix build -L guix emacs-lfs-s3-cache
guix install -L guix lfs-s3-cache emacs-lfs-s3-cache
```

The CLI package wraps its commands with store paths for Git, Git LFS, and direnv.
The existing transfer agent, remote configuration, and credentials remain your
repository's responsibility. Builds run all Python integration tests and require
the Citar/Org ERT checks using declared native inputs, without the user's profile
or network access. `guix shell -D -f guix.scm` opens a development environment.

The growl-at channel provides both packages from a pinned Git revision of
<https://github.com/nicolas-graves/lfs-s3-cache>. With that channel configured:

```sh
guix install lfs-s3-cache emacs-lfs-s3-cache
```

Import `(growl-at packages lfs-s3-cache)` in Guix Home configurations using the
channel packages. The checkout recipe above remains available for development;
its file selection excludes Git metadata, build products, and credentials.

After installation:

```sh
lfs-s3-cache --repo ~/resources status
lfs-s3-cache --repo ~/resources activate
lfs-s3-cache --repo ~/resources gc --dry-run
```

Alternatively, install with `python3 -m pip install --user --no-build-isolation .`.
The source and both Guix packages use GPL-3.0-or-later; see `COPYING`.

Activation starts the inactivity clock for every current tracked document.
Nothing becomes eligible immediately. Newly indexed paths and changed object
IDs get a new clock on the next command. The current index, including staged
pointers, defines the catalog; GC additionally requires a clean file and an
identical pointer on the configured `origin` tracking branch.

```sh
lfs-s3-cache --repo ~/resources hydrate files/library/citekey.pdf
lfs-s3-cache --repo ~/resources pin files/library/citekey.pdf
lfs-s3-cache --repo ~/resources unpin files/library/citekey.pdf
lfs-s3-cache --repo ~/resources sync
lfs-s3-cache --repo ~/resources gc --apply --max-files 5
```

Relative document arguments are relative to `--repo`. Hydration prints an
absolute path. Pinning hydrates a cold file and keeps it local. Opens through
this CLI or the Emacs integration reset its clock. **Direct reads from other
applications do not reset the clock. Pin documents you need permanently local.**
Untracked files are reported separately and never imported or evicted.

## Emacs

With `emacs-lfs-s3-cache` in your Guix profile, enable the integration:

```elisp
(require 'lfs-s3-cache)
(setq lfs-s3-cache-repositories '("~/resources"))
(lfs-s3-cache-mode 1)
```

The Emacs package generates autoloads, byte-compiles the library, and defaults
to the matching CLI's store path. Replace any earlier hardcoded `~/.local/`
load/program settings when switching to the Guix package. For a pip installation,
load `~/.local/share/emacs/site-lisp/lfs-s3-cache/lfs-s3-cache.el` explicitly and
set `lfs-s3-cache-program` to `~/.local/bin/lfs-s3-cache`.

Citar library discovery includes indexed documents even while absent, using
Citar's filename/key matching rules. Bibliography `file` fields can also resolve
cold managed paths. `citar-file-open` and Org file links hydrate before the normal
opener runs. Warm opens update the clock too. Other files keep their normal
behavior. Opening cold files through arbitrary applications or plain `find-file`
is outside this integration. Disable with `(lfs-s3-cache-mode -1)`.

## Eviction and recovery

GC defaults to a dry run. Clean, unpinned documents become eligible after 60 days,
or 7 days when free space is below 10 GiB. Each applied eviction first downloads
exactly that pointer through Git LFS into an empty private store and verifies its
SHA-256 and size. This proves remote recovery even when a local object exists.
It then excludes the working path using local non-cone sparse checkout and
checks that it is absent and tracked Git status is unchanged. It removes the
verified local object only when no retained indexed path anywhere in the repo
requires that object. Duplicate content can therefore reclaim working-tree space
while retaining a shared local object.

The cache refuses foreign or edited sparse-checkout patterns, multiple
worktrees, nonstandard LFS storage, conflicts, dirty candidates, and symlinked
paths. It preserves unrelated staged and unstaged edits. A failed remote check
leaves both local copies intact. Do not run other Git writes during a GC or
hydration operation: the cache serializes its own commands, not arbitrary Git
processes. Git index locks and checks around transfers provide additional guards.

Untracked documents and documents with staged or unstaged changes are protected.
Staging alone never makes a document eligible: finish, commit, and push new or
edited documents first. A clean committed document has nothing to stage and can
be eligible under the other checks. The cache never stages, commits, or pushes
files for you; the pilot's age override does not bypass these protections.
GC explicitly checks that the committed entry and index entry agree, then
verifies the working file's content and executable permissions. It refuses
index flags that can hide changes and repeats eligibility checks after the
remote download, aborting if the repository changed during verification.

State lives in `$XDG_STATE_HOME/lfs-s3-cache/` (default `~/.local/state/`), keyed
by the canonical repository path. Writes use fsync and atomic rename; an advisory
lock serializes commands. Use the same state directory for all commands and
Emacs. A saved transition allows the next command to recover an interruption
without authorizing deletion of an existing working copy. Abrupt termination can
leave temporary `.git/lfs-s3-cache-*` download directories; with no command
running, inspect and remove those directories to recover their space.

After a branch change, run `sync`, or use any other cache command; it reconciles
paths, object IDs, pins, and exclusions against the index. Missing or corrupt
local objects are fetched again on hydration. When recovery is refused, inspect
the error and preserve the state file, sparse patterns, index, and local LFS
objects before intervening. Never delete state while sparse exclusions remain.

To retire the cache: hydrate every entry marked `cold` in `status --json`, disable
the daily job and Emacs mode, then run `git sparse-checkout disable`. Preserve
state until every desired file is restored. No remote deletion is performed.

A supervised single-file pilot can bypass only inactivity with
`gc --apply --path files/library/citekey.pdf --pilot`. It retains all other
checks. This flag is never used by the daily job.

## Daily home mcron job

After the pilot succeeds, add this service to your existing Guix Home services.
Import `(growl-at packages lfs-s3-cache)`, `(guix gexp)`, `(gnu services)`, and
`(gnu home services mcron)` in the configuration:

```scheme
(simple-service
 'lfs-s3-cache-gc home-mcron-service-type
 (list #~(job '(next-hour '(3))
              (lambda ()
                (system* #$(file-append lfs-s3-cache "/bin/lfs-s3-cache-daily")
                         (string-append (getenv "HOME") "/resources"))))))
```

`lfs-s3-cache-daily` applies normal GC to at most five files per run. Its output
is appended to `~/.local/state/lfs-s3-cache/daily-gc.log` (honoring
`XDG_STATE_HOME`); a previous log is retained when it exceeds 2 MiB. mcron also
receives the exit code and log path. The activation grace period still applies.

## Tests

```sh
python3 -m unittest -v
```

Tests use isolated repositories, a local bare remote, and a fake standalone
transfer agent implementing the protocol used by `lfs-s3`. They cover recovery
verification failures, exact-path hydration, clean status, thresholds, pins,
branch reconciliation, shared content, sparse conflicts, locking, and interrupted
transitions. Emacs tests exercise real Citar discovery/opening and Org link opens;
they run when Emacs and the local Citar installation are available. Set
`CITAR_LISP_DIR` to the directory containing `citar-file.el` when it is outside
the Guix Home profile. Test direnv approvals and caches stay inside the fixture.
