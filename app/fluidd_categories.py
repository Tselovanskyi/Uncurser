"""Own one Fluidd macro's category without replacing its other preferences."""
from copy import deepcopy


MACRO = "screws_tilt_calculate"
CATEGORY = "UNCURSER"


def validate(state):
    """Validate only the settings needed for an unambiguous scoped edit."""
    if state is None:
        return
    if not isinstance(state, dict):
        raise ValueError("Fluidd macro settings are not an object.")
    categories, stored = state.get("categories", []), state.get("stored", [])
    if not isinstance(categories, list) or not isinstance(stored, list):
        raise ValueError("Fluidd macro categories or preferences are invalid.")
    ids, helpers, targets = set(), 0, 0
    for category in categories:
        if (not isinstance(category, dict) or not isinstance(category.get("id"), str)
                or not category["id"] or not isinstance(category.get("name"), str)
                or category["id"] in ids):
            raise ValueError("Fluidd macro categories are invalid or duplicated.")
        ids.add(category["id"])
        targets += category["name"] == CATEGORY
    for preference in stored:
        if (not isinstance(preference, dict) or not isinstance(preference.get("name"), str)
                or ("categoryId" in preference and not isinstance(preference["categoryId"], str))):
            raise ValueError("Fluidd macro preferences are invalid.")
        helpers += preference["name"].lower() == MACRO
    if targets > 1 or helpers > 1:
        raise ValueError("The Fluidd helper category or macro preference is ambiguous.")


def validate_record(record):
    if (not isinstance(record, dict)
            or set(record) != {"category", "created_category", "created_macro", "previous_category", "missing"}
            or type(record["created_category"]) is not bool
            or type(record["created_macro"]) is not bool):
        raise ValueError("Invalid Fluidd category uninstall instructions.")
    category, previous, missing = record["category"], record["previous_category"], record["missing"]
    if (not isinstance(category, dict) or set(category) != {"id", "name"}
            or not isinstance(category["id"], str) or not category["id"]
            or category["name"] != CATEGORY
            or not isinstance(previous, dict) or type(previous.get("present")) is not bool
            or set(previous) != ({"present", "value"} if previous["present"] else {"present"})
            or (previous["present"] and not isinstance(previous["value"], str))
            or not isinstance(missing, dict) or set(missing) != {"root", "categories", "stored"}
            or any(type(value) is not bool for value in missing.values())
            or (missing["root"] and not (missing["categories"] and missing["stored"]))
            or (record["created_macro"] and previous["present"])):
        raise ValueError("Invalid Fluidd category uninstall instructions.")


def enable(state, record=None):
    """Return updated macros settings and data-only undo; existing ownership stays."""
    validate(state)
    if record is not None:
        validate_record(record)
        # Updating helper code must not override a user's later category choice.
        return deepcopy(state), deepcopy(record)
    result = deepcopy(state) if state is not None else {}
    missing = {"root": state is None, "categories": "categories" not in result,
               "stored": "stored" not in result}
    categories, stored = result.setdefault("categories", []), result.setdefault("stored", [])
    category = next((entry for entry in categories if entry["name"] == CATEGORY), None)
    created_category = category is None
    if category is None:
        used = {entry["id"] for entry in categories}
        identifier, index = "uncurser", 2
        while identifier in used:
            identifier = "uncurser-" + str(index)
            index += 1
        category = {"id": identifier, "name": CATEGORY}
        categories.append(category)
    preference = next((entry for entry in stored if entry["name"].lower() == MACRO), None)
    created_macro = preference is None
    if preference is None:
        preference = {"name": MACRO}
        stored.append(preference)
    previous = {"present": "categoryId" in preference}
    if previous["present"]:
        previous["value"] = preference["categoryId"]
    preference["categoryId"] = category["id"]
    record = {"category": {"id": category["id"], "name": CATEGORY},
              "created_category": created_category, "created_macro": created_macro,
              "previous_category": previous, "missing": missing}
    validate_record(record)
    return result, record


def disable(state, record):
    """Restore owned membership, retaining preferences and categories edited later."""
    validate(state)
    validate_record(record)
    if state is None:
        return None
    result = deepcopy(state)
    stored, categories = result.get("stored", []), result.get("categories", [])
    preference = next((entry for entry in stored if entry["name"].lower() == MACRO), None)
    category_id = record["category"]["id"]
    if preference is not None and preference.get("categoryId") == category_id:
        previous = record["previous_category"]
        if previous["present"]:
            value = previous["value"]
            preference["categoryId"] = value if value == "0" or any(
                entry["id"] == value for entry in categories) else "0"
        else:
            preference.pop("categoryId", None)
        if record["created_macro"] and set(preference) == {"name"}:
            stored.remove(preference)
    if record["created_category"] and not any(entry.get("categoryId") == category_id for entry in stored):
        category = next((entry for entry in categories if entry["id"] == category_id), None)
        if category == record["category"]:
            categories.remove(category)
    for key in ("categories", "stored"):
        if record["missing"][key] and result.get(key) == []:
            result.pop(key)
    return None if record["missing"]["root"] and not result else result
