"""Schedule pressure-advance changes with a verified, PA-only native helper."""
import ast
import hashlib
import json
from pathlib import Path

from . import mod_comments, mods, storage

TARGET = "/usr/share/klipper/klippy/kinematics/extruder.py"
LIBRARY = "/usr/share/klipper/klippy/chelper/uncurser_pa.so"
STOCK_HELPER = "/usr/share/klipper/klippy/chelper/c_helper.so"
TOOLHEAD = "/usr/share/klipper/klippy/toolhead.py"
FILES = (TARGET, LIBRARY)
ADDED_FILES = (LIBRARY,)
DEPENDENCIES = (STOCK_HELPER, TOOLHEAD)
ORIGINAL = storage.original(TARGET)
MANIFEST = ORIGINAL + ".json"
REFERENCE_HASH = "d3dcf477216bf7ad47ee16994355c4de4c09811cbd74f96036362f374e3d6751"
STOCK_HELPER_HASH = "13127750585144db9b5216908332e8890840dc034c390d680ea777fc919a9221"
_TOOLHEAD_FUNCTIONS = {
    "_process_moves": "1e62a9377956c51b2d631d475c4ef9b2bf3586fdedf8723b8f8819d6a24ced17",
    "note_step_generation_scan_time": "4ca461bfaa42dc0e662f5db0f20f1f0cf16c872a9f6df86a459d513abc0648d2",
    "register_lookahead_callback": "b707a491ac2545370a0a214a9adb3697d97bd99b6a2f2a493d42fa61bda12b64",
}
_QUEUE_FUNCTIONS = {
    "get_last": "713748bfe5b87da1d73825ea7d7b92ef9a99ef95432ff28764a2d2410ae40990",
    "flush": "898fe76abc4ca7eca0dc8c826b161b84428704432d9979801ac36b8e0fa696ec",
}
_ASSETS = Path(__file__).resolve().parent / "assets" / "pa"
_CLASS = "class ExtruderStepper:\n"
_OLD_ALLOC = '''        self.sk_extruder = ffi_main.gc(ffi_lib.extruder_stepper_alloc(),
                                       ffi_lib.free)
'''
_NEW_ALLOC = '''        # Partially restored source must not mix the two native PA layouts.
        loader = globals().get("_uncurser_pa_library")
        self._uncurser_pa = None
        if loader is not None and "_uncurser_pa" in self._set_pressure_advance.__code__.co_consts:
            self._uncurser_pa = loader(ffi_main)
        if self._uncurser_pa is None:
            self.sk_extruder = ffi_main.gc(ffi_lib.extruder_stepper_alloc(),
                                           ffi_lib.free)
        else:
            pointer = self._uncurser_pa.uncurser_pa_alloc()
            if pointer == ffi_main.NULL:
                raise MemoryError("Unable to allocate the Uncurser PA helper")
            self.sk_extruder = ffi_main.gc(pointer,
                lambda pointer, library=self._uncurser_pa: library.uncurser_pa_free(pointer))
'''
_OLD_SET = '''    def _set_pressure_advance(self, pressure_advance, smooth_time):
        old_smooth_time = self.pressure_advance_smooth_time
        if not self.pressure_advance:
            old_smooth_time = 0.
        new_smooth_time = smooth_time
        if not pressure_advance:
            new_smooth_time = 0.
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.note_step_generation_scan_time(new_smooth_time * .5,
                                                old_delay=old_smooth_time * .5)
        ffi_main, ffi_lib = chelper.get_ffi()
        espa = ffi_lib.extruder_set_pressure_advance
        espa(self.sk_extruder, pressure_advance, new_smooth_time)
        self.pressure_advance = pressure_advance
        self.pressure_advance_smooth_time = smooth_time
'''
_NEW_SET = '''    def _set_pressure_advance(self, pressure_advance, smooth_time):
        old_smooth_time = self.pressure_advance_smooth_time if self.pressure_advance else 0.
        new_smooth_time = smooth_time if pressure_advance else 0.
        toolhead = self.printer.lookup_object("toolhead")
        if getattr(self, "_uncurser_pa", None) is None:
            toolhead.note_step_generation_scan_time(new_smooth_time * .5,
                                                    old_delay=old_smooth_time * .5)
            ffi_main, ffi_lib = chelper.get_ffi()
            ffi_lib.extruder_set_pressure_advance(self.sk_extruder,
                                                 pressure_advance, new_smooth_time)
        else:
            queued_move = None
            def apply(print_time):
                if queued_move is not None:
                    # Match the native queue's addition order exactly. Its
                    # callback timestamp uses a differently grouped sum.
                    print_time = toolhead.print_time
                    for entry in toolhead.move_queue.queue:
                        move = entry[0]
                        print_time = ((print_time + move.accel_t)
                                      + move.cruise_t) + move.decel_t
                        if move is queued_move:
                            break
                    else:
                        raise RuntimeError("Uncurser PA move is no longer queued")
                if self._uncurser_pa.uncurser_pa_set(self.sk_extruder, print_time,
                                                   pressure_advance, new_smooth_time):
                    raise MemoryError("Unable to allocate Uncurser PA history")
            if new_smooth_time != old_smooth_time:
                toolhead.note_step_generation_scan_time(new_smooth_time * .5,
                                                        old_delay=old_smooth_time * .5)
                apply(0.)
            else:
                queued = toolhead.move_queue.get_last()
                queued_move = queued[0] if queued is not None else None
                toolhead.register_lookahead_callback(apply)
        self.pressure_advance = pressure_advance
        self.pressure_advance_smooth_time = smooth_time
'''
_LOADER = '''def _uncurser_pa_library(ffi_main):
    import hashlib
    try:
        cached = getattr(chelper, "_uncurser_pa_library", None)
        if cached is not None:
            return cached
        folder = os.path.dirname(os.path.realpath(chelper.__file__))
        for name, expected in (("c_helper.so", "@STOCK_HASH@"),
                               ("uncurser_pa.so", "@LIBRARY_HASH@")):
            with open(os.path.join(folder, name), "rb") as stream:
                if hashlib.sha256(stream.read(262145)).hexdigest() != expected:
                    raise ValueError("Unrecognized PA native helper: " + name)
        ffi_main.cdef("""
            struct stepper_kinematics *uncurser_pa_alloc(void);
            void uncurser_pa_free(struct stepper_kinematics *sk);
            int uncurser_pa_set(struct stepper_kinematics *sk, double print_time,
                                double pressure_advance, double smooth_time);
            unsigned int uncurser_pa_api_version(void);
        """)
        library = ffi_main.dlopen(os.path.join(folder, "uncurser_pa.so"))
        if library.uncurser_pa_api_version() != 1:
            raise ValueError("Unsupported Uncurser PA helper interface")
        chelper._uncurser_pa_library = library
        return library
    except Exception:
        logging.warning("Uncurser PA helper unavailable; using stock PA", exc_info=True)
        return None

'''


