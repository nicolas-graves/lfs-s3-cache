# Optional Git LFS cache for the resources library

## Goal and agreed decisions

Give `~/resources` a NAS-like experience while retaining OVH object storage and the existing `lfs-s3` Git LFS transfer agent. A tracked PDF should remain discoverable through Citar when absent locally and download on demand when opened. Long-idle, safely recoverable tracked files may be evicted to reclaim both working-tree and `.git/lfs` space.

- Build a **separate package** (`lfs-s3-cache`), not a rework of `lfs-s3`. Keep the current OVH objects, compression, object IDs, `.gitattributes`, and transfer-agent behavior.
- Integrate automatic hydration with **Citar and Org file links** in v1. Provide an explicit CLI hydration command for other applications; a filesystem mount is outside v1.
- Use OVH **Standard** storage so cold opens can retrieve immediately.
- Never evict untracked files or files with staged or unstaged changes. Staging is not permission to evict: new or modified documents must first be committed and their exact pointers present on the configured origin tracking branch. Report untracked files' count and size separately; their import is a later project.
- Ravanan is a workflow-output cache, not an interactive document archive. Git-annex has suitable get/drop concepts but would require a storage migration. Rclone mount cannot expose the existing compressed object-ID blobs as named documents without changing the archive layout.

## Package and behavior

Provide `lfs-s3-cache --repo PATH` with `status`, `activate`, `sync`, `hydrate PATH`, `pin PATH`, `unpin PATH`, and `gc --dry-run|--apply`. Default GC mode is dry run. Store access times, pins, and excluded paths under `XDG_STATE_HOME`, keyed by repository path, with locking and atomic state writes.

Discover managed files from the current Git index and LFS pointers under `files/library/` and `files/images/`. `activate` starts the inactivity clock for all current tracked files, avoiding immediate eviction of files with unknown prior access. Reconcile state against the current index before each operation, especially after branch changes. Track opens made through the cache tool; direct reads from other applications do not reset the inactivity clock, so users should pin files they need permanently local.

Use local sparse-checkout exclusions for cold paths. A plain LFS pointer in the working tree is not acceptable because it appears modified in Git status. Hydration fetches only the requested object's path through the existing Git LFS agent, verifies its SHA-256 and size, restores the path, then records the open. Citar must list tracked citation files even while absent; its open function and the Org file-link open path must hydrate before normal opening. Leave Citar's normal behavior for non-managed files intact.

GC considers clean, unpinned files after 60 days without a managed open; shorten to 7 days when free space is below 10 GiB. Before evicting each file, confirm that its current pointer exists on the configured origin tracking branch and force an LFS fetch from OVH to prove exact recovery. Verify SHA-256 and size, exclude the working-tree path, confirm it is absent and Git status remains clean, then delete only the corresponding verified local LFS object if no retained path requires that object. Refuse eviction on failed remote verification, dirty files, unsupported sparse checkout, shared LFS storage, or multiple worktrees. A failed fetch must leave the local copy intact.

### What counts as safe to evict?

Every candidate must satisfy all of these conditions:

- The path is already tracked as an LFS pointer in both `HEAD` and the current
  index, with no staged additions, modifications, deletions, or renames affecting
  that path.
- The working copy has no unstaged changes and its SHA-256 and size match that
  indexed pointer. Untracked files and modified working copies must remain intact.
- The same pointer exists on the configured origin tracking branch, and a fresh
  download through the existing agent proves recovery of those exact bytes.
- The file is unpinned and meets the inactivity policy, except for the explicit
  single-file pilot's age override.

Recheck the index, working copy, and Git status after remote verification and
before exclusion. Neither `--pilot` nor scheduled GC may bypass the protection
for staged or unstaged work. The cache must never stage, commit, or push user
files itself. A clean committed file normally has nothing to stage; this is the
ordinary eligible case. New or edited documents become eligible only after the
user finishes, commits, and pushes the work, subject to the remaining checks.

## Verification and rollout

1. Finish the CLI and test in an isolated Git/LFS repository with a fake or test remote. Cover exact-file hydration, Citar and Org opens, clean status after eviction, pins, both inactivity thresholds, branch changes, duplicate object IDs, missing remote objects, checksum mismatch, untracked files, unstaged candidate edits, staged candidate edits/additions/deletions/renames, edits made during remote verification, preservation of unrelated staged and unstaged work, sparse-checkout conflicts, concurrent commands, and interrupted operations. Confirm that the pilot cannot bypass dirty-file protections.
2. Add Emacs integration as an optional loadable file, then configure the existing Emacs setup to load it. Verify Citar discovery and open behavior with cold and warm files, plus Org file-link opening. Keep existing `lfs-s3` configuration unchanged.
3. In `~/resources`, review `status` and `gc --dry-run`. Pilot one tracked PDF: verify its remote object, evict it, reopen through Citar, compare checksum, and confirm clean Git status. Do not touch untracked library files or unrelated pending wiki edits.
4. Only after the pilot passes, add a daily scheduled GC job using the existing home mcron configuration. Run it conservatively and keep its log visible. Document activation, pinning, explicit hydration, and recovery procedures.

