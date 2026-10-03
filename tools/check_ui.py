"""One local UI check with synthetic file contents; no printer connection."""
import pathlib
import sys
import time
import json
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

root = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QEnterEvent, QFontInfo
from PySide6.QtNetwork import QHostAddress, QNetworkInterface
from PySide6.QtTest import QTest
from app.engine import Snapshot, MeshSnapshot, difference_all
from app import mesh
from app.mods import transform, digest
from app.ui import Window
from app.appearance import configure

data = root / "build" / "ui-check"
data.mkdir(parents=True, exist_ok=True)
app = QApplication([])
configure(app)
assert QFontInfo(app.font()).family() == "Google Sans"
title_bar_results = []
if sys.platform == "win32":
    import ctypes
    native_set = app.title_bars.set_attribute
    def record_title_bar(handle, attribute, pointer, size):
        result = native_set(handle, attribute, pointer, size)
        color = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value
        title_bar_results.append((handle, attribute, color, result))
        return result
    app.title_bars.set_attribute = record_title_bar
temporary = tempfile.TemporaryDirectory(dir=data)
settings_dir = pathlib.Path(temporary.name)
assert settings_dir.resolve().is_relative_to(data.resolve())
(settings_dir / "Uncurser_settings").write_text(json.dumps({"host": "192.168.50.130", "username": "root"}))
window = Window(settings_dir, start_polling=False)
window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
window.show()
fixture = (root / "tests" / "test_mods.py").read_text(encoding="utf-8")
import ast
source = next(ast.literal_eval(node.value) for node in ast.parse(fixture).body
              if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "SOURCE" for target in node.targets))
mesh_fixture = (root / "tests" / "test_mesh.py").read_text(encoding="utf-8")
mesh_source = next(ast.literal_eval(node.value) for node in ast.parse(mesh_fixture).body
                   if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "SOURCE" for target in node.targets))
def snapshot(content, enabled):
    return Snapshot(content, digest(content), {"known": True, "enabled": enabled, "patchable": True, "label": "Uncursed" if enabled else "Original", "reason": ""},
                    None, None, "", {"info": {"hostname": "Preview printer"},
                                     "native": {"model": "F008", "modelVersion": "printer hw ver:;printer sw ver:;DWIN hw ver:CR0CN240110C10;DWIN sw ver:1.1.6.4;"}}, "", {}, time.time(),
                    MeshSnapshot(mesh_source, digest(mesh_source), {"known": True, "enabled": False, "patchable": True, "label": "Original", "reason": ""}))
assert not window.mod_card.isEnabled() and window.pending_label.isHidden()
assert not window.heat_details.isEnabled() and not window.mesh_details.isEnabled()
assert window.scan_card.isHidden()
first = window.printer_rows[0]
assert first.password.text() == "creality_2024"
first.name.setText("Workshop printer")
first.password.setText("synthetic-password")
# Adding/removing a draft must not stretch or move the existing printer row.
app.processEvents()
first_top = first.host.mapTo(window, QPoint()).y()
assert window.add_button.text() == "Add"
window.add_button.click()
draft = window.printer_rows[-1]
assert first.host.mapTo(window, QPoint()).y() == first_top
app.processEvents()
assert first.host.mapTo(window, QPoint()).y() == first_top
window.remove_printer(draft, empty=True)
app.processEvents()
assert first.host.mapTo(window, QPoint()).y() == first_top
second = window.add_printer({"host": "192.168.50.131", "username": "root", "password": "second-synthetic-password"})
window.save_settings()
reloaded = Window(settings_dir, start_polling=False)
assert len(reloaded.printer_rows) == 2
assert reloaded.printer_rows[0].values() == first.values()
assert reloaded.printer_rows[1].values() == second.values()
assert first.password.echoMode() == QLineEdit.EchoMode.Normal
reloaded.close()
window.receive_reachability({"192.168.50.130": True, "192.168.50.131": False})
assert not first.connect_button.isHidden() and second.connect_button.isHidden()
window.run_task = lambda work, done, **options: done(work(lambda message: None))
with patch("app.ui.engine.scan", return_value=snapshot(source, False)):
    window.connect_printer(first)
