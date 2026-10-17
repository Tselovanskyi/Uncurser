"""Current-ROM discovery tests use synthetic read-only command responses."""
import base64
import copy
import hashlib
import unittest

from app import fluidd_screws, mesh, mods, screws, stock


INIT = '''copy_config()
{
    case $model in
        "F008")
            if [ "$board" = "CR0CN240110C10" -a "x$structure_version" = "x0" ]; then
                config_dir=F008_CR0CN240319C13
            elif [ "$board" = "CR0CN240319C13" -a "x$structure_version" = "x0" ]; then
                config_dir=F008_CR0CN240319C13
            elif [ "$board" = "CR0CN240319C13" -a "x$structure_version" = "x1" ]; then
                config_dir=F008_CR0CN240319C13_1
            elif [ "$board" = "CR0CN240319C14" -a "x$structure_version" = "x0" ]; then
                config_dir=F008_CR0CN240319C13
            else
                config_dir=
            fi
            gcode_dir=F008
            ;;
    esac
}
'''
STATUS = {"native": {"model": "F008", "modelVersion": "1.1.7.0"}}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(data):
    return {"data": base64.b64encode(data).decode(), "sha256": digest(data)}


def response(structure="0"):
    files = {}
    for path in (*stock._TOOLS, "/etc/init.d/klipper"):
        data = INIT.encode() if path.endswith("/init.d/klipper") else b"identity source\n"
        files[path] = files["/rom" + path] = encoded(data)
    for variant in ("F008_CR0CN240319C13", "F008_CR0CN240319C13_1"):
        for filename in stock._CONFIGS:
            path = "/rom/usr/share/klipper/config/" + variant + "/" + filename
            files[path] = None if filename == "screws_tilt_adjust.cfg" else encoded((variant + "/" + filename).encode())
    for path in stock._COMMON:
        files["/rom" + path] = None if path in (screws.PYTHON, screws.CONFIG) else encoded(b"stock source\n")
    files["/rom" + fluidd_screws.INDEX] = encoded(('src="./' + fluidd_screws.ASSET + '"').encode())
    return {"files": files,
            "hardware": {"model": "F008", "board": "CR0CN240319C13", "structure_version": structure, "pcba_test": "0"},
            "environment": {"version": "1.1.7.0", "root_partition": "rootfsB", "root_device": "179:7"},
            "mountinfo": "21 0 179:7 / /rom ro,relatime - squashfs /dev/root ro\n"
                         "22 0 0:20 / / rw,relatime - overlay overlayfs:/overlay rw,lowerdir=/,upperdir=/overlay/upper\n",
            "cmdline": "root=/dev/mmcblk0p7 init=/init\n", "boot_device": "179:7", "rom_device": "179:7",
            "directories": {"F008_CR0CN240319C13": True, "F008_CR0CN240319C13_1": True}}


class Remote:
    def __init__(self, value):
        self.value, self.calls = value, []

    def python(self, program, payload, **kwargs):
        self.calls.append((program, payload, kwargs))
        return copy.deepcopy(self.value)


