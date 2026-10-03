"""Collect dependency license texts for embedding in the portable executable."""
import importlib.metadata
import pathlib
import shutil

root = pathlib.Path(__file__).resolve().parents[1]
destination = root / "build" / "licenses"
destination.mkdir(parents=True, exist_ok=True)
for name in ("PySide6-Essentials", "shiboken6", "paramiko", "cryptography", "bcrypt", "PyNaCl", "cffi", "websocket-client", "pycparser"):
    distribution = importlib.metadata.distribution(name)
    target = destination / name
    target.mkdir(exist_ok=True)
    for relative in distribution.files or []:
        if any(word in str(relative).lower() for word in ("license", "copying", "copyright")):
            source = pathlib.Path(distribution.locate_file(relative))
            if source.is_file():
                nested = target / str(relative).replace("/", "_").replace("\\", "_")
                shutil.copyfile(source, nested)
    (target / "package.txt").write_text(f"{name} {distribution.version}\n{distribution.metadata.get('License', '')}\n{distribution.metadata.get('Home-page', '')}\n", encoding="utf-8")
python_license = pathlib.Path(__import__('sys').base_prefix) / "LICENSE.txt"
if python_license.is_file():
    shutil.copyfile(python_license, destination / "Python-LICENSE.txt")
for source in (root / "third_party").iterdir():
    if source.is_file():
        shutil.copyfile(source, destination / source.name)