connection = first.connection()
assert window.mod_card.isEnabled() and window.connected
assert window.heat_details.isEnabled() and window.mesh_details.isEnabled()
assert not window.preview_button.isEnabled()
# Initial-enable previews use the saved original even when the working file
# already has the mod, and never depend on the staged toggle position.
original_mesh = mesh_source.replace(b'speed: 100\n', b'speed: 125\n')
window.snapshot.mesh.original = original_mesh
window.snapshot.mesh.data = mesh.transform(original_mesh, True)
with patch.object(window, "show_diff") as shown:
    window.mesh_details.click()
    initial_preview = shown.call_args.args
    assert initial_preview[1] == "Baseline: on-printer original backup."
    assert "-speed: 125" in initial_preview[2] and "+speed: 700" in initial_preview[2]
    window.mesh_toggle.click()
    window.mesh_details.click()
    assert shown.call_args.args == initial_preview
    window.cancel_staged()
    window.heat_details.click()
    assert "no original backup exists yet" in shown.call_args.args[1]
window.snapshot.mesh.original = None
window.snapshot.mesh.data = mesh_source
assert not window.scan_card.isHidden() and first.connect_button.text() == "Connected"
assert first.connect_button.objectName() == "connected" and first.availability.isHidden()
app.sendEvent(first.connect_button, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
assert first.connect_button.text() == "Disconnect"
app.sendEvent(first.connect_button, QEvent(QEvent.Type.Leave))
assert first.connect_button.text() == "Connected"
assert first.name.text() == "Workshop printer"
window.toggle.click()
assert window.staged is True and window.apply_button.isEnabled() and not window.pending_label.isHidden()
QTest.qWait(25)
mesh_toggle_position = window.mesh_toggle.mapTo(window, QPoint())
window.mesh_toggle.click()
QTest.qWait(25)
assert window.mesh_staged is True and not window.mesh_pending.isHidden()
assert window.mesh_toggle.mapTo(window, QPoint()) == mesh_toggle_position
window.receive_scan(snapshot(source, False), connection)
assert window.staged is True and window.toggle.isChecked()
assert window.mesh_staged is True and window.mesh_toggle.isChecked()
second.reachable = True
with patch("app.ui.engine.scan", return_value=snapshot(source, False)):
    window.connect_printer(second)
    assert second.name.text() == "Preview printer"
    assert window.staged is None and not window.toggle.isChecked()
    window.connect_printer(first)
    assert window.staged is True and window.toggle.isChecked()
    assert window.mesh_staged is True and window.mesh_toggle.isChecked()
window.cancel_staged()
assert window.staged is None and not window.toggle.isChecked() and not window.apply_button.isEnabled()
assert window.pending_label.isHidden()
assert window.mesh_staged is None and not window.mesh_toggle.isChecked() and window.mesh_pending.isHidden()
window.mesh_toggle.click()
diff = difference_all(window.snapshot, window.staged, window.mesh_staged)
assert "printer.cfg" in diff and "pause_resume.py" not in diff and "+lift_speed: 50" in diff
window.cancel_staged()
unknown_heat = snapshot(source, False)
unknown_heat.analysis["known"] = False
window.receive_scan(unknown_heat, connection, review=False)
assert not window.toggle.isEnabled() and window.mesh_toggle.isEnabled()
window.receive_scan(snapshot(source, False), connection, review=False)
window.receive_scan(snapshot(transform(source, True), True), connection)
assert window.toggle.applied and window.toggle.isChecked()
with patch.object(window, "show_diff") as shown:
    window.heat_details.click()
    assert "original backup is missing" in shown.call_args.args[2]
window.toggle.click()
assert window.staged is False and window.toggle.applied
window.receive_reachability({"192.168.50.130": False, "192.168.50.131": True})
assert not window.connected and not window.mod_card.isEnabled() and not window.apply_button.isEnabled()
app.processEvents()
window.grab().save(str(data / "disconnected.png"))
first.reachable = True
with patch("app.ui.engine.scan", return_value=snapshot(transform(source, True), True)):
    window.connect_printer(first)
assert window.staged is False and not window.pending_label.isHidden()
first.name.setText("Workshop K2")
first.name.textEdited.emit(first.name.text())
assert window.connected and window.staged is False and window.apply_button.isEnabled()
assert window.scan_button.toolTip().startswith("Workshop K2  ·  ")
app.processEvents()
window.grab().save(str(data / "interface.png"))
second.remove_button.click()
assert window.printer_rows == [first] and window.connected and window.staged is False
assert json.loads((settings_dir / "Uncurser_settings").read_text())["printers"] == [first.values()]
first.remove_button.click()
assert not window.printer_rows and not window.connected and window.active_row is None
assert window.staged is None and not window.mod_card.isEnabled() and not window.apply_button.isEnabled()
assert json.loads((settings_dir / "Uncurser_settings").read_text())["printers"] == []

# Draft rows stay unique, do not enter settings, and disappear when abandoned.
window.add_manual_printer()
draft = window.printer_rows[0]
window.add_manual_printer()
assert window.printer_rows == [draft]
window.save_settings()
assert json.loads((settings_dir / "Uncurser_settings").read_text())["printers"] == []
assert window.find_button.objectName() == "primary"
window.find_button.setFocus()
window.discard_empty_printers()
assert not window.printer_rows

# Hold the discovery worker open while connecting and staging a found printer.
release_search = threading.Event()
searched = []
def discover(networks, progress, on_found, stop):
    searched.extend(networks)
    on_found(("192.168.50.132", "Discovered printer"))
    progress("Searching another connection")
    if not release_search.wait(3):
        raise TimeoutError("The UI test did not release discovery")
    return [("192.168.50.132", "Discovered printer")]

assert not hasattr(window, "message")
window.apply_button.setToolTip("Saved write status")
def interface(address):
    flags = QNetworkInterface.InterfaceFlag.IsUp | QNetworkInterface.InterfaceFlag.IsRunning
    entry = SimpleNamespace(ip=lambda: QHostAddress(address), prefixLength=lambda: 24)
    return SimpleNamespace(flags=lambda: flags, addressEntries=lambda: [entry])

with (patch("app.ui.network.discover", side_effect=discover),
      patch("app.ui.QNetworkInterface.allInterfaces", return_value=[interface("192.168.50.10"), interface("192.168.60.10")])):
    window.find_printers()
    try:
        deadline = time.monotonic() + 2
        while not window.printer_rows and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert set(searched) == {"192.168.50.0/24", "192.168.60.0/24"} and window.printer_rows
        candidate = window.printer_rows[0]
        assert candidate.password.text() == "creality_2024"
        assert candidate.name.text() == "Discovered printer"
        assert window.discovery_task is not None and not window.busy
        assert window.find_button.running and not window.apply_button.running
        assert window.apply_button.toolTip() == "Saved write status"
        candidate.reachable = True
        with patch("app.ui.engine.scan", return_value=snapshot(source, False)):
            window.connect_printer(candidate)
        window.toggle.click()
        assert window.connected and window.staged is True and window.apply_button.isEnabled()
        candidate.name.setText("My K2 Plus")
        candidate.name.textEdited.emit(candidate.name.text())
        candidate.password.setText("saved-custom-password")
        window.save_settings()
        saved_values = candidate.values()
        window.receive_candidate((candidate.host.text(), "Different discovered name"))
        assert window.printer_rows == [candidate] and candidate.values() == saved_values
        assert window.connected and window.staged is True
        assert json.loads((settings_dir / "Uncurser_settings").read_text())["printers"] == [saved_values]
        window.apply_button.setToolTip("Saved write status")
        app.processEvents()
        window.grab().save(str(data / "searching.png"))
    finally:
        release_search.set()
    deadline = time.monotonic() + 2
    while window.discovery_task is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
assert window.discovery_task is None and not window.find_button.running
assert window.staged is True and window.apply_button.toolTip() == "Saved write status"

# Exercise the actual task UI without reading/writing any printer. Scan status
# belongs to the scan button; write details use Apply's tooltip, without a label row.
immediate_task = window.run_task
window.run_task = Window.run_task.__get__(window, Window)
with patch.object(window.pool, "start") as start, patch("app.ui.threading.Thread") as scan_thread:
    window.scan_clicked()
    scan_task = scan_thread.call_args.kwargs["target"].__self__
    assert window.scanning and window.scan_button.running and not window.scan_card.isHidden()
    assert not window.apply_button.running and window.apply_button.toolTip() == "Saved write status"
    scan_task.signals.progress.emit("Reading current printer files…")
    assert window.scan_button.toolTip() == "Reading current printer files…"
    assert window.apply_button.toolTip() == "Saved write status"
    app.processEvents()
    window.grab().save(str(data / "scanning.png"))
    scan_task.signals.done.emit(snapshot(source, False))
    assert not window.scanning and not window.scan_button.running
    assert window.staged is True and window.apply_button.toolTip() == "Saved write status"
    window.scan_clicked()
    with patch.object(QMessageBox, "warning"):
        scan_thread.call_args.kwargs["target"].__self__.signals.error.emit(RuntimeError("Example scan failure"))
    assert window.scan_button.text() == "Scan failed" and window.apply_button.toolTip() == "Saved write status"
    window.run_task(lambda progress: None, lambda result: window.apply_button.setToolTip("Saved and verified."))
    write_task = start.call_args.args[0]
    assert not window.scanning and not window.scan_button.running and window.apply_button.running
    assert window.apply_button.text() == "Applying…" and not window.apply_button.isEnabled()
    write_task.signals.progress.emit("Verifying saved printer files…")
    assert window.apply_button.toolTip() == "Verifying saved printer files…"
    app.processEvents()
    window.grab().save(str(data / "applying.png"))
    write_task.signals.done.emit(None)
    assert not window.apply_button.running and window.apply_button.text() == "Apply"
    assert window.apply_button.toolTip() == "Saved and verified."
    window.run_task(lambda progress: None, lambda result: None)
    with patch.object(QMessageBox, "warning"):
        start.call_args.args[0].signals.error.emit(RuntimeError("Example apply failure"))
    assert not window.apply_button.running and not window.apply_button.timer.isActive()
    assert window.apply_button.text() == "Apply" and window.apply_button.toolTip() == "Example apply failure"
window.run_task = immediate_task
if sys.platform == "win32":
    # These DWM colors are set-only attributes; verify Windows accepted them.
    for attribute, expected in ((35, 0), (36, 0)):
        assert (int(window.winId()), attribute, expected, 0) in title_bar_results
    assert window.windowTitle() == "Uncurser · K2 Plus"
    assert not window.windowFlags() & Qt.WindowType.FramelessWindowHint
window.cancel_staged()
candidate.connect_button.click()
assert not window.connected and window.scan_card.isHidden() and not window.mod_card.isEnabled()
assert candidate.connect_button.text() == "Connect"
# SSH supplies a blank manual entry's name even if Moonraker status is unavailable.
manual = window.add_printer({"host": "192.168.50.133"})
manual.reachable = True
ssh_snapshot = snapshot(source, False)
ssh_snapshot.status = None
ssh_snapshot.identity = {"hostname": "SSH printer"}
with patch("app.ui.engine.scan", return_value=ssh_snapshot):
    window.connect_printer(manual)
assert manual.name.text() == "SSH printer"
reloaded = Window(settings_dir, start_polling=False)
assert reloaded.printer_rows[0].values() == saved_values
assert reloaded.printer_rows[1].name.text() == "SSH printer"
reloaded.close()
window.close()
temporary.cleanup()
print("Offline UI checks passed, including editable names, persistence, discovery preservation and SSH name fallback. Screenshots saved in build/ui-check.")
