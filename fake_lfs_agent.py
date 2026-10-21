# SPDX-License-Identifier: GPL-3.0-or-later
"""Test-only standalone transfer agent implementing the same protocol as lfs-s3."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

remote = Path(sys.argv[1])
if len(sys.argv) > 2 and os.environ.get('FAKE_LFS_ENV') != 'ready':
    sys.exit('missing repository environment')
for line in sys.stdin:
    event = json.loads(line)
    kind = event['event']
    if kind == 'init':
        reply = {}
    elif kind == 'terminate':
        break
    else:
        oid = event['oid']
        obj = remote / 'lfs' / 'objects' / oid[:2] / oid[2:4] / oid
        reply = {'event': 'complete', 'oid': oid}
        if kind == 'upload':
            obj.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(event['path'], obj)
        elif obj.exists():
            with tempfile.NamedTemporaryFile(prefix='fake-lfs-', delete=False) as stream:
                with open(obj, 'rb') as source:
                    shutil.copyfileobj(source, stream)
                reply['path'] = stream.name
        else:
            reply['error'] = {'code': 404, 'message': 'remote missing object'}
    print(json.dumps(reply), flush=True)
