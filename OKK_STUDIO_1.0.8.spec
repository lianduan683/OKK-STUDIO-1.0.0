from pathlib import Path
from PyInstaller.utils.hooks import collect_all, copy_metadata
build = Path(SPECPATH)
root = build / 'source'
dnd_data, dnd_binaries, dnd_hidden = collect_all('tkinterdnd2')
numpy_metadata = copy_metadata('numpy')
otio_data, otio_binaries, otio_hidden = collect_all('opentimelineio')
aaf_data, aaf_binaries, aaf_hidden = collect_all('otio_aaf_adapter')
aaf2_data, aaf2_binaries, aaf2_hidden = collect_all('aaf2')
timeline_metadata = copy_metadata('otio-aaf-adapter')+copy_metadata('opentimelineio')+copy_metadata('pyaaf2')
import json
asset_names = [item['path'] for item in json.loads((build/'small_assets.json').read_text(encoding='utf-8'))]
ui_data = [(str(root/'assets'/name), 'assets') for name in asset_names]
a = Analysis([str(root/'rtx5090_exporter.py')], pathex=[str(root)],
    binaries=dnd_binaries+otio_binaries+aaf_binaries+aaf2_binaries, datas=ui_data+dnd_data+numpy_metadata+otio_data+aaf_data+aaf2_data+timeline_metadata,
    hiddenimports=['okk_cli', *otio_hidden, *aaf_hidden, *aaf2_hidden, *[m for m in dnd_hidden if '.__pyinstaller' not in m]],
    hookspath=[], runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='OKK STUDIO',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, contents_directory='.', icon=str(root/'assets/app_icon.ico'),
    version=str(build/'version_info.txt'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='OKK STUDIO 1.0.8')
