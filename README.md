# Uncurser
### Portable Windows app for ~~uncursing~~ improving your 3d printer firmware.

**[Download Uncurser](https://github.com/Tselovanskyi/Uncurser/releases/latest)**
(No installation needed)

Currently supports Creality K2 Plus, firmware 1.1.6.4 and tested only with latest Orca.
> [!TIP]
>Erase "G-code thumbnails" field in Orca's printer settings so the printer firmware can reach the values needed for Adaptive Bed Mesh, otherwise they're burried too deep in generated G-Code behind the thumbnail code.

> [!CAUTION]
> It has not been extensively tested yet. Use it at your own risk!



<img width="771" height="1053" alt="image" src="https://github.com/user-attachments/assets/7dd03fb1-3d6d-4c01-b28b-33bbde8f0f4d" />

1. Automatically find or add your printer manually.
2. Connect to it.
3. Select mods you want to use.
4. Apply while the printer is idle.
5. Once the message shows succesfull write - power cycle the printer to finish applying (like actually power cycle it physically, not through fluidd, don't skip this step, it's important).


> [!TIP]
>All mods are reversible.
>Original backups are automatically created directly on the printer on first apply. Turn a mod off to undo it, or use **Restore original** to restore everything to pre-uncurser state.

Every mod, including the bed screw helper, stores its version and verified uninstall instructions in `Uncurser_mods.json` on the printer. The update toggle stages installed mod updates; press Apply to save them. Enabling another mod also enrolls the three shipped unstamped v0.1.5 mods after verifying their code and original backups. Failed verification blocks Apply. Original backups remain unchanged.

## Roadmap

- [x] **Bed screw adjustment helper** - Fluidd helper for manual adjustment of the bed leveling screws. Power cycle, home all then run `SCREWS_TILT_CALCULATE` in Tools dropdown in Fluidd.
- [ ] **Safe print stop** - lower the bed before moving the printhead when stopping or cancelling a print.
- [ ] **Fluidd camera fix** - restore the camera feed in Fluidd.
