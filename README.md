# Uncurser
### Portable Windows app for ~~uncursing~~ improving your 3d printer firmware.

**[Download Uncurser](https://github.com/Tselovanskyi/Uncurser/releases/latest)**
(No installation needed)

Currently supports Creality K2 Plus, firmware 1.1.6.4 and tested only with latest Orca.
> [!TIP]
>Erase "G-code thumbnails" field in Orca's printer settings so the printer firmware can reach the values needed for Adaptive Bed Mesh, otherwise they're burried too deep in generated G-Code behind the thumbnail code.

> [!CAUTION]
> It has not been extensively tested yet. Use it at your own risk!



<img width="745" height="953" alt="image" src="https://github.com/user-attachments/assets/9d11b348-215f-45cc-85ef-02e1ca315db3" />

1. Automatically find or add your printer manually.
2. Connect to it.
3. Select mods you want to use.
4. Apply while the printer is idle.
5. Once the message shows succesfull write - power cycle the printer to finish applying (like actually power cycle it physically, not through fluidd, don't skip this step, it's important).


> [!TIP]
>All mods are reversible.
>Original backups are automatically created directly on the printer on first apply. Turn a mod off to undo it, or use **Restore original** to restore everything to pre-uncurser state.

## Roadmap

- [ ] **Bed screw adjustment helper** — guide manual adjustment of the bed leveling screws.
- [ ] **Safe print stop** — lower the bed before moving the printhead when stopping or cancelling a print.
- [ ] **Fluidd camera fix** — restore the camera feed in Fluidd.