class StockTests(unittest.TestCase):
    def test_hardware_selects_each_current_rom_variant_and_all_sources_are_guarded(self):
        for structure, variant in (("0", "F008_CR0CN240319C13"), ("1", "F008_CR0CN240319C13_1")):
            with self.subTest(structure=structure):
                raw = response(structure)
                remote = Remote(raw)
                result = stock.scan(remote, STATUS)
                self.assertTrue(result["available"], result["reason"])
                self.assertEqual(result["identity"]["variant"], variant)
                self.assertIn("/" + variant + "/", result["files"][mesh.TARGET]["source"])
                self.assertEqual(len(remote.calls), 1)
                self.assertEqual(remote.calls[0][0], stock.READ)
                self.assertEqual(result["proof"]["identity"], result["identity"])
                for value in result["files"].values():
                    self.assertIn(value["source"], result["guards"])
                    self.assertEqual(result["guards"][value["source"]], value["sha256"])
                for path in (screws.PYTHON, screws.CONFIG, screws.LEGACY_CONFIG):
                    self.assertTrue(result["files"][path]["absent"])
                    self.assertTrue(result["files"][path]["available"])
                    self.assertIsNone(result["files"][path]["data"])
                self.assertEqual(result["guards"]["/proc/cmdline"], digest(raw["cmdline"].encode()))

    def test_selection_is_data_only_and_rejects_changed_or_unknown_rules(self):
        self.assertEqual(stock.select_variant(INIT.replace("\n", "\r\n"), "CR0CN240319C13", "1"), "F008_CR0CN240319C13_1")
        for init, board, structure in ((INIT, "UNKNOWN", "0"), (INIT, "CR0CN240110C10", "1"),
                                        (INIT.replace(" -a ", " -o "), "CR0CN240319C13", "0"),
                                        (INIT.replace("config_dir=F008_CR0CN240319C13\n", "config_dir=$(wrong)\n"), "CR0CN240319C13", "0")):
            with self.subTest(board=board, structure=structure), self.assertRaises(ValueError):
                stock.select_variant(init, board, structure)
        changed = INIT.replace("config_dir=F008_CR0CN240319C13_1", "config_dir=F008_NEW_VARIANT")
        self.assertEqual(stock.select_variant(changed, "CR0CN240319C13", "1"), "F008_NEW_VARIANT")

    def test_wrong_readonly_mount_or_active_boot_relationship_blocks_all_stock_use(self):
        for field, value in (("mountinfo", response()["mountinfo"].replace("/rom ro", "/rom rw")),
                             ("mountinfo", response()["mountinfo"].replace("squashfs", "ext4")),
                             ("mountinfo", response()["mountinfo"].replace("lowerdir=/,", "lowerdir=/other,")),
                             ("boot_device", "179:6"), ("rom_device", "179:6")):
            with self.subTest(field=field, value=value):
                raw = response()
                raw[field] = value
                result = stock.scan(Remote(raw), STATUS)
                self.assertFalse(result["available"])
                self.assertEqual(result["files"], {})

    def test_firmware_hardware_identity_and_factory_test_mode_must_match(self):
        for group, key, value in (("environment", "version", "1.1.6.4"), ("environment", "root_device", "179:6"),
                                   ("hardware", "model", "F009"), ("hardware", "pcba_test", "1"),
                                   ("hardware", "structure_version", ""), ("hardware", "board", "CR-UNKNOWN")):
            with self.subTest(group=group, key=key):
                raw = response()
                raw[group][key] = value
                self.assertFalse(stock.scan(Remote(raw), STATUS)["available"])

    def test_damaged_hash_or_modified_identity_tool_cannot_supply_stock(self):
        for fault in ("hash", "tool", "directory"):
            with self.subTest(fault=fault):
                raw = response()
                if fault == "hash":
                    raw["files"]["/rom" + mods.TARGET]["sha256"] = "0" * 64
                elif fault == "tool":
                    raw["files"][stock._TOOLS[0]] = encoded(b"changed tool\n")
                else:
                    raw["directories"]["F008_CR0CN240319C13"] = False
                self.assertFalse(stock.scan(Remote(raw), STATUS)["available"])

    def test_missing_or_different_fluidd_does_not_block_unrelated_stock_files(self):
        for fault in ("asset", "index"):
            with self.subTest(fault=fault):
                raw = response()
                raw["files"]["/rom" + (fluidd_screws.TARGET if fault == "asset" else fluidd_screws.INDEX)] = None
                result = stock.scan(Remote(raw), STATUS)
                self.assertTrue(result["available"], result["reason"])
                self.assertTrue(result["files"][mods.TARGET]["available"])
                self.assertFalse(result["files"][fluidd_screws.TARGET]["available"])
                self.assertFalse(result["files"][fluidd_screws.TARGET]["absent"])

    def test_unknown_or_older_printer_identity_is_rejected_without_querying(self):
        for status in ({}, {"native": {"model": "F009", "modelVersion": "1.1.7.0"}},
                        {"native": {"model": "F008", "modelVersion": "1.1.5.0"}}):
            with self.subTest(status=status):
                remote = Remote(response())
                self.assertFalse(stock.scan(remote, status)["available"])
                self.assertEqual(remote.calls, [])


if __name__ == "__main__":
    unittest.main()
