import unittest
from copy import deepcopy

from app import fluidd_categories as categories


class FluiddCategoriesTests(unittest.TestCase):
    def test_preserves_categories_and_every_other_macro_preference(self):
        state = {"categories": [{"id": "tools", "name": "Tools"}, {"id": "u", "name": "UNCURSER"}],
                 "stored": [{"name": "screws_tilt_calculate", "categoryId": "tools", "alias": "Screws",
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
        self.assertEqual(enabled["stored"], [{"name": "screws_tilt_calculate", "categoryId": "uncurser"}])
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
        self.assertEqual(disabled["stored"][0], {"name": "screws_tilt_calculate", "alias": "My screws"})
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
                 "stored": [{"name": "screws_tilt_calculate", "categoryId": "old"}]}
        enabled, record = categories.enable(state)
        enabled["categories"].pop(0)
        self.assertEqual(categories.disable(enabled, record)["stored"][0]["categoryId"], "0")

    def test_ambiguous_existing_settings_and_bad_undo_are_rejected(self):
        for state in ({"categories": [{"id": "a", "name": "UNCURSER"}, {"id": "b", "name": "UNCURSER"}]},
                      {"stored": [{"name": "screws_tilt_calculate"}, {"name": "SCREWS_TILT_CALCULATE"}]}):
            with self.assertRaises(ValueError):
                categories.enable(state)
        enabled, record = categories.enable(None)
        record["unrelated"] = "not allowed"
        with self.assertRaises(ValueError):
            categories.disable(enabled, record)


if __name__ == "__main__":
    unittest.main()
