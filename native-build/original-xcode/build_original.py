#!/usr/bin/env python3
"""Trusted, bounded original-project ZIP preparation; optional explicitly approved build."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import struct
import shutil
import subprocess
import unicodedata
import uuid
import zipfile
import xml.etree.ElementTree as ET

COMPRESSED = 20 * 1024 * 1024
EXPANDED = 50 * 1024 * 1024
FILE = 10 * 1024 * 1024
COUNT = 2000


def valid_path(name):
    if (not isinstance(name, str) or not name or len(name) > 1024
            or unicodedata.normalize('NFC', name) != name
            or re.search(r'[\\\x00-\x1f\x7f]', name)
            or name.startswith('/') or re.match(r'^[A-Za-z]:', name)
            or any(not x or x in ('.', '..') or x != x.strip() for x in name.rstrip('/').split('/'))):
        raise ValueError('Unsafe archive path')
    return name


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def load_manifest(snapshot, request_id, source_sha):
    if str(uuid.UUID(request_id)) != request_id or not re.fullmatch('[0-9a-f]{40}', source_sha):
        raise ValueError('Canonical UUID and full commit SHA required')
    path = snapshot / 'original-xcode/requests' / (request_id + '.json')
    if path.is_symlink() or path.stat().st_size > 16000:
        raise ValueError('Invalid manifest')
    m = json.loads(path.read_text(), object_pairs_hook=unique_json)
    if (not isinstance(m, dict) or set(m) != {'version', 'request_id', 'zip_path', 'sha256', 'project_path', 'scheme_path', 'scheme'}
            or type(m['version']) is not int or m['version'] != 1
            or m['request_id'] != request_id
            or m['zip_path'] != f'original-xcode/sources/{request_id}.zip'
            or not isinstance(m['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', m['sha256'])):
        raise ValueError('Invalid manifest identity')
    for key in ('project_path', 'scheme_path'):
        valid_path(m[key])
    if not m['project_path'].endswith('.xcodeproj') or m['scheme_path'] != f"{m['project_path']}/xcshareddata/xcschemes/{m['scheme']}.xcscheme":
        raise ValueError('Shared scheme must belong to selected project')
    if not isinstance(m['scheme'], str) or not re.fullmatch(r'[\w .-]{1,100}', m['scheme']) or m['scheme'].startswith('-'):
        raise ValueError('Invalid scheme name')
    # A Git checkout may contain symlinks. Reject them in every input ancestor.
    for relative in (str(path.relative_to(snapshot)), m['zip_path']):
        cursor = snapshot
        for part in Path(relative).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError('Snapshot symlink rejected')
    return m


def prepare(snapshot, request_id, source_sha, output):
    snapshot, output = Path(snapshot).resolve(), Path(output)
    m = load_manifest(snapshot, request_id, source_sha)
    archive = snapshot / m['zip_path']
    if archive.stat().st_size > COMPRESSED:
        raise ValueError('Compressed ZIP exceeds 20 MiB')
    blob = archive.read_bytes()
    if hashlib.sha256(blob).hexdigest() != m['sha256']:
        raise ValueError('ZIP SHA256 mismatch')
    if output.exists() and (output.is_symlink() or not output.is_dir() or any(output.iterdir())):
        raise ValueError('Output must be empty or absent')
    end = blob.rfind(b'PK\x05\x06', max(0, len(blob)-65557))
    if end < 0 or end + 22 > len(blob):
        raise ValueError('Missing ZIP end record')
    disk, central_disk, disk_count, count, size, start, comment = struct.unpack_from('<4H2IH', blob, end+4)
    if disk or central_disk or disk_count != count or count == 65535 or size == 0xffffffff or start == 0xffffffff or start+size != end or end+22+comment != len(blob):
        raise ValueError('ZIP64, multidisk or invalid central directory')
    staged = []
    # zipfile verifies local/central names, overlapping compressed entries and CRC during reads.
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        if len(infos) != count or len(infos) > COUNT:
            raise ValueError('Too many ZIP entries')
        seen, prefixes, total = {}, {}, 0
        for info in infos:
            name = valid_path(info.filename)
            at = info.header_offset
            if at < 0 or at+30 > start or blob[at:at+4] != b'PK\x03\x04':
                raise ValueError('Invalid local header')
            flags, method = struct.unpack_from('<HH', blob, at+6)
            nl, el = struct.unpack_from('<HH', blob, at+26)
            data_at = at+30+nl+el
            if flags != info.flag_bits or method != info.compress_type or data_at+info.compress_size > start:
                raise ValueError('Local header mismatch')
            extra_at = at+30+nl
            while extra_at < data_at:
                if extra_at+4 > data_at:
                    raise ValueError('Invalid local extra field')
                tag, length = struct.unpack_from('<HH', blob, extra_at)
                extra_at += 4+length
                if tag == 1 or extra_at > data_at:
                    raise ValueError('ZIP64 or malformed local extra field')
            if not info.flag_bits & 2048 and any(ord(c) > 127 for c in info.filename):
                raise ValueError('Unsupported filename encoding')
            if info.orig_filename != info.filename:
                raise ValueError('NUL in filename')
            directory = info.is_dir()
            key = name.rstrip('/').upper().lower()
            if key in seen:
                raise ValueError('Duplicate archive path')
            seen[key] = directory
            components = name.rstrip('/').split('/')
            for i in range(1, len(components) + 1):
                exact = '/'.join(components[:i])
                folded = exact.upper().lower()
                if folded in prefixes and prefixes[folded] != exact:
                    raise ValueError('Case collision in path prefix')
                prefixes[folded] = exact
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if stat.S_ISLNK(mode) or (info.create_system == 3 and kind not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError('Special node rejected')
            if info.create_system == 3 and ((kind == stat.S_IFDIR and not directory) or (kind == stat.S_IFREG and directory)):
                raise ValueError('Directory type mismatch')
            if info.flag_bits & ~(2048 | 8 | 6) or info.compress_type not in (0, 8):
                raise ValueError('Encryption or unsupported compression')
            p = 0
            while p < len(info.extra):
                if p + 4 > len(info.extra):
                    raise ValueError('Invalid ZIP extra field')
                tag = int.from_bytes(info.extra[p:p+2], 'little')
                length = int.from_bytes(info.extra[p+2:p+4], 'little')
                p += 4 + length
                if tag == 1 or p > len(info.extra):
                    raise ValueError('ZIP64 or malformed extra field')
            total += info.file_size
            if info.file_size > FILE or total > EXPANDED or info.file_size > max(1, info.compress_size) * 1000 or (directory and info.file_size):
                raise ValueError('Expanded ZIP limit exceeded')
        for key in seen:
            parts = key.split('/')
            if any('/'.join(parts[:i]) in seen and not seen['/'.join(parts[:i])] for i in range(1, len(parts))):
                raise ValueError('File/folder collision')
        for info in infos:
            with z.open(info) as stream:
                data = stream.read(FILE + 1)
                if len(data) != info.file_size or stream.read(1):
                    raise ValueError('Expanded file length mismatch')
            staged.append((info, data))
    files = {i.filename for i, _ in staged if not i.is_dir()}
    if m['project_path'] + '/project.pbxproj' not in files or m['scheme_path'] not in files:
        raise ValueError('Selected project or shared scheme missing')
    scheme_bytes = next(data for info, data in staged if info.filename == m['scheme_path'])
    if b'<!DOCTYPE' in scheme_bytes.upper() or b'<!ENTITY' in scheme_bytes.upper():
        raise ValueError('DTD/entities are not permitted in scheme XML')
    if ET.fromstring(scheme_bytes).tag != 'Scheme':
        raise ValueError('Expected Xcode Scheme XML')
    output.mkdir(parents=True, exist_ok=True)
    for info, data in staged:
        dest = output / info.filename
        if info.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            # Preserve only executable bits, never setuid/setgid or broad source permissions.
            dest.chmod(0o755 if (info.external_attr >> 16) & 0o111 else 0o644)
    return m


def build(output, manifest, allow_project_code=False):
    if not allow_project_code:
        raise ValueError('Explicit allow-project-code is required: Xcode projects can execute arbitrary scripts')
    derived = Path(output).resolve().parent / 'original-derived-data'
    if derived.exists():
        raise ValueError('Derived data directory must not exist')
    subprocess.run(['xcodebuild', '-project', str(Path(output).resolve() / manifest['project_path']),
                    '-scheme', manifest['scheme'], '-configuration', 'Release',
                    '-destination', 'generic/platform=iOS', '-derivedDataPath', str(derived),
                    '-disableAutomaticPackageResolution', '-skipPackageUpdates',
                    'CODE_SIGNING_ALLOWED=NO', 'CODE_SIGNING_REQUIRED=NO', 'CODE_SIGN_IDENTITY=', 'build'], check=True)
    products = derived / 'Build/Products/Release-iphoneos'
    apps = sorted(products.glob('*.app'))
    if len(apps) != 1:
        raise ValueError('Expected exactly one application output')
    if any(p.is_symlink() for p in apps[0].rglob('*')) or apps[0].is_symlink():
        raise ValueError('Refusing to package application containing symlinks')
    artifacts = Path(output).resolve().parent / 'artifacts'
    artifacts.mkdir(exist_ok=True)
    shutil.make_archive(str(artifacts / 'unsigned-app'), 'zip', products, apps[0].name)
    payload_root = Path(output).resolve().parent / 'ipa-payload'
    payload = payload_root / 'Payload'
    payload.mkdir(parents=True, exist_ok=False)
    shutil.copytree(apps[0], payload / apps[0].name, symlinks=True)
    archive = shutil.make_archive(str(artifacts / 'unsigned'), 'zip', payload_root)
    Path(archive).rename(artifacts / 'unsigned.ipa')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('snapshot', 'request-id', 'source-sha', 'output'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--allow-project-code', action='store_true')
    args = parser.parse_args()
    if args.build and not args.allow_project_code:
        parser.error('Building requires explicit --allow-project-code')
    m = prepare(args.snapshot, args.request_id, args.source_sha, args.output)
    print(json.dumps({'prepared': True, 'request_id': args.request_id, 'source_sha': args.source_sha, 'project_path': m['project_path'], 'scheme': m['scheme']}))
    if args.build:
        build(args.output, m, args.allow_project_code)

if __name__ == '__main__':
    main()
