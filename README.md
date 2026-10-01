# Uncurser — portable K2 Plus desktop app

Extract the complete `Uncurser-portable-win64.zip` to a writable folder and run
`Uncurser.exe`. Keep `_internal` beside the executable. Python, Qt and SSH
dependencies are bundled; no installation or Windows administrator access is
required. App settings, trusted SSH keys and bounded logs live in `data` beside
the executable. Saved printer names, IPs, SSH usernames and passwords are stored in
`data/settings.json`. Passwords are stored as plain text so the saved list remains
portable between PCs; the password fields in the app show the saved text.
New entries use `root` / `creality_2024`; saved nonempty passwords are preserved.
Google Sans is bundled under the SIL Open Font License, so no font installation
is needed. The Windows 11 main title bar hides its text against black while
keeping native controls and the app's name in the taskbar. Dialog titles remain visible.
The app writes no printer backups to
the PC and installs no printer service or extra Klipper module.

## First use

1. Use **+ Add** beside **Find printer** to enter an IP, e.g.
   `192.168.50.130`, or use **Find printer** to add discovered printers.
   Search covers all active IPv4 networks in the background. Candidates appear
   as they are found and can be connected while the circular search indicator
   continues. Empty manual entries are discarded when you leave the row.
   Each row keeps an editable name, IP, SSH username and password. New discoveries
   use the reported hostname; blank names also fill on connection, with SSH as
   a fallback. Names are local to the app. Searching again does not overwrite
   existing entries. SSH port is 22.
   The **×** button removes that saved printer and its password from the list.
2. Reachable printers have a **Connect** button; the app checks saved addresses
   every 10 seconds. **Connect** reads the working file, existing backup and live
   status. Accept the printer's SSH key on the first connection. A changed key
   is rejected rather than silently trusted. **Scan printer** refreshes the
   connected printer at any time. Mods are greyed out until connected and scanned.
   The connected row's button reads **Connected**, changes to **Disconnect** on
   hover, and disconnects when clicked. During initial connection, the dark blue
   panel shows only a centered “Reading printer details…” message. It then shows the scanned
   printer model and firmware above its files and **Scan printer** button. Mods
   require **K2 Plus (F008), firmware 1.1.6.4**. Missing or unsupported identities
   keep modifications and restoration blocked, including in full-risk mode.
   Apply checks the model and firmware again before writing. Scan progress appears
   on that button; Apply progress appears inside the **Apply** button.
3. Review compatibility. Unknown files offer **Cancel**, **known versions only**,
   or the smaller **full risk** option. Full risk does not bypass ambiguous
   edits, damaged backups, active printing, unsupported model/firmware, or missing cancellation support.
4. Mod descriptions start collapsed. Click a mod card or **Show details** to
   smoothly expand or collapse its description. Opening one closes the others.
   **Hide details** stays below the
   revealed text and moves back up as it closes. The file summary stays visible beneath
   each mod name, above **Show details**. The file summary and switch keep their
   own actions without expanding or collapsing the card. The whole page
   scrolls together when needed. Set the mod switches. The single **Review changes** button above **Apply**
   is text-only and shows the exact pending diff for all staged printer-file edits.
   Scanning again retains staged selections. **Cancel** restores the latest
   scanned values in the UI without changing printer files.
   Each clickable **modifies … file(s)** / **adds … new file(s)** summary opens
   that mod's original-to-enabled diff, independent of the current toggle position.
   These summaries remain available whenever connected, including with no pending
   edits. The comparison uses the on-printer original backup; before a backup
   exists, an unmodified scanned file can serve as a clearly labelled prospective
   baseline. A missing original for an already-applied mod or a damaged backup
   is reported without inventing original values. The view contains no backup code
   and makes no changes to printer files.
5. With the printer idle, press **Apply**. This secures every required original backup,
   rechecks the files, prepares and verifies the replacements, saves them by rename,
   and reads them back to verify the saved contents. Then manually **power cycle
   the printer before printing** to load the changes. Apply does not restart
   Klipper, reboot the printer, or command heating or motion.

Cyan means the mod is actually present in the scanned file. Grayscale means
recognized original behavior. **Pending** appears above a changed toggle.

## Heat before native print preparation

The first release targets **K2 Plus, model F008, firmware 1.1.6.4** and the
`pause_resume.py` fingerprint read from the project owner's printer on
2026-09-26. Only the fingerprint and additive patch definitions are shipped,
not a copy of the printer's original file. No G-code postprocessor, Orca host
connection change, firmware binary edit, or extra runtime module is involved.

The mod adds code to the existing `pause_resume.py`. On connection it remembers
the latest native job ID. A newly recorded job at the native early-start callback
starts bed and requested chamber heating, then waits using the existing
M190/M191 handling. Both starts precede both waits. Existing native cancellation
hooks consume the current job ID; cancelled waits report cancellation. The
original cleanup code remains. Manual G28 and bed mesh are not modified.

Temperature requests are read from executable startup commands before
`START_PRINT`. `START_PRINT BED_TEMP` takes precedence over preceding M140/M190.
No chamber request or an explicit M141 S0 means chamber off and no chamber wait.
Footer comments are ignored. Unsupported temperature commands, missing
START_PRINT, movement before START_PRINT, and invalid targets stop that early
callback rather than guessing a temperature. This first parser supports a header
up to 2 MiB and literal numeric M140/M190/M141/M191 S commands.

**Print verification is pending.** Automated checks cover patch reversal,
temperature parsing, new-job gating and simulated cancellation. A real print
must confirm the native controller tolerates the full heating wait and that
cancellation works from the printer/Orca interface. Watch the first run; the
startup should heat before motion, then continue once waits finish. A separate
cancelled warmup should stop heating and prevent startup motion. The native log
and Klipper log include `Uncurser:` messages from the patch. The app does not
claim this behavior has already been verified on a print.

