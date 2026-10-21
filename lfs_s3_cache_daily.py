# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded daily GC with a visible, rotated log for home mcron."""
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / 'resources')
    base = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'lfs-s3-cache'
    base.mkdir(parents=True, exist_ok=True)
    log = base / 'daily-gc.log'
    if log.exists() and log.stat().st_size > 2 * 1024**2:
        log.replace(log.with_suffix('.log.1'))
    with log.open('a') as stream:
        stream.write(f'\n{datetime.now().astimezone().isoformat()} daily GC: {repo}\n')
        stream.flush()
        proc = subprocess.run([sys.executable, '-m', 'lfs_s3_cache', '--repo', repo,
                               'gc', '--apply', '--max-files', '5'],
                              stdout=stream, stderr=subprocess.STDOUT)
        stream.write(f'exit: {proc.returncode}\n')
    print(f'LFS cache GC exit {proc.returncode}; log: {log}')
    return proc.returncode


if __name__ == '__main__':
    sys.exit(main())
