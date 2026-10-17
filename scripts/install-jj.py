#!/usr/bin/env python3
"""Download jj's official prebuilt binary for this platform into a directory.

Usage: install-jj.py VERSION DEST, e.g. install-jj.py 0.46.0 ~/jj. CI puts DEST on PATH afterwards.
"""

import io
import platform
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

# The release assets git-p4son's CI runs on, by (system, machine).
ASSETS = {
    ('Linux', 'x86_64'): 'x86_64-unknown-linux-musl.tar.gz',
    ('Windows', 'AMD64'): 'x86_64-pc-windows-msvc.zip',
    ('Darwin', 'arm64'): 'aarch64-apple-darwin.tar.gz',
}


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    version, dest = sys.argv[1].removeprefix('v'), Path(sys.argv[2])
    system, machine = platform.system(), platform.machine()
    suffix = ASSETS.get((system, machine))
    if suffix is None:
        print(f'Error: no jj release asset for {system} {machine}',
              file=sys.stderr)
        return 1
    url = f'https://github.com/jj-vcs/jj/releases/download/v{version}/jj-v{version}-{suffix}'
    print(f'Downloading {url}')
    with urllib.request.urlopen(url) as response:
        data = response.read()

    name = 'jj.exe' if suffix.endswith('.zip') else 'jj'
    dest.mkdir(parents=True, exist_ok=True)
    if suffix.endswith('.zip'):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            (dest / name).write_bytes(archive.read(name))
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            names = archive.getnames()
            member = archive.getmember(
                f'./{name}' if f'./{name}' in names else name)
            extracted = archive.extractfile(member)
            assert extracted is not None
            (dest / name).write_bytes(extracted.read())
            (dest / name).chmod(0o755)
    print(f'Installed jj {version} as {dest / name}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
