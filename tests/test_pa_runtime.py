"""PA callback timing against Creality's two different accumulation orders."""
from types import SimpleNamespace
import unittest

from app import pa


namespace = {}
exec("class Stepper:\n" + pa._NEW_SET, namespace)


class Scheduler:
    def __init__(self, start):
        self.print_time = start
        self.queue = []
        self.move_queue = SimpleNamespace(queue=self.queue,
            get_last=lambda: self.queue[-1] if self.queue else None)
        self.expected = []
        self.callback_times = []
        self.scan_flushes = 0

    def add(self, accel=.0123, cruise=.02, decel=.02):
        self.queue.append([SimpleNamespace(accel_t=accel, cruise_t=cruise, decel_t=decel), []])

    def register_lookahead_callback(self, callback):
        last = self.move_queue.get_last()
        if last is None:
            callback(self.print_time)
        else:
            last[1].append(callback)

    def note_step_generation_scan_time(self, delay, old_delay=0.):
        self.scan_flushes += 1
        self.flush()

    def flush(self, count=None):
        batch = self.queue[:] if count is None else self.queue[:count]
        native_time = self.print_time
        boundaries = []
        for move, _ in batch:
            # trapq_append_from_moveq's actual, left-associative C arithmetic.
            native_time += move.accel_t
            native_time += move.cruise_t
            native_time += move.decel_t
            boundaries.append(native_time)
        callback_time = self.print_time
        for (move, callbacks), boundary in zip(batch, boundaries):
            # Stock ToolHead._process_moves groups the duration sum instead.
            callback_time += move.accel_t + move.cruise_t + move.decel_t
            for callback in callbacks:
                self.callback_times.append(callback_time)
                self.expected.append(boundary)
                callback(callback_time)
        self.print_time = native_time
        del self.queue[:len(batch)]


def stepper(scheduler):
    result = namespace["Stepper"]()
    result.pressure_advance = .1
    result.pressure_advance_smooth_time = .04
    result.printer = SimpleNamespace(lookup_object=lambda _: scheduler)
    result.calls = []
    result.sk_extruder = object()

    def apply(pointer, timestamp, advance, smooth_time):
        result.calls.append((timestamp, advance, smooth_time))
        return 0

    result._uncurser_pa = SimpleNamespace(uncurser_pa_set=apply)
    return result


class PARuntimeTests(unittest.TestCase):
    def test_partial_source_uses_matching_allocator_and_setter(self):
        for allocation, handler, loader_state in (
                (pa._OLD_ALLOC, pa._OLD_SET, "available"),
                (pa._NEW_ALLOC, pa._OLD_SET, "available"),
                (pa._OLD_ALLOC, pa._NEW_SET, "available"),
                (pa._NEW_ALLOC, pa._NEW_SET, "missing"),
                (pa._NEW_ALLOC, pa._NEW_SET, "unavailable"),
                (pa._NEW_ALLOC, pa._NEW_SET, "available")):
            with self.subTest(allocation=allocation == pa._NEW_ALLOC,
                              handler=handler == pa._NEW_SET, loader=loader_state):
                stock_pointer, native_pointer = object(), object()
                calls = []

                def stock_set(pointer, advance, smooth_time):
                    self.assertIs(pointer, stock_pointer)
                    calls.append("stock")

                def native_set(pointer, timestamp, advance, smooth_time):
                    self.assertIs(pointer, native_pointer)
                    calls.append("native")
                    return 0

                native = SimpleNamespace(uncurser_pa_alloc=lambda: native_pointer,
                    uncurser_pa_free=lambda _: None, uncurser_pa_set=native_set)
                ffi = SimpleNamespace(NULL=None, gc=lambda pointer, destructor: pointer)
                stock = SimpleNamespace(extruder_stepper_alloc=lambda: stock_pointer,
                    free=lambda _: None, extruder_set_pressure_advance=stock_set)
                scope = {"ffi_main": ffi, "ffi_lib": stock,
                         "chelper": SimpleNamespace(get_ffi=lambda: (ffi, stock))}
                if loader_state != "missing":
                    scope["_uncurser_pa_library"] = lambda _: native if loader_state == "available" else None
                exec("class Stepper:\n    def __init__(self):\n" + allocation + handler, scope)
                owner = scope["Stepper"]()
                owner.printer = SimpleNamespace(lookup_object=lambda _: Scheduler(100.))
                owner.pressure_advance, owner.pressure_advance_smooth_time = .1, .04
                owner._set_pressure_advance(.2, .04)
                expected = ("native" if allocation == pa._NEW_ALLOC and handler == pa._NEW_SET
                            and loader_state == "available" else "stock")
                self.assertEqual(calls, [expected])

    def test_batch_callbacks_match_native_boundaries_exactly_at_long_uptime(self):
        for start in (10., 100., 1.e9):
            with self.subTest(start=start):
                scheduler = Scheduler(start)
                owner = stepper(scheduler)
                for index in range(200):
                    scheduler.add(*((.0123, .02, .02) if index % 2 == 0
                                    else (.00431, .05235, .000213)))
                    if index % 7 == 0:
                        owner._set_pressure_advance(.2 if index % 2 else .3, .04)
                scheduler.flush()
                self.assertNotEqual(scheduler.callback_times, scheduler.expected)
                self.assertEqual([call[0] for call in owner.calls], scheduler.expected)
                self.assertEqual(scheduler.scan_flushes, 0)

    def test_partial_flush_and_repeated_updates_on_same_move(self):
        scheduler = Scheduler(100.)
        owner = stepper(scheduler)
        scheduler.add()
        owner._set_pressure_advance(.2, .04)
        owner._set_pressure_advance(.3, .04)
        scheduler.add()
        owner._set_pressure_advance(.4, .04)
        scheduler.flush(1)
        self.assertEqual(len(owner.calls), 2)
        self.assertEqual(len(scheduler.queue), 1)
        scheduler.flush()
        self.assertEqual([call[0] for call in owner.calls], scheduler.expected)
        self.assertEqual([call[1] for call in owner.calls], [.2, .3, .4])

    def test_empty_queue_and_window_or_enable_changes_keep_required_flushes(self):
        scheduler = Scheduler(100.)
        owner = stepper(scheduler)
        owner._set_pressure_advance(.2, .04)
        self.assertEqual(owner.calls, [(100., .2, .04)])
        scheduler.add()
        owner._set_pressure_advance(.3, .04)
        owner._set_pressure_advance(.4, .08)
        self.assertEqual(owner.calls[-2:], [(scheduler.expected[0], .3, .04), (0., .4, .08)])
        owner._set_pressure_advance(0., .08)
        owner._set_pressure_advance(.4, .08)
        self.assertEqual(scheduler.scan_flushes, 3)
        self.assertEqual(owner.calls[-2:], [(0., 0., 0.), (0., .4, .08)])


if __name__ == "__main__":
    unittest.main()
