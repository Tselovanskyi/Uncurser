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
- Capture every affected file and the exact changes that mod owns. Added files require proof of their original absence and ownership; remove them only after verifying the recorded installed contents. Keep immutable original backups unchanged.
- Mods sharing a file must own their changes independently. Disabling or updating one must preserve other mods, unrelated user edits and printer-generated calibration data. Block conflicting or ambiguous changes.
- If an update changes a mod's affected-file list, implement and verify migration of its stored file scopes before shipping. Do not invalidate installed uninstall records by only changing the current file list.
- Apply working-file changes and their records in the same guarded batch, commit the records last, and include them in rollback handling.
- When changing or removing Python source files, clear only their adjacent compiled caches and matching `__pycache__` entries in that guarded batch. Include cache cleanup in rollback and handle orphan caches during restore even if the source is already absent; do not create permanent backups for generated caches.
- Send one-shot printer maintenance code through SSH stdin using the fixed in-memory bootstrap. Keep the SSH command short, disable bytecode writing, and never leave an uploaded maintenance script on the printer.
- When an installed record exists, disable or update using its verified uninstall instructions, not old app templates. The three shipped unstamped mods may be enrolled as v0.1.5 only after checking their code and original backups; other missing records mean an unknown version.
- Full **Restore original** uses verified printer-side original backups independently of installed mod versions or code recognition. Preserve current generated calibration data and validate backup integrity and fresh-file guards.
