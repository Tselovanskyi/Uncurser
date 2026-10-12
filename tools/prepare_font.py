"""Prepare Google Sans's missing selectable SemiBold face, without changing outlines.

Manual asset preparation only; requires fontTools, never runs in the app or build.
Source: Google Sans v14.000's GoogleSans[GRAD,opsz,wght].ttf from
https://github.com/googlefonts/googlesans/releases/tag/v14.000 (SIL OFL 1.1,
no Reserved Font Names). Copyright and license records remain unchanged.
The source already supports wght=600; only its name, fvar and STAT tables change.

Usage: python tools/prepare_font.py SOURCE.ttf OUTPUT.ttf
"""
import argparse
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.ttLib.tables import otTables
from fontTools.ttLib.tables._f_v_a_r import NamedInstance


def prepare(source: Path, output: Path):
    with TTFont(source, recalcTimestamp=False) as font:
        if not all(table in font for table in ("fvar", "STAT", "name")):
            raise ValueError("Expected the Google Sans variable font with name/fvar/STAT tables.")
        if font["name"].getDebugName(1) != "Google Sans":
            raise ValueError("Expected Google Sans, not a different font family.")
        axes = {axis.axisTag: axis for axis in font["fvar"].axes}
        if set(axes) != {"opsz", "wght", "GRAD"} or not axes["wght"].minValue <= 600 <= axes["wght"].maxValue:
            raise ValueError("Expected the supported Google Sans variable axes and weight range.")
        coordinates = {tag: axis.defaultValue for tag, axis in axes.items()}
        coordinates["wght"] = 600
        matches = [instance for instance in font["fvar"].instances if instance.coordinates == coordinates]
        if len(matches) > 1:
            raise ValueError("Ambiguous duplicate SemiBold instances.")
        if matches:
            name_id = matches[0].subfamilyNameID
            if font["name"].getDebugName(name_id) != "SemiBold":
                raise ValueError("The existing weight-600 instance has an unexpected name.")
        else:
            name_id = font["name"].addName("SemiBold")
            instance = NamedInstance()
            instance.subfamilyNameID = name_id
            instance.flags = 0
            instance.coordinates = coordinates
            font["fvar"].instances.append(instance)
        stat = font["STAT"].table
        weight_index = next(i for i, axis in enumerate(stat.DesignAxisRecord.Axis) if axis.AxisTag == "wght")
        matches = [value for value in stat.AxisValueArray.AxisValue
                   if value.Format in (1, 3) and value.AxisIndex == weight_index and value.Value == 600]
        if len(matches) > 1 or matches and matches[0].ValueNameID != name_id:
            raise ValueError("Conflicting weight-600 STAT metadata.")
        if not matches:
            value = otTables.AxisValue()
            value.Format = 1
            value.AxisIndex = weight_index
            value.Flags = 0
            value.ValueNameID = name_id
            value.Value = 600
            stat.AxisValueArray.AxisValue.append(value)
            stat.AxisValueCount += 1
        font.save(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    prepare(args.source, args.output)


if __name__ == "__main__":
    main()
