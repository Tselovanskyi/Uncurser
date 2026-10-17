# Project guidelines

- Ignore Git history unless the user explicitly requests Git-related work.
- Questions do not authorize code changes, builds, or other heavy work. Ask first unless the user has explicitly requested that work.
- The user does testing when possible. Keep automated checks focused; avoid excessive testing.
- Keep project work inside this project folder.
- Keep commit messages and release descriptions concise.

## Typography

- Keep Google Sans at a minimum weight of 500 (Medium). Use the bundled native faces: 500 (Medium), 600 (SemiBold) and intentional 700 (Bold) in UI styles, rich text and font API calls.
- The bundled variable font includes a genuine named SemiBold instance so Windows can select 600 without substituting Bold. Preserve it when updating the font asset; use `tools/prepare_font.py` to prepare a new upstream font.
- Use default system font rendering with Qt 6.12 or newer. Do not add font hinting or antialiasing overrides to address individual labels.
- Check all UI and popup font rules when fixing font selection. `tools/check_fonts.py` checks supported weights and their resolved faces before packaging; do not bypass it. The user checks visual appearance.

## Release versions

- Before every new release, update `__version__` in `app/__init__.py` to a new `major.minor.patch` version. Never replace a published release with a changed build under the same version.
- `app/__init__.py` is the single source of truth. The top-right app label, EXE filename (`Uncurser_v<version>.exe`), and Windows File version and Product version must derive from it. Do not hardcode those versions elsewhere.
- After changing the version, build using `build-portable.ps1`. It generates Windows metadata through `tools/version_info.py`, verifies the metadata, and runs the packaged startup check.
- Before publishing, verify that the app label, EXE filename, Windows metadata, Git tag (`v<version>`), and GitHub release version all agree. Upload the newly built versioned EXE from `build/single-exe-release`.
- Distribute one EXE. Preserve `Uncurser_settings` beside it when replacing a local build; never include saved printer settings in a release.

## Mod versions and uninstall records

- Every mod, including helpers and future additions, must be registered in `app/mod_versions.py` with its revision and support for verified uninstall records. Do not add a mod without this support.
- A mod revision is the last app release that changed its installed printer code or configuration. Update only affected revisions; UI changes and adding other mods do not change existing revisions.
- Store only the currently installed revision and validated, data-only uninstall instructions in `Uncurser_mods.json` on the printer. Never execute those records or keep a history of previous implementations in the app.
- Each mod record also stores the canonical printer firmware used for its last installation or verified update. Write the stamp only with that mod's install/update transaction; keep retained records unchanged during scans, removals of other mods and storage migration. Missing legacy stamps mean unknown. Check present mods with missing or mismatched firmware after normal scans on tested and untested releases; offer verified cleanup without writing automatically or discarding staged edits.
- The firmware-issues popup's explicit **Fix issues** action runs an isolated guarded cleanup immediately, then reinstalls only compatible mods selected in that dialog after a fresh scan. It must exclude unrelated staged installs, automatic updates and Restore choices; normal scans and merely opening the popup remain read-only. Validate reinstall eligibility from verified cleaned files and current firmware sources. If reinstall verification fails, retain the cleanup result, rescan when possible and report any possible writes and required physical power cycle.
- While detected issues remain, disable all mod and update toggles and replace the main Apply action with a red **Fix issues** button that opens the scan's issues dialog. Preserve staged edits and cancel deferred Apply requests; restore normal controls only after a scan confirms the issues are resolved.
- Store uninstall records, immutable originals, their manifests and recovery copies under `/mnt/UDISK/Uncurser/`. Use `app/storage.py` to derive collision-free backup paths. Scans remain read-only; migrate existing adjacent backups and the old ledger during guarded Apply, preserving their bytes, attributes and firmware metadata. Verify destination copies before removing old copies; block conflicts and never reconstruct missing originals from modified working files.
- In **Fix issues**, remove obsolete originals and Uncurser backups with failed checksums or missing/incomplete metadata instead of relocating them. Normal Apply also removes eligible backups; defer those still needed by retained mods without migrating them. Limit cleanup to registered original-backup paths; preserve valid current-firmware originals, recovery copies and uninstall-ledger proofs. Remove dependent mods first using verified uninstall instructions or current firmware recovery files; never use a damaged backup as a restoration source. Guard present and missing files, ownership evidence and the uninstall ledger, and retain rollback data until completion.
- Capture every affected file and the exact changes that mod owns. Added files require proof of their original absence and ownership; remove them only after verifying the recorded installed contents. Keep immutable original backups unchanged.
- Mods sharing a file must own their changes independently. Disabling or updating one must preserve other mods, unrelated user edits and printer-generated calibration data. Block conflicting or ambiguous changes.
- If an update changes a mod's affected-file list, implement and verify migration of its stored file scopes before shipping. Do not invalidate installed uninstall records by only changing the current file list.
- Apply working-file changes and their records in the same guarded batch, commit the records last, and include them in rollback handling.
- After verified cleanup, reinstall through the clean-install path using the cleaned working files and fresh uninstall instructions. Preserve other mods' records. The app-owned `Uncurser_mods.json` may be updated when both original absence-proof files are missing: validate its contents and checksum, guard the missing proof paths, restrict updates to the selected records, and keep a recovery copy for rollback. Never fabricate an original or absence proof; partial or damaged proof files still block writes.
- Reinstall validates only the selected mods and their actual file/runtime dependencies. Do not enroll, update or revalidate unrelated retained mods. For shared files, verify only the retained ownership entries in files actually changed; preserve all other records and code.
- When changing or removing Python source files, clear only their adjacent compiled caches and matching `__pycache__` entries in that guarded batch. Include cache cleanup in rollback and handle orphan caches during restore even if the source is already absent; do not create permanent backups for generated caches.
- Send one-shot printer maintenance code through SSH stdin using the fixed in-memory bootstrap. Keep the SSH command short, disable bytecode writing, and never leave an uploaded maintenance script on the printer.
- When an installed record exists, disable or update using its verified uninstall instructions, not old app templates. The three shipped unstamped mods may be enrolled as v0.1.5 only after checking their code and original backups; other missing records mean an unknown version.
- Full **Restore original** uses verified printer-side original backups independently of installed mod versions or code recognition. Preserve current generated calibration data and validate backup integrity and fresh-file guards.
- On newer untested firmware for the verified printer model, permit installs, updates, verified removal and storage migration with an untested-firmware warning. Retain all ownership, backup integrity, conflict and printer-activity checks. Block cross-firmware full restore and report ambiguous leftovers without changing them. Never label a firmware release tested solely because operations are allowed.
- When backups or uninstall records are missing, recovery may use only verified active read-only firmware files. Select the configuration variant using that firmware's hardware-selection rules and the printer's board/structure identity. Restore only proven mod-owned regions, preserve unrelated edits and calibration data, and leave ambiguous changes unresolved. Keep pre-recovery copies separate from immutable originals; never present firmware defaults as a reconstructed original backup.
