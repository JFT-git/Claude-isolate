# Two entry points share one runtime folder. A console subsystem is necessary
# for the binary stdin/stdout socket used by QEMU's guestfwd worker.
from pathlib import Path
root = Path(SPECPATH).parent
core = Analysis([str(root / 'windows/core.py')], pathex=[str(root)],
                datas=[(str(root / 'guest'), 'guest')] + [(str(p), '.') for p in root.glob('guest-image-*.json')], hiddenimports=['pycdlib'])
gui = Analysis([str(root / 'windows/app.py')], pathex=[str(root)],
               hiddenimports=['PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets'])
core_exe = EXE(PYZ(core.pure), core.scripts, [], exclude_binaries=True,
               name='Claude Isolate Core', console=True, upx=False,
               version=str(root / 'windows/version.txt'))
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True,
              name='Claude Isolate', console=False, upx=False,
              version=str(root / 'windows/version.txt'))
COLLECT(gui_exe, core_exe, core.binaries, core.datas, gui.binaries, gui.datas,
        strip=False, upx=False, name='Claude Isolate')
