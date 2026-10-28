PA-only Klipper helper for K2 Plus

The PA integration and timestamp history are adapted from:
https://github.com/Klipper3d/klipper/blob/master/klippy/chelper/kin_extruder.c
Retrieved 2026-10-25. Original copyright Kevin O'Connor; GNU GPLv3.

The four layout headers and local move-distance expression come from the
byte-identical K2 Plus firmware 1.1.6.4 / 1.1.7.0 sources. Preserve these layouts.

Local changes: prefixed exports, standalone move-distance expression, explicit
freeing of retired history, allocation-failure handling, and an API id.
The app bundles the compiled ARM hard-float helper and verifies its checksum.

Build and focused checks: tools/build_pa.py
License text: third_party/GPL-3.0.txt
