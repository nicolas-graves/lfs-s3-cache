# SPDX-License-Identifier: GPL-3.0-or-later
"""Real Git/LFS integration tests; no network or user configuration is used."""
import hashlib
import json
import os
from pathlib import Path
from contextlib import contextmanager
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import lfs_s3_cache as mod


class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'repo'
        self.remote = self.base / 'remote.git'
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null',
                        GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.org',
                        GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.org',
                        XDG_CONFIG_HOME=str(self.base / 'config'),
                        XDG_DATA_HOME=str(self.base / 'data'),
                        XDG_CACHE_HOME=str(self.base / 'cache'))
        self.env.pop('GIT_LFS_SKIP_SMUDGE', None)
        self.envpatch = patch.dict(os.environ, self.env, clear=True)
        self.envpatch.start()
        self.addCleanup(self.envpatch.stop)
        self.cmd('git', 'init', '--bare', str(self.remote))
        self.cmd('git', 'init', '-b', 'main', str(self.repo))
        self.git('lfs', 'install', '--local')
        # Git LFS generates /bin/sh hooks, which is absent in a Guix sandbox.
        # Keep the real hooks, using the shell supplied by the test environment.
        shell = mod.shutil.which('sh')
        self.assertIsNotNone(shell)
        for hook in (self.repo / '.git/hooks').iterdir():
            if not hook.name.endswith('.sample') and hook.is_file():
                content = hook.read_text()
                if content.startswith('#!/bin/sh\n'):
                    hook.write_text('#!' + shell + '\n' + content.split('\n', 1)[1])
        self.git('config', 'lfs.concurrenttransfers', '1')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.org')
        self.git('remote', 'add', 'origin', str(self.remote))
        (self.repo / '.gitattributes').write_text('*.pdf filter=lfs diff=lfs merge=lfs -text\n')
        self.path = 'files/library/paper.pdf'
        self.content = b'%PDF-1.7\n' + b'document data\n' * 30
        self.write(self.path, self.content)
        self.git('add', '.')
        self.git('commit', '-m', 'document')
        self.git('push', '-u', 'origin', 'main')
        self.git('config', 'lfs.standalonetransferagent', 'fake')
        self.git('config', 'lfs.customtransfer.fake.path', sys.executable)
        self.git('config', 'lfs.customtransfer.fake.args',
                 str(Path(__file__).with_name('fake_lfs_agent.py')) + ' ' + str(self.remote))
        self.cache = mod.Cache(self.repo, self.base / 'state')
        self.addCleanup(self.cache.close)
        self.catalog = self.cache.catalog()
        self.cache.activate(self.catalog)
        self.oid, self.size = self.catalog[self.path]
        self.age(61)

    def cmd(self, *args, cwd=None):
        p = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode:
            raise AssertionError(p.stderr.decode())
        return p.stdout

    def git(self, *args):
        return self.cmd('git', *args, cwd=self.repo)

    def write(self, path, content):
        p = self.repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)

    def age(self, days):
        for record in self.cache.state['files'].values():
            record['opened'] = time.time() - days * 86400
        self.cache.save()

    def evict(self, **kwargs):
        self.cache.gc(self.catalog, apply=True, **kwargs)

    def test_evict_hydrate_clean(self):
        self.evict()
        self.assertFalse((self.repo / self.path).exists())
        self.assertFalse(self.cache.object_path(self.oid).exists())
        self.assertEqual(self.git('status', '--porcelain'), b'')
        self.assertIn(self.path.encode(), self.git('ls-files'))
        self.cache.hydrate(self.path, self.catalog)
        self.assertEqual((self.repo / self.path).read_bytes(), self.content)
        self.assertEqual(self.git('status', '--porcelain'), b'')
        self.assertGreater(self.cache.state['files'][self.path]['opened'], time.time() - 10)

    def test_dry_run_and_thresholds(self):
        self.cache.gc(self.catalog)
        self.assertTrue((self.repo / self.path).exists())
        self.age(8)
        with patch.object(mod.shutil, 'disk_usage', return_value=type('D', (), {'free': 20 * 1024**3})()):
            self.evict()
            self.assertTrue((self.repo / self.path).exists())
        with patch.object(mod.shutil, 'disk_usage', return_value=type('D', (), {'free': 5 * 1024**3})()):
            self.evict()
            self.assertFalse((self.repo / self.path).exists())

    def test_pin_cold_and_unpin(self):
        self.cache.pin(self.path, self.catalog)
        self.age(100)
        self.evict()
        self.assertTrue((self.repo / self.path).exists())
        self.cache.pin(self.path, self.catalog, False)
        self.evict()
        self.cache.pin(self.path, self.catalog)
        self.assertTrue((self.repo / self.path).exists())
        self.assertIn(self.path, self.cache.state['pins'])

    def test_dirty_and_untracked(self):
        self.write('files/library/untracked.pdf', b'untracked')
        self.write(self.path, b'modified')
        self.evict()
        self.assertEqual((self.repo / self.path).read_bytes(), b'modified')
        with self.assertRaises(mod.CacheError):
            self.cache.hydrate(self.path, self.catalog)
        self.assertEqual((self.repo / 'files/library/untracked.pdf').read_bytes(), b'untracked')

    def assert_gc_preserves_work(self, expected):
        """Neither normal GC nor the pilot may fetch or remove dirty candidates."""
        self.catalog = self.cache.catalog()
        self.cache.reconcile(self.catalog)
        self.age(100)
        before = self.git('status', '--porcelain', '-z')
        staged = self.git('diff', '--cached', '--binary')
        with patch.object(self.cache, 'download', side_effect=AssertionError('dirty candidate fetched')):
            self.evict()
            for rel in self.catalog:
                self.evict(only=rel, pilot=True)
        for rel, content in expected.items():
            self.assertEqual((self.repo / rel).read_bytes(), content)
        self.assertTrue(self.cache.object_path(self.oid).exists())
        self.assertEqual(self.cache.state['excluded'], [])
        self.assertEqual(self.git('status', '--porcelain', '-z'), before)
        self.assertEqual(self.git('diff', '--cached', '--binary'), staged)

    def test_staged_candidate_edit_is_protected(self):
        self.write(self.path, b'staged document')
        self.git('add', self.path)
        self.assert_gc_preserves_work({self.path: b'staged document'})

    def test_staged_candidate_addition_is_protected(self):
        rel = 'files/library/new.pdf'
        self.write(rel, self.content)
        self.git('add', rel)
        self.cache.pin(self.path, self.catalog)
        self.assert_gc_preserves_work({rel: self.content, self.path: self.content})

    def test_staged_candidate_deletion_is_protected(self):
        self.git('rm', '--cached', self.path)
        self.assert_gc_preserves_work({self.path: self.content})

    def test_staged_candidate_rename_is_protected(self):
        rel = 'files/library/renamed.pdf'
        self.git('mv', self.path, rel)
        self.assert_gc_preserves_work({rel: self.content})
        self.assertFalse((self.repo / self.path).exists())

    def test_partly_staged_candidate_is_protected(self):
        self.write(self.path, b'staged version')
        self.git('add', self.path)
        self.write(self.path, b'newer unstaged version')
        self.assert_gc_preserves_work({self.path: b'newer unstaged version'})

    def test_assume_unchanged_cannot_hide_unstaged_mode_change(self):
        self.git('update-index', '--assume-unchanged', self.path)
        (self.repo / self.path).chmod(0o755)
        self.assertEqual(self.git('status', '--porcelain'), b'')
        self.assert_gc_preserves_work({self.path: self.content})
        self.assertTrue((self.repo / self.path).stat().st_mode & 0o111)

    def test_ignored_filemode_cannot_hide_unstaged_mode_change(self):
        self.git('config', 'core.filemode', 'false')
        (self.repo / self.path).chmod(0o755)
        self.assertEqual(self.git('status', '--porcelain'), b'')
        self.assert_gc_preserves_work({self.path: self.content})
        self.assertTrue((self.repo / self.path).stat().st_mode & 0o111)

    def test_unstaged_edit_is_protected_even_in_pilot(self):
        self.write(self.path, b'unstaged document')
        self.assert_gc_preserves_work({self.path: b'unstaged document'})

    def test_worktree_edit_during_remote_verification_aborts(self):
        download = self.cache.download

        @contextmanager
        def edited(*args):
            with download(*args) as verified:
                self.write(self.path, b'edited during download')
                yield verified

        with patch.object(self.cache, 'download', edited):
            with self.assertRaises(mod.CacheError):
                self.evict(pilot=True, only=self.path)
        self.assertEqual((self.repo / self.path).read_bytes(), b'edited during download')
        self.assertTrue(self.cache.object_path(self.oid).exists())
        self.assertNotIn(self.path, self.cache.state['excluded'])

    def test_index_edit_during_remote_verification_aborts(self):
        download = self.cache.download

        @contextmanager
        def staged(*args):
            with download(*args) as verified:
                self.write(self.path, b'staged during download')
                self.git('add', self.path)
                yield verified

        with patch.object(self.cache, 'download', staged):
            with self.assertRaises(mod.CacheError):
                self.evict()
        self.assertEqual((self.repo / self.path).read_bytes(), b'staged during download')
        self.assertTrue(self.cache.object_path(self.oid).exists())
        self.assertNotIn(self.path, self.cache.state['excluded'])
        self.assertTrue(self.git('diff', '--cached', '--name-only'))

    def test_head_change_during_remote_verification_aborts(self):
        download = self.cache.download

        @contextmanager
        def committed(*args):
            with download(*args) as verified:
                self.git('commit', '--allow-empty', '-m', 'HEAD changed during download')
                yield verified

        with patch.object(self.cache, 'download', committed):
            with self.assertRaises(mod.CacheError):
                self.evict()
        self.assertEqual((self.repo / self.path).read_bytes(), self.content)
        self.assertTrue(self.cache.object_path(self.oid).exists())
        self.assertNotIn(self.path, self.cache.state['excluded'])

    def test_unrelated_staged_and_unstaged_work_is_preserved(self):
        self.write('work.txt', b'staged')
        self.git('add', 'work.txt')
        self.write('work.txt', b'unstaged')
        before = self.git('status', '--porcelain')
        staged = self.git('diff', '--cached')
        self.evict()
        self.assertEqual(self.git('status', '--porcelain'), before)
        self.cache.hydrate(self.path, self.catalog)
        self.assertEqual(self.git('status', '--porcelain'), before)
        self.assertEqual(self.git('diff', '--cached'), staged)
        self.assertEqual((self.repo / 'work.txt').read_bytes(), b'unstaged')

    def test_unpublished_pointer_is_not_evicted(self):
        self.write(self.path, b'new unpublished document')
        self.git('add', self.path)
        self.git('commit', '-m', 'unpublished')
        self.catalog = self.cache.catalog()
        self.cache.reconcile(self.catalog)
        self.age(100)
        self.evict()
        self.assertEqual((self.repo / self.path).read_bytes(), b'new unpublished document')

    def test_missing_remote_preserves_both_copies(self):
        for p in self.remote.rglob(self.oid):
            p.unlink()
        with self.assertRaises(mod.CacheError):
            self.evict()
        self.assertEqual((self.repo / self.path).read_bytes(), self.content)
        self.assertTrue(self.cache.valid(self.cache.object_path(self.oid), self.oid, self.size))

    def test_remote_download_uses_approved_direnv(self):
        if not mod.shutil.which('direnv'):
            self.skipTest('direnv unavailable')
        (self.repo / '.envrc').write_text('export FAKE_LFS_ENV=ready\n')
        self.cmd('direnv', 'allow', str(self.repo))
        self.git('config', 'lfs.customtransfer.fake.args',
                 str(Path(__file__).with_name('fake_lfs_agent.py')) + ' ' + str(self.remote) + ' require-env')
        self.assertNotIn('FAKE_LFS_ENV', os.environ)
        self.evict()
        self.cache.hydrate(self.path, self.catalog)
        self.assertEqual((self.repo / self.path).read_bytes(), self.content)

    def test_corrupt_remote_preserves_copy(self):
        objects = list(self.remote.rglob(self.oid))
        self.assertTrue(objects)
        for p in objects:
            p.write_bytes(b'bad data')
        with self.assertRaises(mod.CacheError):
            self.evict()
        self.assertEqual((self.repo / self.path).read_bytes(), self.content)

    def test_duplicate_oid_retains_object_outside_managed(self):
        self.write('other/copy.pdf', self.content)
        self.git('add', '.')
        self.git('commit', '-m', 'copy')
        self.git('push')
        self.evict()
        self.assertTrue(self.cache.object_path(self.oid).exists())
        self.assertEqual((self.repo / 'other/copy.pdf').read_bytes(), self.content)

    def test_duplicate_managed_paths_can_both_be_evicted(self):
        self.write('files/images/copy.pdf', self.content)
        self.git('add', '.')
        self.git('commit', '-m', 'copy')
        self.git('push')
        self.catalog = self.cache.catalog()
        self.cache.reconcile(self.catalog)
        self.age(100)
        self.evict()
        self.assertFalse(self.cache.object_path(self.oid).exists())
        self.assertEqual(len(self.cache.state['excluded']), 2)
        self.assertEqual(self.git('status', '--porcelain'), b'')

    def test_branch_change_resets_age_and_fetches_index_oid(self):
        self.evict()
        self.git('switch', '-c', 'new')
        # Materialize first, commit a new object, then evict on its own upstream.
        self.cache.hydrate(self.path, self.catalog)
        self.write(self.path, b'new version')
        self.git('add', self.path)
        self.git('commit', '-m', 'new object')
        self.git('push', '-u', 'origin', 'new')
        self.catalog = self.cache.catalog()
        self.cache.reconcile(self.catalog)
        self.assertGreater(self.cache.state['files'][self.path]['opened'], time.time() - 10)
        self.age(100)
        self.evict()
        self.git('switch', 'main')
        self.catalog = self.cache.catalog()
        self.cache.reconcile(self.catalog)
        self.cache.hydrate(self.path, self.catalog)
        self.assertEqual((self.repo / self.path).read_bytes(), self.content)

    def test_sparse_conflict_refused(self):
        self.cache.sparse_file.write_text('/*\n!/unrelated\n')
        with self.assertRaises(mod.CacheError):
            self.cache.reconcile(self.catalog)

    def test_multiple_worktrees_and_shared_storage_refused(self):
        self.git('worktree', 'add', '--detach', str(self.base / 'other'))
        with self.assertRaises(mod.CacheError):
            self.cache.require_active()
        self.git('worktree', 'remove', str(self.base / 'other'))
        self.cache.media = self.base / 'shared'
        with self.assertRaises(mod.CacheError):
            self.cache.require_active()

    def test_interruption_before_sparse_keeps_warm_copy(self):
        self.cache.state['pending'] = {'after': [self.path]}
        self.cache.save()
        self.cache.reconcile(self.catalog)
        self.assertTrue((self.repo / self.path).exists())
        self.assertEqual(self.cache.state['excluded'], [])

    def test_interruption_after_sparse_keeps_recoverable_object(self):
        self.cache.transition([self.path])
        self.cache.reconcile(self.catalog)
        self.assertEqual(self.cache.state['excluded'], [self.path])
        self.assertTrue(self.cache.object_path(self.oid).exists())
        self.cache.hydrate(self.path, self.catalog)
        self.assertEqual((self.repo / self.path).read_bytes(), self.content)

    def test_interrupted_hydration_rolls_pointer_back_to_cold(self):
        self.cache.transition([self.path])
        self.cache.finish_transition()
        self.cache.transition([])
        self.assertEqual((self.repo / self.path).read_bytes(), mod.pointer(self.oid, self.size))
        self.cache.reconcile(self.catalog)
        self.assertFalse((self.repo / self.path).exists())
        self.assertEqual(self.cache.state['excluded'], [self.path])
        self.cache.hydrate(self.path, self.catalog)
        self.assertEqual((self.repo / self.path).read_bytes(), self.content)
        self.assertEqual(self.git('status', '--porcelain'), b'')

    def test_literal_filename_and_only_one_download(self):
        rel = 'files/library/a [b]*?, name.pdf'
        self.write(rel, b'other content')
        self.git('add', '.')
        self.git('commit', '-m', 'literal filename')
        self.git('push')
        self.catalog = self.cache.catalog()
        self.cache.reconcile(self.catalog)
        self.age(100)
        self.evict(only=rel)
        self.assertTrue((self.repo / self.path).exists())
        self.assertFalse((self.repo / rel).exists())
        self.cache.hydrate(rel, self.catalog)
        self.assertEqual((self.repo / rel).read_bytes(), b'other content')
        self.assertEqual(self.git('status', '--porcelain'), b'')

    def test_new_index_path_starts_clock(self):
        self.write('files/library/new.pdf', b'new')
        self.git('add', '.')
        self.catalog = self.cache.catalog()
        self.cache.reconcile(self.catalog)
        self.assertGreater(self.cache.state['files']['files/library/new.pdf']['opened'], time.time() - 10)

    def test_concurrent_command_waits_for_lock(self):
        p = subprocess.Popen([sys.executable, str(Path(mod.__file__).resolve()), '--repo', str(self.repo),
                              '--state-dir', str(self.base / 'state'), 'status'], stdout=subprocess.PIPE)
        try:
            time.sleep(0.15)
            self.assertIsNone(p.poll())
            self.cache.close()
            self.assertEqual(p.wait(timeout=10), 0)
        finally:
            if p.poll() is None:
                p.kill()
            p.communicate()

    def test_emacs_citar_and_org(self):
        if not mod.shutil.which('emacs'):
            if os.environ.get('LFS_CACHE_REQUIRE_EMACS_TESTS'):
                self.fail('Emacs is required for the package integration checks')
            self.skipTest('Emacs unavailable')
        citar_dir = os.environ.get('CITAR_LISP_DIR')
        citar = ([Path(citar_dir) / 'citar-file.el'] if citar_dir else
                 list((Path.home() / '.guix-home/profile/share/emacs/site-lisp').glob('citar-*/citar-file.el')))
        if not citar:
            if os.environ.get('LFS_CACHE_REQUIRE_EMACS_TESTS'):
                self.fail('Citar is required for the package integration checks')
            self.skipTest('Set up Citar on the Emacs load path to run this integration')
        self.evict()
        # Emacs invokes separate CLI processes; release our command lock.
        self.cache.close()
        with patch.dict(os.environ, XDG_STATE_HOME=str(self.base / 'state'),
                        LFS_CACHE_TEST_REPO=str(self.repo),
                        LFS_CACHE_TEST_PROGRAM=os.environ.get('LFS_CACHE_TEST_PROGRAM',
                            str(Path(mod.__file__).with_name('lfs-s3-cache')))):
            self.cmd('emacs', '-Q', '--batch', '-L', str(citar[0].parent),
                     '-L', str(Path(mod.__file__).parent),
                     '-l', str(Path(mod.__file__).with_name('test-lfs-s3-cache.el')))


if __name__ == '__main__':
    unittest.main()