## Handoff state (2026-10-21, before implementation)

`pyproject.toml` and `lfs_s3_cache.py` are a **partial, untested prototype**. No Citar/Org integration, tests, documentation, schedule, or live pilot have been completed. No live library file has been evicted or activated. Review and correct the prototype against this plan before using `activate` or `gc --apply` on `~/resources`; do not assume its safety checks are complete.

The target package directory was created at `/home/graves/projects/src/lfs-s3-cache`. The live resources repository has extensive unrelated uncommitted and untracked work; preserve it. Its LFS remote uses the existing `lfs-s3` standalone transfer agent. Local sparse checkout was initially disabled, and a one-file isolated experiment showed sparse exclusion can remove a tracked LFS file while leaving Git status clean. The local disk had roughly 5.6 GiB free when planning began.


## Implementation and live rollout (2026-10-21)

- Implemented the Python CLI, atomic locked state, index reconciliation, literal
  sparse exclusions, pins, inactivity thresholds, bounded GC, and interruption
  recovery. Remote recovery is proven by smudging exactly one index pointer into
  a new private LFS store, then checking SHA-256 and size. Repository `.envrc`
  environments are loaded through already-approved direnv for transfers.
- Added optional `lfs-s3-cache.el` for cold Citar discovery (including file fields
  and entirely absent library directories), Citar opens, and Org file-link opens.
  Loaded it in the running Emacs and added the load to
  `~/spheres/info/dots/configuration.scm`, preserving existing edits.
- Installed the package in the user Python environment, with commands under
  `~/.local/bin/` and the optional Elisp under `~/.local/share/emacs/site-lisp/`.
  Usage, safety boundaries, pinning, and recovery are documented in `README.md`.
- Activated the resources repository with fresh inactivity clocks for every
  indexed managed file. The initial inventory found 285; the index continued
  evolving during the rollout, and final reconciliation found 292, all warm.
  The 392 untracked library/image files (478,623,643 bytes)
  remain untouched. Initial dry-run GC found zero candidates.
- Live pilot: `files/library/buyck_2021_matching_jeunes.pdf` (44,082 bytes),
  SHA-256 `1ae73ad546b9b573644fdd42d246adceb4e66a6df0d85bae5675b24ec743dc87`.
  Verified OVH recovery through the unchanged `lfs-s3` agent, evicted the working
  path and local object, confirmed cold Citar discovery, reopened with Citar,
  verified checksum/size, and opened the restored document through Org. The
  repository's complete Git status and staged diff match the pre-pilot capture;
  every untracked library/image path, size, and modification time is unchanged.
- Added a 03:00 daily home mcron job, limited to five normal evictions per run.
  Reloaded only the current mcron service with its original two jobs plus the
  cache job; its schedule confirms the next cache run. A manual daily run
  completed with zero evictions and exit status 0. The detailed log is
  `$XDG_STATE_HOME/lfs-s3-cache/daily-gc.log`, currently
  `~/.local/var/lib/lfs-s3-cache/daily-gc.log`.
- The live mcron override is stored under the cache state directory. The
  declarative Emacs/mcron edits become persistent across future logins with the
  next home reconfigure. A full reconfigure was not run because that would also
  deploy unrelated pending home-configuration changes.
- Final validation: all 21 Python integration tests passed, including four ERT
  checks for Citar/Org behavior, absent library directories, and repository path
  expansion. Also checked Emacs byte compilation, Python compilation, Scheme
  parsing, installed/source equality, and whitespace checks. The final live
  status has zero cold files; all 392 untracked file snapshots, the complete Git
  status, and the staged diff still match the pre-pilot capture.

## Safety refinement: preserve uncommitted work

GC now explicitly compares the candidate's committed `HEAD` entry with its
stage-zero index entry, including the executable mode, before considering
eviction. It checks working-copy bytes, executable permissions, and Git status,
and refuses assume-unchanged or unmanaged skip-worktree flags. After the remote
download it repeats eligibility checks and aborts if `HEAD`, the configured
upstream, its pointer, the catalog, or tracked status changed. The age override
uses the same checks.

Regression coverage includes staged additions, edits, deletions, renames,
partially staged documents, unstaged edits, hidden permission changes, and
working-tree/index/HEAD changes during remote verification. Each dirty-file
case checks that contents, local objects, index state, and Git status survive
both ordinary GC and the single-file pilot.

Validation: all 32 integration tests passed, including the Citar/Org ERT checks;
Python compilation and whitespace checks also passed.
