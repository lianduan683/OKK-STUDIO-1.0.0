from pathlib import Path
from PyInstaller.utils.hooks import collect_all, copy_metadata
build = Path(SPECPATH)
root = build / 'source'
dnd_data, dnd_binaries, dnd_hidden = collect_all('tkinterdnd2')
numpy_metadata = copy_metadata('numpy')
import json
asset_names = [item['path'] for item in json.loads((build/'small_assets.json').read_text(encoding='utf-8'))]
ui_data = [(str(root/'assets'/name), 'assets') for name in asset_names]
a = Analysis([str(root/'rtx5090_exporter.py')], pathex=[str(root)],
    binaries=dnd_binaries, datas=ui_data+dnd_data+numpy_metadata, hiddenimports=['okk_cli', *[m for m in dnd_hidden if '.__pyinstaller' not in m]],
    hookspath=[], runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='OKK STUDIO',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, contents_directory='.', icon=str(root/'assets/app_icon.ico'),
    version=str(build/'version_info.txt'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='OKK STUDIO 1.0.2')
