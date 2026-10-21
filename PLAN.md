# Optional Git LFS cache for the resources library

## Goal and agreed decisions

Give `~/resources` a NAS-like experience while retaining OVH object storage and the existing `lfs-s3` Git LFS transfer agent. A tracked PDF should remain discoverable through Citar when absent locally and download on demand when opened. Long-idle, safely recoverable tracked files may be evicted to reclaim both working-tree and `.git/lfs` space.

- Build a **separate package** (`lfs-s3-cache`), not a rework of `lfs-s3`. Keep the current OVH objects, compression, object IDs, `.gitattributes`, and transfer-agent behavior.
- Integrate automatic hydration with **Citar and Org file links** in v1. Provide an explicit CLI hydration command for other applications; a filesystem mount is outside v1.
- Use OVH **Standard** storage so cold opens can retrieve immediately.
- Never evict untracked files. Report their count and size separately; their import is a later project.
- Ravanan is a workflow-output cache, not an interactive document archive. Git-annex has suitable get/drop concepts but would require a storage migration. Rclone mount cannot expose the existing compressed object-ID blobs as named documents without changing the archive layout.

## Package and behavior

Provide `lfs-s3-cache --repo PATH` with `status`, `activate`, `sync`, `hydrate PATH`, `pin PATH`, `unpin PATH`, and `gc --dry-run|--apply`. Default GC mode is dry run. Store access times, pins, and excluded paths under `XDG_STATE_HOME`, keyed by repository path, with locking and atomic state writes.

Discover managed files from the current Git index and LFS pointers under `files/library/` and `files/images/`. `activate` starts the inactivity clock for all current tracked files, avoiding immediate eviction of files with unknown prior access. Reconcile state against the current index before each operation, especially after branch changes. Track opens made through the cache tool; direct reads from other applications do not reset the inactivity clock, so users should pin files they need permanently local.

Use local sparse-checkout exclusions for cold paths. A plain LFS pointer in the working tree is not acceptable because it appears modified in Git status. Hydration fetches only the requested object's path through the existing Git LFS agent, verifies its SHA-256 and size, restores the path, then records the open. Citar must list tracked citation files even while absent; its open function and the Org file-link open path must hydrate before normal opening. Leave Citar's normal behavior for non-managed files intact.

GC considers clean, unpinned files after 60 days without a managed open; shorten to 7 days when free space is below 10 GiB. Before evicting each file, confirm that its current pointer exists on the configured origin tracking branch and force an LFS fetch from OVH to prove exact recovery. Verify SHA-256 and size, exclude the working-tree path, confirm it is absent and Git status remains clean, then delete only the corresponding verified local LFS object if no retained path requires that object. Refuse eviction on failed remote verification, dirty files, unsupported sparse checkout, shared LFS storage, or multiple worktrees. A failed fetch must leave the local copy intact.

## Verification and rollout

1. Finish the CLI and test in an isolated Git/LFS repository with a fake or test remote. Cover exact-file hydration, Citar and Org opens, clean status after eviction, pins, both inactivity thresholds, branch changes, duplicate object IDs, missing remote objects, checksum mismatch, dirty files, untracked files, sparse-checkout conflicts, concurrent commands, and interrupted operations.
2. Add Emacs integration as an optional loadable file, then configure the existing Emacs setup to load it. Verify Citar discovery and open behavior with cold and warm files, plus Org file-link opening. Keep existing `lfs-s3` configuration unchanged.
3. In `~/resources`, review `status` and `gc --dry-run`. Pilot one tracked PDF: verify its remote object, evict it, reopen through Citar, compare checksum, and confirm clean Git status. Do not touch untracked library files or unrelated pending wiki edits.
4. Only after the pilot passes, add a daily scheduled GC job using the existing home mcron configuration. Run it conservatively and keep its log visible. Document activation, pinning, explicit hydration, and recovery procedures.

## Handoff state (2026-10-21)

`pyproject.toml` and `lfs_s3_cache.py` are a **partial, untested prototype**. No Citar/Org integration, tests, documentation, schedule, or live pilot have been completed. No live library file has been evicted or activated. Review and correct the prototype against this plan before using `activate` or `gc --apply` on `~/resources`; do not assume its safety checks are complete.

The target package directory was created at `/home/graves/projects/src/lfs-s3-cache`. The live resources repository has extensive unrelated uncommitted and untracked work; preserve it. Its LFS remote uses the existing `lfs-s3` standalone transfer agent. Local sparse checkout was initially disabled, and a one-file isolated experiment showed sparse exclusion can remove a tracked LFS file while leaving Git status clean. The local disk had roughly 5.6 GiB free when planning began.
