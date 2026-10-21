"""Category ownership and file/database rollback on local fixtures only."""
import base64
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from app import engine, fluidd_categories, mod_versions, mods, network, screws
from tests.test_screws import snapshot
from tests import test_storage_batch as storage_tests


PREFERENCES = {"categories": [{"id": "tools", "name": "Tools"}],
               "stored": [{"name": "screws_tilt_calculate", "categoryId": "tools",
                           "visible": False, "alias": "Alignment", "order": 4}], "expanded": [0]}


def installed():
    initial = snapshot()
    initial.fluidd_macros = deepcopy(PREFERENCES)
    prepared = engine.prepare_all(initial, None, screws_desired=True)
    current = snapshot(True, True)
    for path, data in prepared.items():
        if path == mod_versions.TARGET:
            current.versions = engine.MeshSnapshot(data, mods.digest(data),
                {"known": True, "enabled": True, "patchable": True},
                original_hash=mods.digest(b""), original_absent=True)
        else:
            value = engine.file_snapshot(current, path)
            value.data, value.sha256 = data, mods.digest(data)
    current.fluidd_macros = engine.category_change(initial, prepared)["value"]
    return current


class CategoryApplyTests(unittest.TestCase):
    def test_install_records_scoped_undo_and_remove_and_restore_keep_preferences(self):
        for restore in (False, True):
            with self.subTest(restore=restore):
                current = installed()
                record = engine.installed_records(current)[screws.PYTHON]
                self.assertEqual(record["version"], mod_versions.VERSIONS[screws.PYTHON])
                self.assertIn("fluidd_category", record)
                current.fluidd_macros["stored"][0]["color"] = "blue"
                prepared = engine.prepare_all(current, None, restore=True) if restore else engine.prepare_all(
                    current, None, screws_desired=False)
                change = engine.category_change(current, prepared)
                expected = deepcopy(PREFERENCES)
                expected["stored"][0]["color"] = "blue"
                self.assertEqual(change["value"], expected)

    def test_update_of_published_helper_adds_category_and_retains_other_mod_records(self):
        current = installed()
        records = engine.installed_records(current)
        records[screws.PYTHON].pop("fluidd_category")
        records[screws.PYTHON]["version"] = "0.1.16"
        other = "/mnt/UDISK/printer_data/config/custom.cfg"
        records[other] = {"version": "0.1.0", "undo": [{"before": "old", "after": "new"}]}
        data = mod_versions.dump(records)
        current.versions.data, current.versions.sha256 = data, mods.digest(data)
        current.fluidd_macros = deepcopy(PREFERENCES)
        self.assertTrue(engine.update_statuses(current)[screws.PYTHON]["needed"])
        prepared = engine.prepare_all(current, None, screws_desired=True)
        updated = mod_versions.load(prepared[mod_versions.TARGET])
        self.assertEqual(updated[other], records[other])
        self.assertIn("fluidd_category", updated[screws.PYTHON])
        self.assertEqual(engine.category_change(current, prepared)["value"]["stored"][0]["categoryId"], "uncurser")

    def test_existing_record_update_preserves_manual_category_choice_and_original_undo(self):
        current = installed()
        current.fluidd_macros["stored"][0]["categoryId"] = "0"
        prepared = engine.prepare_all(current, None, screws_desired=True)
        self.assertIsNone(engine.category_change(current, prepared))
        updated = mod_versions.load(prepared.get(mod_versions.TARGET, current.versions.data))
        self.assertEqual(updated[screws.PYTHON]["fluidd_category"],
                         engine.installed_records(current)[screws.PYTHON]["fluidd_category"])

    def test_failed_preferences_read_blocks_helper_install_without_making_network_calls(self):
        current = snapshot()
        current.fluidd_error = "Unavailable"
        with self.assertRaisesRegex(ValueError, "Cannot read Fluidd"):
            engine.prepare_all(current, None, screws_desired=True)

    def test_both_reviews_include_database_assignment(self):
        current = snapshot()
        current.fluidd_macros = deepcopy(PREFERENCES)
        combined = engine.difference_all(current, None, screws_desired=True)
        initial, _ = engine.initial_difference(current, screws.MOD_ID)
        for review in (combined, initial):
            self.assertIn("Fluidd macro preferences", review)
            self.assertIn('"name": "UNCURSER"', review)

    def test_database_read_uses_macros_key_and_distinguishes_absence_from_auth_failure(self):
        with patch.object(network, "http", return_value={"value": PREFERENCES}) as http:
            self.assertEqual(network.fluidd_macros("192.0.2.1"), PREFERENCES)
            http.assert_called_once_with("192.0.2.1", "/server/database/item?namespace=fluidd&key=macros")
        for code in (404, 401):
            with patch.object(network, "http", side_effect=HTTPError("unused", code, "missing", {}, None)):
                if code == 404:
                    self.assertIsNone(network.fluidd_macros("192.0.2.1"))
                else:
                    with self.assertRaises(HTTPError):
                        network.fluidd_macros("192.0.2.1")


