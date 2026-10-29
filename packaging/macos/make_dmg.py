#!/usr/bin/env python3
"""Create a Rock Ridge ISO with no source filesystem extended attributes."""
import os
from pathlib import Path
import stat
import subprocess
import sys
import pycdlib


def make_iso(source, output):
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=4, rock_ridge='1.09', vol_ident='IMG_SWITCH_TEST')
    def visit(directory, parent):
        for index, path in enumerate(sorted(directory.iterdir()), 1):
            if index > 9999999:
                raise ValueError('too many directory entries')
            if path.is_symlink():
                iso.add_symlink(symlink_path=parent + '/L%07d.;1' % index,
                                rr_symlink_name=path.name, rr_path=os.readlink(path))
            elif path.is_dir():
                target = parent + '/D%07d' % index
                iso.add_directory(iso_path=target, rr_name=path.name,
                                  file_mode=stat.S_IFDIR | stat.S_IMODE(path.stat().st_mode))
                visit(path, target)
            elif path.is_file():
                iso.add_file(str(path), iso_path=parent + '/F%07d.;1' % index,
                             rr_name=path.name,
                             file_mode=stat.S_IFREG | stat.S_IMODE(path.stat().st_mode))
            else:
                raise ValueError('unsupported filesystem entry')
    try:
        visit(source, '')
        iso.write(str(output))
    finally:
        iso.close()


def provenance_markers(source):
    """Read marker bytes without printing them; no xattrs are copied to the image."""
    markers = set()
    for path in [source] + list(source.rglob('*')):
        if path.is_symlink():
            continue
        listing = subprocess.run(['xattr', str(path)], capture_output=True, text=True, check=True)
        if 'com.apple.provenance' in listing.stdout.splitlines():
            value = subprocess.run(['xattr', '-px', 'com.apple.provenance', str(path)],
                                   capture_output=True, text=True, check=True)
            marker = bytes.fromhex(value.stdout)
            if len(marker) < 8:
                raise ValueError('invalid provenance marker size')
            markers.add(marker)
    return markers


def check_raw_image(image, markers):
    data = image.read_bytes()
    name = 'com.apple.provenance'
    if any(name.encode(encoding) in data for encoding in ('utf-8', 'utf-16-be', 'utf-16-le')):
        raise ValueError('provenance attribute name found in image')
    if any(marker in data for marker in markers):
        raise ValueError('provenance attribute value found in image')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('Usage: make_dmg.py source-folder output.iso')
    source, output = Path(sys.argv[1]), Path(sys.argv[2])
    markers = provenance_markers(source)
    make_iso(source, output)
    check_raw_image(output, markers)
    print('Rock Ridge image: extended provenance markers absent; checked', len(markers), 'distinct source markers')