def _manifest():
    try:
        return json.loads((_ASSETS / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def helper_data():
    manifest = _manifest()
    try:
        data = (_ASSETS / "uncurser_pa.so").read_bytes()
    except OSError as error:
        raise ValueError("The bundled PA native helper is missing.") from error
    if (len(data) != manifest.get("size") or mods.digest(data) != manifest.get("sha256")
            or data[:5] != b"\x7fELF\x01" or data[18:20] != b"\x28\x00"):
        raise ValueError("The bundled PA native helper failed verification.")
    return data


def _loader():
    return _LOADER.replace("@STOCK_HASH@", STOCK_HELPER_HASH).replace(
        "@LIBRARY_HASH@", _manifest().get("sha256", "0" * 64))


def _text(data):
    if not isinstance(data, bytes):
        raise ValueError("The printer extruder source is missing.")
    mod_comments.read(data)
    text = data.decode("utf-8-sig").replace("\r\n", "\n")
    tree = ast.parse(text, filename=TARGET, feature_version=(3, 9))
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "ExtruderStepper"]
    if len(classes) != 1 or text.count(_CLASS) != 1:
        raise ValueError("Expected one native ExtruderStepper class.")
    methods = {node.name: node for node in classes[0].body if isinstance(node, ast.FunctionDef)}
    if "__init__" not in methods or "_set_pressure_advance" not in methods:
        raise ValueError("The native extruder initialization or PA handler is missing.")
    enabled = "_uncurser_pa" in text
    alloc, setter = (_NEW_ALLOC, _NEW_SET) if enabled else (_OLD_ALLOC, _OLD_SET)
    init = methods["__init__"]
    source = "".join(text.splitlines(True)[init.lineno - 1:init.end_lineno])
    if source.count(alloc) != 1 or text.count(setter) != 1:
        raise ValueError("The extruder allocator or PA handler does not match the verified implementation.")
    if enabled and text.count(_loader() + _CLASS) != 1:
        raise ValueError("The installed PA loader differs from this mod revision.")
    return text, enabled


def validate(data):
    _text(data)


def fingerprint(data):
    text, enabled = _text(mod_comments.strip(data))
    if enabled:
        text = text.replace(_NEW_ALLOC, _OLD_ALLOC, 1).replace(_NEW_SET, _OLD_SET, 1)
        text = text.replace(_loader() + _CLASS, _CLASS, 1)
    return mods.digest(text.encode("utf-8"))


def inspect(data, original=None):
    try:
        _, enabled = _text(data)
        reference = fingerprint(original) if original is not None else REFERENCE_HASH
        known = fingerprint(data) == reference
        return {"known": known, "enabled": enabled, "patchable": True,
                "present": enabled, "label": "Uncursed" if enabled and known else "Original" if known else "Unknown",
                "reason": "" if known else "extruder.py differs outside this mod's owned changes."}
    except (ValueError, SyntaxError, UnicodeError) as error:
        return {"known": False, "enabled": None, "patchable": False,
                "present": isinstance(data, bytes) and b"_uncurser_pa" in data,
                "label": "Conflict", "reason": str(error)}


def transform(data, enabled, original=None):
    text, current = _text(data)
    if current == enabled:
        return data
    if enabled:
        helper_data()
        text = text.replace(_OLD_ALLOC, _NEW_ALLOC, 1).replace(_OLD_SET, _NEW_SET, 1)
        text = text.replace(_CLASS, _loader() + _CLASS, 1)
    else:
        text = text.replace(_NEW_ALLOC, _OLD_ALLOC, 1).replace(_NEW_SET, _OLD_SET, 1)
        text = text.replace(_loader() + _CLASS, _CLASS, 1)
    newline = "\r\n" if b"\r\n" in data else "\n"
    result = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + text.replace("\n", newline).encode()
    if fingerprint(result) != fingerprint(data):
        raise ValueError("An unrelated part of the extruder file changed.")
    return result


def replacements(before, after):
    old, old_enabled = _text(before)
    _, enabled = _text(after)
    if old_enabled == enabled:
        return []
    pairs = [(_OLD_ALLOC, _NEW_ALLOC), (_OLD_SET, _NEW_SET), (_CLASS, _loader() + _CLASS)]
    if not enabled:
        pairs = [(b, a) for a, b in pairs]
    newline = "\r\n" if b"\r\n" in before else "\n"
    return [{"before": a.replace("\n", newline), "after": b.replace("\n", newline)} for a, b in pairs]


def validate_dependencies(dependencies):
    if not isinstance(dependencies, dict) or mods.digest(dependencies.get(STOCK_HELPER) or b"") != STOCK_HELPER_HASH:
        raise ValueError("The native step-generation helper does not match the verified K2 interface.")
    source = dependencies.get(TOOLHEAD)
    if not isinstance(source, bytes):
        raise ValueError("The toolhead motion scheduler is missing.")
    tree = ast.parse(source.decode("utf-8-sig"), feature_version=(3, 9))
    for owner_name, functions in (("ToolHead", _TOOLHEAD_FUNCTIONS), ("MoveQueue", _QUEUE_FUNCTIONS)):
        owners = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner_name]
        methods = [node for owner in owners for node in owner.body if isinstance(node, ast.FunctionDef)]
        for name, expected in functions.items():
            matching = [node for node in methods if node.name == name]
            if len(matching) != 1 or hashlib.sha256(ast.dump(matching[0], include_attributes=False).encode()).hexdigest() != expected:
                raise ValueError("The toolhead PA scheduling dependency has changed: " + owner_name + "." + name)