## Speed up Bed mesh

This mod edits six complete values in the active `printer.cfg`:

| Section | Setting | Enabled value |
| --- | --- | --- |
| `bed_mesh` | `speed` | `700` mm/s |
| `bed_mesh` | `probe_count` | `9,9` |
| `printer` | `max_z_velocity` | `50` mm/s |
| `prtouch_v3` | `lift_speed` | `50` mm/s |
| `z_align` | `distance_ratio` | `0.9` |
| `z_align` | `quick_speed` | `50` mm/s |

The probe travel height (`horizontal_move_z`), retry count and downward probing
speed are unchanged. Existing travel heights set by an older version of the mod
are also left untouched when enabling or disabling this preset. These persistent
settings also affect manual mesh and relevant Z operations. Adaptive probing can
still reduce the configured grid for the print area.

The preset follows the owner's earlier
[bed-mesh settings](https://github.com/Tselovanskyi/K2Pus-UNCURSED#speed-up-bed-mesh).
The app replaces a whole numeric value within its exact section, then parses and
verifies the result. It does not match numeric prefixes. Duplicate definitions,
included-file overrides, malformed values and targeted options in the generated
SAVE_CONFIG block block editing, including with full risk selected.

The reference fingerprint was captured read-only from the owner's printer on
2026-10-01. It excludes these six option lines and the generated SAVE_CONFIG
block, so different initial values are accepted without assuming factory defaults.
The previous preset with its 3 mm travel height is also recognized.
The complete files are not distributed with the app.

Disabling restores only these six options from the immutable **on-printer**
original backup. An option initially absent, such as `lift_speed`, is removed.
Neither the current working values nor GitHub defaults replace an existing
original backup. Current generated calibration data and unrelated edits are
preserved. Read-only compatibility and local replacement checks passed; actual
machine behavior must be tested by the user after applying and power cycling.

## Mesh at print temperature

This independent mod changes one condition in
`[gcode_macro BED_MESH_CALIBRATE_START_PRINT]` in `gcode_macro.cfg`.
The original condition ignores a supplied `BED_TEMP` below
`custom_macro.default_bed_temp` (50°C on the reference printer). The enabled
condition accepts the supplied target, including zero when bed heating is off.
Existing heater validation still handles invalid targets. When no target is
supplied, the existing fallback remains. Manual `G29` is unchanged.

The captured 2026-10-01 startup log confirmed that the controller passed
`BED_TEMP=42`, but this macro raised the bed target to 50°C; `START_PRINT` then
returned it to 42°C. The new condition removes that extra heating cycle.
No slicer change, extra printer script, or global default-temperature change is
needed. The app finds the exact macro and condition, rejects ambiguous definitions,
and recognizes the mod from file contents. Turning it off restores the condition
from the immutable on-printer original while preserving unrelated edits.

Read-only recognition against the printer and local patch, reversal, and batch
checks passed. The fix has not yet been applied or verified during a real print.
After applying through the app, power cycle the printer before testing.

## Backups and restoration

The permanent original is stored beside the working file on the printer:

```
/usr/share/klipper/klippy/extras/pause_resume.ptn.Original.txt
/usr/share/klipper/klippy/extras/pause_resume.ptn.Original.txt.json
/mnt/UDISK/printer_data/config/printer.cfn.Original.txt
/mnt/UDISK/printer_data/config/printer.cfn.Original.txt.json
/mnt/UDISK/printer_data/config/gcode_macro.cfn.Original.txt
/mnt/UDISK/printer_data/config/gcode_macro.cfn.Original.txt.json
```

The `ptn` and `cfn` markers represent the original `.py` and `.cfg` extensions.
Restore writes to the original working paths and keeps the backups intact.
The manifest records the original path, checksum, owner and permissions. Neither
file is overwritten. There are no historical restore points or backup types.
Disabling a mod reverses only that mod's settings or additions, preserving
unrelated user edits. **Restore original** stages restoration of the eligible files
that have on-printer backups; the confirmation names those files. Other files
are left alone. This removes later manual edits, but retains the current generated
calibration block in `printer.cfg`.

An adjacent `*.uncursed-pending-<id>.txt` exists during Apply only. Uploads and
saved contents are verified, file and directory writes are flushed, and a changed
working file aborts replacement. A lost connection requires rescanning actual
contents before retrying. There remains a small check-to-rename concurrency
window; this is not a universal lock against other software writing the file.
For a combined Apply, every file is validated and every needed original backup
is secured before the first working-file replacement. Ordinary failures attempt
to roll back already-replaced files only if their contents still match what the
app wrote. Replacements remain atomic per file, not across the entire batch;
power loss or termination can leave a partial batch. Rescan before retrying.
Filesystem recovery after a sudden power loss and backup survival across firmware
updates or factory resets have not been verified. Config changes after a scan
require a rescan, except generated calibration changes, which are preserved from
a fresh read. Includes are checked again before replacement.

If original creation is interrupted and leaves an incomplete backup/manifest,
Apply stops for review rather than overwriting or inventing an original. Likewise,
an existing mod without an original backup is not automatically adopted.

## Development

All application files and build outputs are inside this project. Existing Orca
and monitoring scripts are not part of the app and are not changed.

```
.venv\Scripts\python.exe Uncurser.py
.venv\Scripts\python.exe -m unittest discover -s tests
powershell -ExecutionPolicy Bypass -File build-portable.ps1
```

The portable build uses PyInstaller's folder mode to bundle the runtime without
requiring a compiler installation. Dependencies and their licenses are included.
Separate builds are required for other operating systems.
