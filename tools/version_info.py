"""Generate Windows EXE metadata from the app's version."""
from pathlib import Path
import sys

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable,
    VarFileInfo, VarStruct, VSVersionInfo,
)

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from app import __version__

parts = tuple(int(part) for part in __version__.split("."))
if len(parts) != 3 or any(not 0 <= part <= 65535 for part in parts):
    raise ValueError("App version must contain three numbers between 0 and 65535.")
name = f"Uncurser_v{__version__}"
info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=parts + (0,), prodvers=parts + (0,), fileType=1),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("FileDescription", "Uncurser for K2 Plus"),
            StringStruct("FileVersion", __version__),
            StringStruct("InternalName", "Uncurser"),
            StringStruct("OriginalFilename", name + ".exe"),
            StringStruct("ProductName", "Uncurser"),
            StringStruct("ProductVersion", __version__),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)
output = root / "build" / "version-info.txt"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(str(info), encoding="utf-8")
print(name)
