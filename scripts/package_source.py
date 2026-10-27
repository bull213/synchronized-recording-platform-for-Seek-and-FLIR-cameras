"""Create a source ZIP containing only the reviewed SOURCE_FILES.txt inventory."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {'.git', '.venv', '.idea', '.validation', '__pycache__', 'runtime',
                  'build', 'dist', 'exports', 'recordings', 'camera_sdk', 'drivers',
                  'pretrained_models', 'mouse_thermal_results', 'github-upload'}
EXCLUDED_SUFFIXES = {'.pyc', '.pyd', '.dll', '.exe', '.whl', '.zip', '.wav', '.mp3',
                     '.mp4', '.avi', '.mkv', '.csv', '.jsonl', '.pem', '.key', '.pdf'}


def source_files(source=ROOT):
    """Validate the explicit inventory without traversing data or environment folders."""
    source = Path(source).resolve()
    names = (source / 'SOURCE_FILES.txt').read_text(encoding='utf-8').splitlines()
    files, seen = [], set()
    for line in names:
        name = line.strip()
        if not name or name.startswith('#'):
            continue
        relative = PurePosixPath(name)
        if (relative.is_absolute() or relative.as_posix() != name or
                any(part in ('..', '.') for part in relative.parts) or
                '\\' in name or ':' in name):
            raise ValueError(f'Unsafe manifest path: {name}')
        if name.casefold() in seen:
            raise ValueError(f'Duplicate manifest path: {name}')
        seen.add(name.casefold())
        if (any(part.casefold() in EXCLUDED_PARTS for part in relative.parts) or
                relative.suffix.lower() in EXCLUDED_SUFFIXES or
                relative.name.lower().startswith('.env')):
            raise ValueError(f'Private/generated file is not permitted in source: {name}')
        path = source.joinpath(*relative.parts)
        for ancestor in (path, *path.parents):
            if ancestor == source:
                break
            if ancestor.is_symlink() or getattr(ancestor, 'is_junction', lambda: False)():
                raise ValueError(f'Linked files are not permitted: {name}')
        if not path.resolve().is_relative_to(source):
            raise ValueError(f'Manifest path escapes the source folder: {name}')
        if not path.is_file():
            raise FileNotFoundError(f'Manifest file is missing: {name}')
        if path.stat().st_size >= 25 * 1024 * 1024:
            raise ValueError(f'Source file is too large: {name}')
        files.append((name, path))
    if not files:
        raise ValueError('The source inventory is empty.')
    return files


def package_source(output, source=ROOT):
    source = Path(source).resolve()
    output = Path(output).resolve()
    checksum_path = output.with_suffix(output.suffix + '.sha256')
    if output.is_relative_to(source):
        raise ValueError('Write the ZIP outside the source folder.')
    if output.suffix.lower() != '.zip':
        raise ValueError('The source archive must end in .zip.')
    files = source_files(source)
    if output.exists() or checksum_path.exists():
        raise FileExistsError('Choose a new archive name; existing exports are never replaced.')
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also protects against a concurrent export with this name.
    with ZipFile(output, 'x', ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in files:
            archive.write(path, f'UltramicStudio/{name}')
    with ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('Archive integrity verification failed.')
    with output.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    with checksum_path.open('x', encoding='ascii', newline='\n') as handle:
        handle.write(f'{digest}  {output.name}\n')
    return len(files), digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', action='store_true', help='Validate the reviewed inventory')
    action.add_argument('--output', type=Path, help='New ZIP path outside the source folder')
    args = parser.parse_args()
    if args.check:
        files = source_files()
        print(f'Approved source inventory: {len(files)} files, {sum(p.stat().st_size for _, p in files):,} bytes')
    else:
        count, digest = package_source(args.output)
        print(f'Source archive: {args.output}\nFiles: {count}\nSHA256: {digest}')


if __name__ == '__main__':
    main()
