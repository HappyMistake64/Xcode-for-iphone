#!/usr/bin/env python3
"""Approved original-project simulator build/launch capture; not interactive streaming."""
import argparse
import json
from pathlib import Path
import plistlib
import re
import subprocess
import time
import uuid
from build_original import prepare


def command(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=kwargs.pop('timeout', 120), **kwargs)


def choose(inventory, runtime_id=None, device_type_id=None):
    if bool(runtime_id) != bool(device_type_id):
        raise ValueError('Select both runtime_id and device_type_id, or neither')
    if runtime_id and (not re.fullmatch(r'com\.apple\.CoreSimulator\.SimRuntime\.iOS-[0-9-]+', runtime_id) or not re.fullmatch(r'com\.apple\.CoreSimulator\.SimDeviceType\.[A-Za-z0-9-]+', device_type_id)):
        raise ValueError('Invalid simulator identifier')
    runtimes = [r for r in inventory.get('runtimes', []) if r.get('isAvailable') is True and
                isinstance(r.get('identifier'), str) and re.fullmatch(r'com\.apple\.CoreSimulator\.SimRuntime\.iOS-[0-9-]+', r['identifier']) and re.fullmatch(r'\d+(?:\.\d+)*', r.get('version', ''))]
    types = [d for d in inventory.get('devicetypes', []) if d.get('productFamily') == 'iPhone' and isinstance(d.get('identifier'), str) and re.fullmatch(r'com\.apple\.CoreSimulator\.SimDeviceType\.[A-Za-z0-9-]+', d['identifier'])]
    if runtime_id:
        runtimes = [r for r in runtimes if r['identifier'] == runtime_id]
        types = [d for d in types if d['identifier'] == device_type_id]
    if not runtimes or not types:
        raise ValueError('Requested available iOS runtime/iPhone device type not found')
    runtime = max(runtimes, key=lambda r: tuple(int(n) for n in r['version'].split('.')))
    supported = runtime.get('supportedDeviceTypes')
    if isinstance(supported, list):
        ids = {d.get('identifier') for d in supported if isinstance(d, dict)}
        types = [d for d in types if d['identifier'] in ids]
    if not types:
        raise ValueError('Runtime does not support selected iPhone device type')
    device = sorted(types, key=lambda d: d['identifier'])[-1]
    return runtime['identifier'], device['identifier']


def run(prepared, manifest, artifacts, request_id, allow_project_code=False, runtime_id=None, device_type_id=None):
    if not allow_project_code:
        raise ValueError('Explicit approval for project execution and simulator launch required')
    if str(uuid.UUID(request_id)) != request_id:
        raise ValueError('Invalid request UUID')
    prepared, artifacts = Path(prepared).resolve(), Path(artifacts).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    derived = prepared.parent / ('simulator-derived-' + request_id)
    if derived.exists():
        raise ValueError('Fresh simulator derived-data directory required')
    inventory = json.loads(command(['xcrun', 'simctl', 'list', '--json']).stdout)
    runtime, device_type = choose(inventory, runtime_id, device_type_id)
    device = None
    bundle = None
    cleanup_errors = []
    try:
        created = command(['xcrun', 'simctl', 'create', 'Pocket-' + request_id, device_type, runtime]).stdout.strip()
        uuid.UUID(created)  # Validate, but preserve simctl casing for xcodebuild destination.
        device = created
        # UUID is validated; its original representation is passed as an argument.
        command(['xcrun', 'simctl', 'boot', device])
        command(['xcrun', 'simctl', 'bootstatus', device, '-b'], timeout=240)
        args = ['xcodebuild', '-project', str(prepared / manifest['project_path']), '-scheme', manifest['scheme'],
                '-configuration', 'Debug', '-sdk', 'iphonesimulator', '-destination', 'id=' + device,
                '-derivedDataPath', str(derived), '-disableAutomaticPackageResolution', '-skipPackageUpdates',
                'CODE_SIGNING_ALLOWED=NO', 'CODE_SIGNING_REQUIRED=NO', 'CODE_SIGN_IDENTITY=', 'build']
        with (artifacts / 'build.log').open('w') as log:
            subprocess.run(args, check=True, text=True, stdout=log, stderr=subprocess.STDOUT, timeout=900)
        products = derived / 'Build/Products/Debug-iphonesimulator'
        apps = list(products.glob('*.app'))
        if len(apps) != 1 or apps[0].is_symlink():
            raise ValueError('Expected one real simulator application')
        app = apps[0]
        if any(p.is_symlink() for p in app.rglob('*')):
            raise ValueError('Simulator app symlinks are not supported by this bounded runner')
        with (app / 'Info.plist').open('rb') as f:
            info = plistlib.load(f)
        bundle = info.get('CFBundleIdentifier')
        if not isinstance(bundle, str) or not re.fullmatch(r'[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', bundle):
            raise ValueError('Invalid built application bundle identifier')
        command(['xcrun', 'simctl', 'install', device, str(app)])
        launch = command(['xcrun', 'simctl', 'launch', '--terminate-running-process', device, bundle])
        (artifacts / 'launch.log').write_text(launch.stdout + launch.stderr)
        # A short settling delay is not evidence of app health or full interaction coverage.
        time.sleep(3)
        command(['xcrun', 'simctl', 'io', device, 'screenshot', str(artifacts / 'screenshot.png')])
        (artifacts / 'run.json').write_text(json.dumps({'request_id': request_id, 'device_uuid': device,
            'runtime_id': runtime, 'device_type_id': device_type, 'bundle_id': bundle,
            'capture': 'single screenshot after launch; not interactive streaming'}, indent=2))
    finally:
        if device:
            actions = ([['terminate', device, bundle]] if bundle else []) + [['shutdown', device], ['delete', device]]
            for action in actions:
                try:
                    command(['xcrun', 'simctl'] + action)
                except Exception as error:
                    cleanup_errors.append(type(error).__name__ + ': ' + action[0])
        (artifacts / 'cleanup.json').write_text(json.dumps({'device_uuid': device, 'errors': cleanup_errors}))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('snapshot', 'request-id', 'source-sha', 'output', 'artifacts'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--runtime-id')
    p.add_argument('--device-type-id')
    p.add_argument('--allow-project-code', action='store_true')
    a = p.parse_args()
    if not a.allow_project_code:
        p.error('Project execution approval is required')
    m = prepare(Path(a.snapshot), a.request_id, a.source_sha, Path(a.output))
    run(a.output, m, a.artifacts, a.request_id, True, a.runtime_id, a.device_type_id)

if __name__ == '__main__':
    main()