class Database:
    def __init__(self, state, *, lost_response=False):
        self.state = deepcopy(state)
        self.writes = []
        self.lost_response = lost_response

    def open(self, request, timeout):
        assert request.full_url == "http://127.0.0.1:7125/server/database/item?namespace=fluidd&key=macros"
        method = request.get_method()
        if method == "GET" and self.state is None:
            raise HTTPError(request.full_url, 404, "missing key", {}, None)
        if method != "GET":
            self.state = None if method == "DELETE" else json.loads(request.data)["value"]
            self.writes.append(deepcopy(self.state))
            if self.lost_response:
                self.lost_response = False
                raise TimeoutError("response lost after commit")
        return io.BytesIO(json.dumps({"result": {"value": self.state}}).encode())


class CategoryBatchTests(unittest.TestCase):
    # Reuse the local file-batch harness without enrolling its unrelated tests.
    setUp = storage_tests.StorageBatchTests.setUp
    tearDown = storage_tests.StorageBatchTests.tearDown
    attrs = storage_tests.StorageBatchTests.attrs
    move = storage_tests.StorageBatchTests.move
    normal_request = storage_tests.StorageBatchTests.normal_request
    run_request = storage_tests.StorageBatchTests.run_request
    assert_no_candidates = storage_tests.StorageBatchTests.assert_no_candidates

    def request(self):
        request, path = self.normal_request()
        before = deepcopy(PREFERENCES)
        after, _ = fluidd_categories.enable(before)
        request["fluidd_category"] = {"expected": before, "value": after}
        ledger = self.storage / "Uncurser_mods.json"
        ledger.parent.mkdir(parents=True)
        old = b'{"schema":1,"mods":{}}'
        new = b'{"schema":1,"mods":{"helper":{}}}'
        ledger.write_bytes(old)
        request["files"].append({"path": str(ledger), "kind": "json", "expected": mods.digest(old),
            "backup": str(self.storage / "originals" / "records.jsn.Original.txt"),
            "data": base64.b64encode(new).decode(), "new_hash": mods.digest(new),
            "original_hash": None, "may_capture_original": True, "original_absent": False,
            "reference": "fixture", "restore": False})
        return request, path, ledger

    def test_category_and_files_commit_together_with_ledger_last(self):
        request, path, ledger = self.request()
        database = Database(PREFERENCES)
        native_replace = os.replace
        def replace(source, destination):
            if destination == str(ledger):
                self.assertEqual(database.state, request["fluidd_category"]["value"])
            return native_replace(source, destination)
        with patch("urllib.request.build_opener", return_value=database):
            self.run_request(request, replace=replace)
        self.assertEqual(path.read_bytes(), b"value = 2\n")
        self.assertEqual(database.state, request["fluidd_category"]["value"])
        self.assert_no_candidates()

    def test_ledger_write_failure_rolls_back_category_and_working_file(self):
        request, path, ledger = self.request()
        database = Database(PREFERENCES)
        native_replace = os.replace
        def replace(source, destination):
            if destination == str(ledger):
                raise OSError("ledger write failed")
            return native_replace(source, destination)
        with patch("urllib.request.build_opener", return_value=database):
            with self.assertRaisesRegex(OSError, "ledger write failed"):
                self.run_request(request, replace=replace)
        self.assertEqual(path.read_bytes(), b"value = 1\n")
        self.assertEqual(database.state, PREFERENCES)
        self.assertEqual(ledger.read_bytes(), b'{"schema":1,"mods":{}}')
        self.assert_no_candidates()

    def test_lost_post_response_is_rolled_back_even_when_request_raised(self):
        request, path, _ = self.request()
        database = Database(PREFERENCES, lost_response=True)
        with patch("urllib.request.build_opener", return_value=database):
            with self.assertRaisesRegex(TimeoutError, "response lost"):
                self.run_request(request)
        self.assertEqual(path.read_bytes(), b"value = 1\n")
        self.assertEqual(database.state, PREFERENCES)
        self.assertEqual(len(database.writes), 2)
        self.assert_no_candidates()

    def test_concurrent_preferences_edit_is_preserved_and_aborts_file_batch(self):
        request, path, _ = self.request()
        database = Database(PREFERENCES)
        native_replace = os.replace
        def replace(source, destination):
            if destination == str(path):
                database.state["expanded"] = ["tools"]
            return native_replace(source, destination)
        with patch("urllib.request.build_opener", return_value=database):
            with self.assertRaisesRegex(ValueError, "preferences changed"):
                self.run_request(request, replace=replace)
        self.assertEqual(path.read_bytes(), b"value = 1\n")
        expected = deepcopy(PREFERENCES)
        expected["expanded"] = ["tools"]
        self.assertEqual(database.state, expected)
        self.assertEqual(database.writes, [])
        self.assert_no_candidates()


if __name__ == "__main__":
    unittest.main()
