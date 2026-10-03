# Project guidelines

- Ignore Git history unless the user explicitly requests Git-related work.
- Questions do not authorize code changes, builds, or other heavy work. Ask first unless the user has explicitly requested that work.
- The user does testing when possible. Keep automated checks focused; avoid excessive testing.
- Keep project work inside this project folder.
- Keep commit messages and release descriptions concise.

## Release versions

- Before every new release, update `__version__` in `app/__init__.py` to a new `major.minor.patch` version. Never replace a published release with a changed build under the same version.
- `app/__init__.py` is the single source of truth. The top-right app label, EXE filename (`Uncurser_v<version>.exe`), and Windows File version and Product version must derive from it. Do not hardcode those versions elsewhere.
- After changing the version, build using `build-portable.ps1`. It generates Windows metadata through `tools/version_info.py`, verifies the metadata, and runs the packaged startup check.
- Before publishing, verify that the app label, EXE filename, Windows metadata, Git tag (`v<version>`), and GitHub release version all agree. Upload the newly built versioned EXE from `build/single-exe-release`.
- Distribute one EXE. Preserve `Uncurser_settings` beside it when replacing a local build; never include saved printer settings in a release.
