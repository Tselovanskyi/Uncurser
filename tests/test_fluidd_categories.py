import unittest
from copy import deepcopy

from app import fluidd_categories as categories


class FluiddCategoriesTests(unittest.TestCase):
    def test_preserves_categories_and_every_other_macro_preference(self):
        state = {"categories": [{"id": "tools", "name": "Tools"}, {"id": "u", "name": "UNCURSER"}],
                 "stored": [{"name": "bed_screw_helper", "categoryId": "tools", "alias": "Screws",
                             "visible": False, "color": "red", "order": 7, "disabledWhilePrinting": True},
                            {"name": "other", "categoryId": "tools"}], "expanded": ["tools"]}
        before = deepcopy(state)
        enabled, record = categories.enable(state)
        self.assertEqual(state, before)
        expected = deepcopy(before)
        expected["stored"][0]["categoryId"] = "u"
        self.assertEqual(enabled, expected)
        self.assertEqual(categories.disable(enabled, record), before)

    def test_fresh_settings_roundtrip_and_deterministic_ids(self):
        enabled, record = categories.enable(None)
        self.assertEqual(categories.enable(None), (enabled, record))
        self.assertEqual(enabled["stored"], [{"name": "bed_screw_helper", "categoryId": "uncurser"}])
        self.assertIsNone(categories.disable(enabled, record))
        state = {"categories": [{"id": "uncurser", "name": "Other"}]}
        enabled, record = categories.enable(state)
        self.assertEqual(record["category"]["id"], "uncurser-2")
        self.assertEqual(categories.disable(enabled, record), state)

    def test_update_and_uninstall_preserve_later_preferences_and_shared_category(self):
        enabled, record = categories.enable(None)
        enabled["stored"][0]["alias"] = "My screws"
        enabled["stored"].append({"name": "my_other_macro", "categoryId": "uncurser"})
        self.assertEqual(categories.enable(enabled, record), (enabled, record))
        disabled = categories.disable(enabled, record)
        self.assertEqual(disabled["stored"][0], {"name": "bed_screw_helper", "alias": "My screws"})
        self.assertEqual(disabled["categories"], enabled["categories"])
        enabled["stored"][0]["categoryId"] = "0"
        self.assertEqual(categories.enable(enabled, record), (enabled, record))
        self.assertEqual(categories.disable(enabled, record), enabled)

    def test_uninstall_preserves_renamed_category_and_new_root_settings(self):
        enabled, record = categories.enable(None)
        enabled["categories"][0]["name"] = "Mine"
        enabled["expanded"] = ["uncurser"]
        disabled = categories.disable(enabled, record)
        self.assertEqual(disabled, {"categories": [{"id": "uncurser", "name": "Mine"}], "expanded": ["uncurser"]})

    def test_previous_deleted_category_falls_back_to_uncategorized(self):
        state = {"categories": [{"id": "old", "name": "Old"}],
                 "stored": [{"name": "bed_screw_helper", "categoryId": "old"}]}
        enabled, record = categories.enable(state)
        enabled["categories"].pop(0)
        self.assertEqual(categories.disable(enabled, record)["stored"][0]["categoryId"], "0")

    def test_legacy_record_migrates_membership_preserving_preferences_and_shared_category(self):
        legacy = {"category": {"id": "uncurser", "name": "UNCURSER"},
                  "created_category": True, "created_macro": False,
                  "previous_category": {"present": True, "value": "tools"},
                  "missing": {"root": False, "categories": False, "stored": False}}
        state = {"categories": [{"id": "tools", "name": "Tools"}, legacy["category"].copy()],
                 "stored": [{"name": "screws_tilt_calculate", "categoryId": "uncurser", "alias": "Old",
                             "visible": False, "color": "red", "order": 2},
                            {"name": "bed_screw_helper", "categoryId": "tools", "alias": "My helper"},
                            {"name": "another_helper", "categoryId": "uncurser"}], "expanded": ["uncurser"]}
        before = deepcopy(state)
        categories.validate_record(legacy)
        self.assertEqual(categories.record_macro(legacy), "screws_tilt_calculate")
        migrated, record = categories.enable(state, legacy)
        expected = deepcopy(state)
        expected["stored"][0]["categoryId"] = "tools"
        expected["stored"][1]["categoryId"] = "uncurser"
        self.assertEqual(migrated, expected)
        self.assertEqual(state, before)
        self.assertEqual(record["macro"], "bed_screw_helper")
        self.assertFalse(record["created_category"])
        expected["stored"][1]["categoryId"] = "tools"
        self.assertEqual(categories.disable(migrated, record), expected)

    def test_legacy_uninstall_only_removes_its_recorded_macro_assignment(self):
        legacy = {"category": {"id": "uncurser", "name": "UNCURSER"},
                  "created_category": True, "created_macro": True,
                  "previous_category": {"present": False},
                  "missing": {"root": True, "categories": True, "stored": True}}
        state = {"categories": [legacy["category"].copy()],
                 "stored": [{"name": "screws_tilt_calculate", "categoryId": "uncurser"},
                            {"name": "bed_screw_helper", "categoryId": "uncurser", "alias": "Keep"}]}
        expected = deepcopy(state)
        expected["stored"].pop(0)
        for record in (legacy, dict(legacy, macro="screws_tilt_calculate")):
            self.assertEqual(categories.disable(state, record), expected)

    def test_ambiguous_existing_settings_and_bad_undo_are_rejected(self):
        for state in ({"categories": [{"id": "a", "name": "UNCURSER"}, {"id": "b", "name": "UNCURSER"}]},
                      {"stored": [{"name": "bed_screw_helper"}, {"name": "BED_SCREW_HELPER"}]},
                      {"stored": [{"name": "screws_tilt_calculate"}, {"name": "SCREWS_TILT_CALCULATE"}]}):
            with self.assertRaises(ValueError):
                categories.enable(state)
        enabled, record = categories.enable(None)
        record["unrelated"] = "not allowed"
        with self.assertRaises(ValueError):
            categories.disable(enabled, record)
        record.pop("unrelated")
        record["macro"] = "other_macro"
        with self.assertRaises(ValueError):
            categories.disable(enabled, record)


if __name__ == "__main__":
    unittest.main()
