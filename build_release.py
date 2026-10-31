"""Build OKK STUDIO 1.0.8 into a fresh, private output directory.

No network access, source edits, old-output deletion, uploads, or security changes.
Run --check first. Python dependencies and Inno Setup must already be installed.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import uuid
import zipfile

VERSION = '1.0.8'
ROOT = Path(__file__).resolve().parent
REPOSITORY = 'https://github.com/lianduan683/OKK-STUDIO-1.0.0'
TEMPLATES = ('OKK_STUDIO_1.0.8.spec', 'version_info.txt',
             'OKK-STUDIO-1.0.8-Setup.iss', 'installer_info.txt',
             'small_assets.json', 'resource_packages.json', 'build_release.py')
PUBLIC_DOCS = ('README.md', 'INSTALLATION.md', 'CHANGELOG.md', 'BUILDING.md',
               'MCP.md', 'EXPOSURE.md', 'EFFECT_CONTRACT.md', 'THIRD_PARTY_NOTICES.md', 'TIMELINE_IMPORT.md', 'LICENSE')
MCP_SUPPORT = ('mcp-config.json', 'codex-mcp.toml', 'start-editor-mcp.cmd', 'MCP_VALIDATION.json')
FORBIDDEN_TOP = {'work', 'backups', 'projects', 'tests', '.git', '.mimosa', '.zcode',
                 '__pycache__', 'workspace-layout.json', 'bin'}


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def output_path(path):
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT) or path == ROOT:
        raise ValueError(f'Output escaped this build directory: {path}')
    return path


def write_new(path, data):
    path = output_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(data if isinstance(data, bytes) else data.encode('utf-8'))
    return path


def copy_new(source, destination):
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ValueError(f'Expected a regular input file: {source}')
    destination = output_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as incoming, destination.open('xb') as outgoing:
        shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
    return destination


def write_json(path, value):
    return write_new(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def app_version(path):
    tree = ast.parse(Path(path).read_text(encoding='utf-8-sig'), filename=Path(path).name)
    for item in tree.body:
        if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'APP_VERSION'
                                                for t in item.targets):
            return ast.literal_eval(item.value)
    raise ValueError(f'APP_VERSION is missing in {path}')


def checked_inputs(args):
    source = args.source_root.resolve()
    if not source.is_dir():
        raise ValueError(f'Source directory does not exist: {source}')
    modules = sorted(source.glob('*.py'))
    if not 30 <= len(modules) <= 500:
        raise ValueError('Unexpected source module count; select the application source root')
    for name in ('rtx5090_exporter.py', 'studio_project.py'):
        if app_version(source/name) != VERSION:
            raise ValueError(f'{name} must already declare APP_VERSION={VERSION}; source is never rewritten')
    for module in modules:
        if module.is_symlink() or module.stat().st_size > 5*1024*1024:
            raise ValueError(f'Unexpected source module: {module.name}')
        ast.parse(module.read_text(encoding='utf-8-sig'), filename=module.name)
    tests = sorted((source/'tests').glob('*.py'))
    if not tests or len(tests) > 500:
        raise ValueError('Expected the public Python regression suite in source/tests')
    for test in tests:
        if test.is_symlink() or not test.is_file() or test.stat().st_size > 512*1024:
            raise ValueError(f'Unexpected regression source: {test.name}')
        if test.name != '__init__.py' and not test.name.startswith('test_'):
            raise ValueError(f'Only regression test modules are permitted: {test.name}')
        ast.parse(test.read_text(encoding='utf-8-sig'), filename='tests/'+test.name)
    assets = json.loads((ROOT/'small_assets.json').read_text(encoding='utf-8'))
    asset_root = args.old_release/'source'/'assets'
    if not asset_root.is_dir():
        asset_root = source/'assets'
    if len(assets) != 16:
        raise ValueError('Unexpected small asset whitelist; do not copy the full effects library')
    for item in assets:
        name = item['path']
        if Path(name).name != name or Path(name).suffix.lower() not in {'.ico', '.png'}:
            raise ValueError('UI assets must be flat, explicitly approved image files')
        path = asset_root/name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item['size'] or sha256(path) != item['sha256']:
            raise ValueError(f'Approved UI asset does not match: {name}')
        if item['size'] > 1024*1024:
            raise ValueError('A core UI image exceeds the size bound')
    resources = json.loads((ROOT/'resource_packages.json').read_text(encoding='utf-8'))['packages']
    expected_names = {f'OKK-STUDIO-1.0.0-resources-{i:02d}.zip' for i in range(1, 5)}
    if {item['name'] for item in resources} != expected_names or len(resources) != 4:
        raise ValueError('Exactly four frozen 1.0.0 resource packages are required')
    iss = (ROOT/'OKK-STUDIO-1.0.8-Setup.iss').read_text(encoding='utf-8')
    if 'AppId=OKKSTUDIO.1.0.0' not in iss or 'UsePreviousAppDir=yes' not in iss:
        raise ValueError('Upgrade application identity or previous-directory behavior changed')
    if 'core.zip' in iss or 'dist\\OKK STUDIO 1.0.8\\*' not in iss:
        raise ValueError('Setup must embed core files directly, with no core ZIP download')
    for item in resources:
        if item['url'] != REPOSITORY + '/releases/download/v1.0.0/' + item['name']:
            raise ValueError('Unexpected resource origin or version')
        if len(item['sha256']) != 64 or not all(c in '0123456789abcdef' for c in item['sha256']):
            raise ValueError('Malformed resource SHA256')
        if item['url'] not in iss or item['sha256'] not in iss:
            raise ValueError('Installer resource URL/hash disagrees with the frozen manifest')
    for name in (*TEMPLATES, *MCP_SUPPORT):
        if not (ROOT/name).is_file():
            raise ValueError(f'Missing build template: {name}')
    docs = ROOT/'public_repo'
    if not docs.is_dir():
        docs = ROOT/'public_docs'
    for name in PUBLIC_DOCS:
        if not (docs/name).is_file():
            raise ValueError(f'Missing public document: {name}')
    license_path = args.python_license
    if license_path is None:
        options = [args.old_release/'dist'/'OKK STUDIO 1.0.0'/'licenses'/'Python-3.12-LICENSE.txt',
                   Path(sys.base_prefix)/'LICENSE.txt']
        license_path = next((p for p in options if p.is_file()), None)
    if license_path is None or not license_path.is_file():
        raise ValueError('Supply the Python runtime license with --python-license')
    if not args.python.is_file() or not args.iscc.is_file():
        raise ValueError('Python and Inno Setup paths must point to installed executable files')
    return source, modules, tests, assets, asset_root, resources, docs, license_path


def zip_new(path, entries):
    path = output_path(path)
    seen = set()
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source, relative in sorted(entries, key=lambda item: item[1]):
            name = PurePosixPath(relative)
            if name.is_absolute() or '..' in name.parts or name.as_posix() in seen or ':' in name.parts[0]:
                raise ValueError('Unsafe or duplicate archive member')
            if Path(source).is_symlink() or not Path(source).is_file():
                raise ValueError('Archive input must be a regular file')
            seen.add(name.as_posix())
            archive.write(source, name.as_posix())
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad is not None:
            raise ValueError(f'Archive CRC verification failed: {bad}')
    return {'name': path.name, 'size': path.stat().st_size, 'sha256': sha256(path), 'file_count': len(seen)}


def run_logged(command, directory, log_path, environment):
    print('Running ' + Path(command[0]).name + '; log: ' + str(log_path), flush=True)
    with output_path(log_path).open('xb') as log:
        result = subprocess.run(command, cwd=directory, env=environment,
                                stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f'Build step failed ({result.returncode}); inspect {log_path}')


def build(args):
    source, modules, tests, assets, asset_root, resources, docs, license_path = checked_inputs(args)
    print(f'Input check passed: {len(modules)} application modules, {len(tests)} regression modules, {len(assets)} small UI assets, 4 fixed resource packages.', flush=True)
    if args.check:
        print('Check only: no staging, compilation, installation, upload, or security changes.', flush=True)
        return
    build_id = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    run = output_path(ROOT/'builds'/build_id)
    run.mkdir(parents=True, exist_ok=False)
    stage, artifacts = run/'source', run/'artifacts'
    stage.mkdir(); artifacts.mkdir()
    snapshots = []
    for module in modules:
        before = sha256(module)
        copied = copy_new(module, stage/module.name)
        if sha256(copied) != before or sha256(module) != before:
            raise RuntimeError('Source changed while staging; finish edits and start a new build')
        snapshots.append({'path': module.name, 'sha256': before, 'size': copied.stat().st_size})
    test_snapshots = []
    for test in tests:
        before = sha256(test)
        copied = copy_new(test, stage/'tests'/test.name)
        if sha256(copied) != before or sha256(test) != before:
            raise RuntimeError('Regression source changed while staging; finish edits and start a new build')
        test_snapshots.append({'path': 'tests/'+test.name, 'sha256': before, 'size': copied.stat().st_size})
    for name in ('LICENSE', 'requirements-build.txt', 'requirements-mcp.txt'):
        copy_new(source/name, stage/name)
    copy_new(source/'effect_contract_schema.json', stage/'effect_contract_schema.json')
    for item in sorted((source/'effect_contract_examples').rglob('*.json')):
        if item.is_symlink():
            raise ValueError('Effect examples must be regular files')
        copy_new(item, stage/item.relative_to(source))
    for item in assets:
        copy_new(asset_root/item['path'], stage/'assets'/item['path'])
    for name in (*TEMPLATES, *MCP_SUPPORT):
        copy_new(ROOT/name, run/name)
    for name in PUBLIC_DOCS:
        copy_new(docs/name, run/'public_docs'/name)
    environment = os.environ.copy()
    environment['PYINSTALLER_CONFIG_DIR'] = str(run/'pyinstaller-cache')
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    temporary = output_path(run/'temp')
    temporary.mkdir()
    environment['TEMP'] = str(temporary)
    environment['TMP'] = str(temporary)
    command = [str(args.python), '-m', 'PyInstaller', '--noconfirm', '--distpath', str(run/'dist'),
               '--workpath', str(run/'pyinstaller-work'), str(run/'OKK_STUDIO_1.0.8.spec')]
    run_logged(command, run, run/'build.log', environment)
    dist = output_path(run/'dist'/'OKK STUDIO 1.0.8')
    if not (dist/'OKK STUDIO.exe').is_file():
        raise RuntimeError('PyInstaller did not produce the application executable')
    copy_new(stage/'LICENSE', dist/'LICENSE')
    copy_new(license_path, dist/'licenses'/'Python-3.12-LICENSE.txt')
    for name in PUBLIC_DOCS:
        if name != 'LICENSE':
            copy_new(run/'public_docs'/name, dist/name)
    dist_files = [p for p in dist.rglob('*') if p.is_file()]
    total = sum(p.stat().st_size for p in dist_files)
    if total > 200*1024*1024:
        raise RuntimeError('Core exceeds 200 MiB; inspect for accidentally included resources')
    for path in dist_files:
        relative = path.relative_to(dist)
        if path.is_symlink() or relative.parts[0].casefold() in FORBIDDEN_TOP:
            raise RuntimeError(f'Forbidden core content: {relative}')
        if relative.parts[0] == 'assets' and relative.as_posix() not in {'assets/'+item['path'] for item in assets}:
            raise RuntimeError(f'Unexpected asset in core: {relative}')
    core = zip_new(artifacts/'OKK-STUDIO-1.0.8-core.zip', [(p, p.relative_to(dist).as_posix()) for p in dist_files])
    core.update({'uncompressed_size': total, 'embedded_in_setup': True,
                 'url': REPOSITORY+'/releases/download/v1.0.8/'+core['name']})
    run_logged([str(args.iscc), str(run/'OKK-STUDIO-1.0.8-Setup.iss')], run, run/'installer-build.log', environment)
    setup = artifacts/'OKK-STUDIO-1.0.8-Setup.exe'
    if not setup.is_file():
        raise RuntimeError('Inno Setup did not produce the installer')
    packages = {'version': VERSION, 'core_distribution': 'embedded-in-setup', 'core': core,
                'resource_version': '1.0.0', 'resources': resources}
    write_json(run/'packages.json', packages)
    resource_sums = ''.join(item['sha256']+'  '+item['name']+'\n' for item in resources)
    write_new(run/'RESOURCE-SHA256SUMS.txt', resource_sums)
    public = run/'public_repo'
    for name in PUBLIC_DOCS:
        copy_new(run/'public_docs'/name, public/name)
    for name in (*TEMPLATES, *MCP_SUPPORT, 'packages.json', 'RESOURCE-SHA256SUMS.txt'):
        copy_new(run/name, public/name)
    source_entries = [(p, 'source/'+p.relative_to(stage).as_posix()) for p in stage.rglob('*') if p.is_file()]
    source_entries += [(public/name, name) for name in (*PUBLIC_DOCS, *TEMPLATES, *MCP_SUPPORT, 'packages.json', 'RESOURCE-SHA256SUMS.txt')]
    source_entries += [(run/'public_docs'/name, 'public_docs/'+name) for name in PUBLIC_DOCS]
    source_zip = zip_new(artifacts/'OKK-STUDIO-1.0.8-source.zip', source_entries)
    new_assets = [setup, artifacts/core['name'], artifacts/source_zip['name']]
    sums = ''.join(sha256(path)+'  '+path.name+'\n' for path in new_assets)
    write_new(artifacts/'SHA256SUMS.txt', sums)
    write_new(artifacts/'RESOURCE-SHA256SUMS.txt', resource_sums)
    report = {'version': VERSION, 'build_id': build_id, 'build_directory': str(run),
              'artifacts': [{'path': str(p), 'size': p.stat().st_size, 'sha256': sha256(p)} for p in new_assets],
              'core_uncompressed_bytes': total, 'core_files': len(dist_files), 'source_modules': snapshots,
              'regression_modules': test_snapshots,
              'source_zip_files': source_zip['file_count'], 'resource_download_bytes': sum(p['size'] for p in resources),
              'signed': False, 'installation_tested_by_this_script': False,
              'original_sources_modified': False, 'security_settings_modified': False}
    write_json(run/'build_report.json', report)
    print(json.dumps({'build_directory': str(run), 'artifacts': str(artifacts), 'public_repo': str(public),
                      'report': str(run/'build_report.json')}, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default_source = ROOT/'source' if (ROOT/'source').is_dir() else Path.home()/'Desktop'/'OKK STUDIO'
    parser.add_argument('--source-root', type=Path, default=default_source)
    parser.add_argument('--old-release', type=Path, default=ROOT.parent/'installer_1.0.0')
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--python-license', type=Path)
    compiler_candidates = [ROOT/'tools'/'InnoSetup'/'ISCC.exe',
                           Path(os.environ.get('LOCALAPPDATA', ''))/'Programs'/'Inno Setup 7'/'ISCC.exe',
                           Path(os.environ.get('ProgramFiles', 'C:/Program Files'))/'Inno Setup 7'/'ISCC.exe',
                           Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)'))/'Inno Setup 7'/'ISCC.exe']
    compiler = next((p for p in compiler_candidates if p.is_file()),
                    Path(shutil.which('ISCC.exe') or compiler_candidates[0]))
    parser.add_argument('--iscc', type=Path, default=compiler)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    args.python = args.python.resolve(); args.iscc = args.iscc.resolve()
    args.old_release = args.old_release.resolve()
    if args.python_license is not None: args.python_license = args.python_license.resolve()
    build(args)


if __name__ == '__main__':
    main()
