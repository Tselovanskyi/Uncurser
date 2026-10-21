Bed Screw Adjustment Helper for K2 Plus

Manual helper: homes if needed, probes a fresh full bed mesh, and fits its
overall tilt to report how far to turn each configured screw. It does not
run Z_TILT_ADJUST.
It does not add any steps to print startup or change existing homing commands.

Installation (requires root access)

1. Back up printer.cfg before editing it. If either helper file already exists
   on the printer, inspect and back it up before replacing it.
2. Copy screws_tilt_adjust.py to:
   /usr/share/klipper/klippy/extras/screws_tilt_adjust.py
3. Copy screws_tilt_adjust.cfg to:
   /mnt/UDISK/printer_data/config/screws_tilt_adjust.cfg
4. Add this include once to /mnt/UDISK/printer_data/config/printer.cfg:
   [include screws_tilt_adjust.cfg]
5. Physically power cycle the printer after installation.

Use

Clear the bed, clean the nozzle, heat the bed to the chosen temperature and
home all axes. Once the bed reaches that temperature, run:
BED_SCREW_HELPER

Leave the screw marked "base" unchanged and adjust the other screws as shown.
CW means clockwise; CCW means counter-clockwise. 18° means 1/20 of a turn;
360° means one full turn. Run the command again to check the adjustment.
Keep bed and chamber temperatures consistent between measurements.

Uncurser arranges the supported Fluidd adjustment window's existing rows into
a grid, with arrows and degrees. PROBED AT 70°C shows the actual bed temperature
when probing starts. After the final check, Save bed mesh saves a profile named
for the rounded temperature (for example screws_70C); saving again at that
temperature replaces that profile. Save as default bed mesh replaces default.
Probing alone leaves the saved default unchanged. Neither button restarts
Klipper or changes print startup's choice of mesh.
Only this popup changes; shared Fluidd styles and components are untouched.
This supports direct HTTP access to Fluidd. Hard-refresh the browser (Ctrl+F5)
after applying or removing the mod. HTTPS service-worker caching is not handled.
Manual installation of these two helper files changes console output only;
Fluidd's standard window keeps its original turns:minutes display.

Optional example:
BED_SCREW_HELPER DIRECTION=CW MAX_DEVIATION=0.05

DIRECTION selects a reference screw so adjustments use the requested direction.
MAX_DEVIATION is the allowed height difference from that reference, in mm;
exceeding it reports an error after measuring. Omit it to report adjustments
without a tolerance check. Zero requires no measured difference.
The full mesh uses the printer's configured bed mesh bounds and probe count.
It also uses the current bed mesh travel speed. The Fluidd popup stays open
through homing and probing, with point progress and an approximate remaining
time that includes retries. Close dismisses the popup without
stopping the measurement. Progress reaches 100% only after successful completion.

Uninstall

Remove the include, remove the two installed helper files, then physically
power cycle the printer. Restore prior file versions if this installation
replaced an existing helper.
