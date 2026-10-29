#!/usr/bin/env python3
"""Copy only the files needed by an offline app build."""
import argparse
from pathlib import Path
import shutil

REQUIRED = ('app.py', 'LICENSE', 'engine/SKILL.md',
            'engine/scripts/failovercfg.py', 'engine/scripts/failover_imagegen_tool.py',
            'engine/scripts/setup.py')


def stage(root, destination):
    for relative in REQUIRED:
        source = root / relative
        if not source.is_file() or source.is_symlink():
            raise ValueError('missing or invalid input: ' + relative)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    stage(args.root, args.destination)
