# IFC4: Hent dekketykkelse (slab thickness) fra IFC
# Installer: pip install ifcopenshell
#
# Bruk:
#   python slab_thickness_ifc4.py "C:/path/modell.ifc" --only-floors --to-mm --csv out.csv
#
# Finner tykkelse i denne rekkefølgen:
#   1) Material-lag (IfcMaterialLayerSetUsage / IfcMaterialLayerSet)  -> sum LayerThickness
#   2) PropertySet (Pset_SlabCommon / annet)                           -> Thickness / OverallThickness
#   3) Geometri (IfcExtrudedAreaSolid.Depth)                           -> Depth

import argparse
import csv
import math
import ifcopenshell
import ifcopenshell.util.element as util_element
import ifcopenshell.util.unit as util_unit


# -----------------------------
# Helpers: units
# -----------------------------
def get_length_scale_to_m(model) -> float:
    """
    Returnerer faktor som ganger IFC-lengde til meter.
    (f.eks. hvis IFC er i mm => 0.001)
    """
    try:
        # ifcopenshell.util.unit.calculate_unit_scale(model) returnerer ofte faktor til SI (meter)
        return float(util_unit.calculate_unit_scale(model))
    except Exception:
        return 1.0


# -----------------------------
# Thickness sources
# -----------------------------
def thickness_from_layers(slab) -> float | None:
    """
    Leser tykkelse fra material-lag, hvis finnes.
    Returnerer tykkelse i IFC-lengdeenhet (samme enhet som i filen).
    """
    mat = util_element.get_material(slab, should_inherit=True)
    if not mat:
        return None

    # Noen ganger får man en liste tilbake
    candidates = []
    if hasattr(mat, "__iter__") and not mat.is_a("IfcMaterialLayerSetUsage") and not mat.is_a("IfcMaterialLayerSet"):
        candidates = [m for m in mat if m]
    else:
        candidates = [mat]

    for m in candidates:
        if m.is_a("IfcMaterialLayerSetUsage"):
            layer_set = m.ForLayerSet
            if layer_set and layer_set.MaterialLayers:
                return sum((layer.LayerThickness or 0.0) for layer in layer_set.MaterialLayers)

        if m.is_a("IfcMaterialLayerSet"):
            if m.MaterialLayers:
                return sum((layer.LayerThickness or 0.0) for layer in m.MaterialLayers)

    return None


def thickness_from_psets(slab) -> float | None:
    """
    Leser tykkelse fra PropertySets (Pset_*), typisk:
      - Pset_SlabCommon.Thickness
      - Pset_SlabCommon.OverallThickness
    Returnerer tykkelse i IFC-lengdeenhet.
    """
    try:
        psets = util_element.get_psets(slab, psets_only=False, qtos_only=False)
    except Exception:
        return None

    # Søk først i Pset_SlabCommon
    for pset_name in ("Pset_SlabCommon", "Pset_ElementCommon", "Pset_BuildingElementCommon"):
        pset = psets.get(pset_name)
        if isinstance(pset, dict):
            for key in ("Thickness", "OverallThickness", "NominalThickness"):
                val = pset.get(key)
                if isinstance(val, (int, float)) and val > 0:
                    return float(val)

    # Fallback: let etter noe som heter *Thickness* uansett pset
    for _, pset in psets.items():
        if isinstance(pset, dict):
            for key, val in pset.items():
                if "thickness" in str(key).lower() and isinstance(val, (int, float)) and val > 0:
                    return float(val)

    return None


def _walk_representation_items(representation):
    """
    Generator som flater ut items fra Representation, inkl. mapped items.
    """
    if not representation:
        return
    reps = getattr(representation, "Representations", None) or []
    for rep in reps:
        for item in getattr(rep, "Items", None) or []:
            yield item
            # IfcMappedItem -> gå inn i mapped representation
            if item.is_a("IfcMappedItem"):
                try:
                    mapped_rep = item.MappingSource.MappedRepresentation
                    for mapped_item in getattr(mapped_rep, "Items", None) or []:
                        yield mapped_item
                except Exception:
                    pass


def thickness_from_geometry(slab) -> float | None:
    """
    Leser tykkelse fra geometri (IfcExtrudedAreaSolid.Depth).
    Returnerer tykkelse i IFC-lengdeenhet.
    """
    rep = getattr(slab, "Representation", None)
    if not rep:
        return None

    # Prøv å finne første ExtrudedAreaSolid med fornuftig depth
    for item in _walk_representation_items(rep):
        if item.is_a("IfcExtrudedAreaSolid") and item.Depth is not None:
            d = float(item.Depth)
            if d > 0:
                return d

        # Noen ganger ligger solid inne i IfcBooleanClippingResult
        if item.is_a("IfcBooleanClippingResult"):
            # Base operand kan være ExtrudedAreaSolid
            base = getattr(item, "FirstOperand", None)
            if base and base.is_a("IfcExtrudedAreaSolid") and base.Depth is not None:
                d = float(base.Depth)
                if d > 0:
                    return d

    return None


# -----------------------------
# Main extraction
# -----------------------------
def extract_slab_thicknesses(
    ifc_path: str,
    only_floors: bool = False,
    to_mm: bool = False,
):
    model = ifcopenshell.open(ifc_path)
    scale_to_m = get_length_scale_to_m(model)

    # IFC4 kan ha IfcSlab og IfcSlabElementedCase
    slabs = list(model.by_type("IfcSlab")) + list(model.by_type("IfcSlabElementedCase"))

    results = []
    for slab in slabs:
        ptype = str(getattr(slab, "PredefinedType", "") or "").upper()

        if only_floors and ptype != "FLOOR":
            continue

        thickness = None
        source = None

        # 1) Material layers
        t = thickness_from_layers(slab)
        if t and t > 0:
            thickness = t
            source = "layers"

        # 2) Psets
        if thickness is None:
            t = thickness_from_psets(slab)
            if t and t > 0:
                thickness = t
                source = "pset"

        # 3) Geometry
        if thickness is None:
            t = thickness_from_geometry(slab)
            if t and t > 0:
                thickness = t
                source = "geometry"

        # Convert
        thickness_out = None
        unit_out = None
        if thickness is not None:
            # thickness is in IFC unit. Convert to mm if requested.
            if to_mm:
                thickness_out = thickness * scale_to_m * 1000.0
                unit_out = "mm"
            else:
                thickness_out = thickness
                unit_out = "IFC_length_unit"

        results.append(
            {
                "GlobalId": slab.GlobalId,
                "Name": slab.Name or "",
                "PredefinedType": ptype,
                "Thickness": thickness_out,
                "Unit": unit_out,
                "Source": source,
            }
        )

    return results


def print_results(results):
    for r in results:
        if r["Thickness"] is None:
            print(f"{r['GlobalId']} | {r['Name']} | {r['PredefinedType']} | thickness=NOT FOUND")
        else:
            # pen utskrift
            val = r["Thickness"]
            if isinstance(val, float):
                s = f"{val:.2f}" if r["Unit"] == "mm" else f"{val:.4f}"
            else:
                s = str(val)
            print(
                f"{r['GlobalId']} | {r['Name']} | {r['PredefinedType']} | thickness={s} {r['Unit']} | {r['Source']}"
            )


def write_csv(results, csv_path):
    fieldnames = ["GlobalId", "Name", "PredefinedType", "Thickness", "Unit", "Source"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(results)


def main():
    ifc_path = "LLYN.B308.ifc"
    results = extract_slab_thicknesses(ifc_path, only_floors=True, to_mm=True)
    print_results(results)

