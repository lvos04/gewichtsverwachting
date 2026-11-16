"""Consistent SQLite backups and offline restore into a new, empty directory."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from .db import DATA_DIR, engine

# 'secrets' bevat de sleutel van het Bridge-token; een back-up bevat zo nooit een bruikbaar token.
EXCLUDED_DIRS = {'backups', 'tmp', '__pycache__', 'secrets'}

def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def create_backup() -> Path:
    if engine.dialect.name != 'sqlite' or not engine.url.database:
        raise ValueError('Deze back-upfunctie ondersteunt alleen SQLite-bestandsdatabases.')
    backup_dir = DATA_DIR / 'backups'
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = backup_dir / f'chrysant-{stamp}.zip'
    partial = target.with_suffix('.partial')
    with tempfile.TemporaryDirectory(dir=backup_dir) as tmp:
        snapshot = Path(tmp) / 'app.db'
        source_path = Path(engine.url.database).resolve()
        with sqlite3.connect(str(source_path)) as source, sqlite3.connect(str(snapshot)) as dest:
            source.backup(dest)
            result = dest.execute('PRAGMA integrity_check').fetchone()[0]
            if result != 'ok':
                raise ValueError('Databasecontrole mislukt.')
        entries = [('app.db', snapshot)]
        for path in sorted(DATA_DIR.rglob('*')):
            relative = path.relative_to(DATA_DIR)
            if any(part in EXCLUDED_DIRS for part in relative.parts) or path.is_symlink() or not path.is_file():
                continue
            if path.resolve() == source_path or path.name in {source_path.name + '-wal', source_path.name + '-shm'}:
                continue
            # Never copy a live sqlite database under another filename.
            if relative.as_posix() == 'app.db':
                continue
            entries.append((relative.as_posix(), path))
        with zipfile.ZipFile(partial, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            manifest = {'format': 1, 'created_at': stamp, 'database': 'app.db', 'files': {}}
            for name, path in entries:
                # Hash precisely the bytes written, even while an upload is finishing.
                h = hashlib.sha256()
                with path.open('rb') as src, archive.open(name, 'w') as dst:
                    for chunk in iter(lambda: src.read(1024 * 1024), b''):
                        dst.write(chunk)
                        h.update(chunk)
                manifest['files'][name] = h.hexdigest()
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        os.chmod(partial, 0o600)
        os.replace(partial, target)
    return target

def restore_backup(archive_path: Path, destination: Path) -> dict:
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Herstel alleen naar een lege map; stop de app vóór omschakelen.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        staging = Path(tmp)
        with zipfile.ZipFile(archive_path) as archive:
            if len(archive.infolist()) > 100000:
                raise ValueError('Te veel archiefbestanden.')
            manifest = json.loads(archive.read('manifest.json'))
            if manifest.get('format') != 1 or manifest.get('database') != 'app.db':
                raise ValueError('Onbekend back-upformaat.')
            allowed = set(manifest.get('files', {}))
            names = [i.filename for i in archive.infolist()]
            if len(names) != len(set(names)) or set(names) != allowed | {'manifest.json'}:
                raise ValueError('Bestandslijst komt niet overeen met manifest.')
            for name, expected in manifest['files'].items():
                relative = Path(name)
                if relative.is_absolute() or '..' in relative.parts or '\\' in name or name == 'manifest.json':
                    raise ValueError('Onveilig bestandspad.')
                path = staging / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as src, path.open('wb') as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                if _digest(path) != expected:
                    raise ValueError(f'Checksum ongeldig: {name}')
            with sqlite3.connect(str(staging / 'app.db')) as restored:
                if restored.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Herstelde database is beschadigd.')
                if restored.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise ValueError('Herstelde database bevat ongeldige verwijzingen.')
        destination.mkdir(mode=0o700, exist_ok=True)
        for child in staging.iterdir():
            shutil.move(str(child), destination / child.name)
        for path in destination.rglob('*'):
            if path.is_file():
                os.chmod(path, 0o600)
    return manifest

def main():
    parser = argparse.ArgumentParser(description='Lokale back-up en offline herstel')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('create')
    restore = commands.add_parser('restore')
    restore.add_argument('archive', type=Path)
    restore.add_argument('destination', type=Path)
    args = parser.parse_args()
    if args.command == 'create':
        print(create_backup())
    else:
        manifest = restore_backup(args.archive, args.destination)
        print(f"Hersteld: {len(manifest['files'])} bestanden. Start met DATA_DIR={args.destination.resolve()}")

if __name__ == '__main__':
    main()
