"""Publication boundaries: unrelated recordings, linked files, and stale manifests."""
from zipfile import ZipFile

import pytest

from scripts.package_source import package_source, source_files


def test_only_approved_files_are_archived_including_hidden_rules(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'SOURCE_FILES.txt').write_text('README.md\n.gitignore\n', encoding='utf-8')
    (source / 'README.md').write_text('Synthetic test project', encoding='utf-8')
    (source / '.gitignore').write_text('recordings/\n', encoding='utf-8')
    (source / 'recordings').mkdir()
    (source / 'recordings' / 'private.wav').write_bytes(b'unapproved test sentinel')
    archive = tmp_path / 'source.zip'
    count, digest = package_source(archive, source)
    assert count == 2 and len(digest) == 64
    with ZipFile(archive) as zipped:
        assert set(zipped.namelist()) == {'UltramicStudio/README.md', 'UltramicStudio/.gitignore'}
    assert archive.with_suffix('.zip.sha256').read_text().startswith(digest)
    before = archive.read_bytes()
    with pytest.raises(FileExistsError):
        package_source(archive, source)
    assert archive.read_bytes() == before


@pytest.mark.parametrize('name', ['../private.txt', 'C:/private.txt', '.venv/library.py',
                                 'recordings/data.txt', 'take.wav', '.env', 'missing.py'])
def test_unsafe_or_missing_inventory_entries_fail(tmp_path, name):
    (tmp_path / 'SOURCE_FILES.txt').write_text(name + '\n', encoding='utf-8')
    with pytest.raises((ValueError, FileNotFoundError)):
        source_files(tmp_path)


def test_archive_cannot_be_created_inside_source(tmp_path):
    with pytest.raises(ValueError, match='outside'):
        package_source(tmp_path / 'source.zip', tmp_path)