def inspect_files(files, originals=None, dependencies=None):
    originals = originals or {}
    analysis = inspect(files.get(TARGET), originals.get(TARGET))
    library = files.get(LIBRARY)
    try:
        wanted = helper_data()
        recognized = library is None or library == wanted
        source_enabled = analysis.get("enabled")
        complete = source_enabled is True and library == wanted
        original = source_enabled is False and library is None
        partial = not (complete or original)
        analysis.update(enabled=True if complete else False if original else None,
                        known=analysis.get("known", False) and recognized and not partial,
                        patchable=analysis.get("patchable", False) and recognized and not partial,
                        present=analysis.get("present", False) or library is not None, partial=partial)
        if partial:
            analysis.update(label="Incomplete" if recognized else "Unknown",
                            reason="The PA source and native helper are not a complete matching installation.")
        if dependencies is not None:
            validate_dependencies(dependencies)
    except (ValueError, SyntaxError, UnicodeError) as error:
        analysis.update(patchable=False, dependency_error=str(error), reason=str(error))
    return analysis


def transform_files(files, enabled, originals=None, dependencies=None):
    if enabled:
        if dependencies is not None:
            validate_dependencies(dependencies)
        wanted = helper_data()
        if files.get(LIBRARY) not in (None, wanted):
            raise ValueError("The PA helper path contains an unrecognized file.")
        return {TARGET: transform(files[TARGET], True), LIBRARY: wanted}
    if files.get(LIBRARY) not in (None, helper_data()):
        raise ValueError("The PA helper contents cannot be verified for removal.")
    return {TARGET: transform(files[TARGET], False), LIBRARY: None}
