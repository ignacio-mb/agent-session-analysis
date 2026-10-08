"""Master data of the true world: plants, lines, equipment, raw materials, products, recipes, suppliers, customers,
employees, storage, FX and prices. Each builder returns DataFrames with canonical keys; the recorded layer
(recorded.py) later turns them into what the source systems hold."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from . import catalog as C
from .common import (
    END,
    ERP_UPGRADE,
    P2_OPEN,
    P3_ACQUIRED,
    P3_GO_LIVE,
    P3_LEGACY_HISTORY_FROM,
    START,
    d,
    fake_tax_id,
    rng,
)


class World:
    """Holds every true table (DataFrames) and the facts of each planted story."""

    def __init__(self):
        self.t: dict[str, pd.DataFrame] = {}
        self.story: dict[str, dict] = {}

    def __getitem__(self, k):
        return self.t[k]

    def __setitem__(self, k, v):
        self.t[k] = v


def person(r, used=None):
    while True:
        name = f"{C.FIRST[r.integers(len(C.FIRST))]} {C.LAST[r.integers(len(C.LAST))]}"
        if r.random() < 0.25:
            name += f" {C.LAST[r.integers(len(C.LAST))]}"
        if used is None or name not in used:
            if used is not None:
                used.add(name)
            return name


def place(r, bias=None):
    if bias and r.random() < 0.7:
        prov = bias
    else:
        provs = list(C.PROVINCE_WEIGHT)
        w = np.array([C.PROVINCE_WEIGHT[p] for p in provs])
        prov = provs[r.choice(len(provs), p=w / w.sum())]
    cities = C.PROVINCES_CITIES[prov]
    return prov, cities[r.integers(len(cities))]


# ---- Plants, lines, equipment, storage ---------------------------------------------------------------------------------

def build_plants(w: World):
    w["plants"] = pd.DataFrame(C.PLANTS, columns=["plant_id", "plant_name", "city", "province", "opened_date",
                                                  "erp_go_live", "acquired_date"])
    rows = []
    for line_id, plant, name, fam, kgh, bh, shifts, sh, start, legacy in C.LINES:
        a, p, q = C.LINE_PERF[line_id]
        rows.append(dict(line_id=line_id, plant_id=plant, line_name=name, family=fam, rated_kg_per_hour=kgh,
                         batch_hours=bh, shifts_per_day=shifts, shift_hours=sh, start_date=d(start),
                         legacy_name=legacy, true_availability=a, true_performance=p, true_quality=q))
    w["lines"] = pd.DataFrame(rows)

    r = rng("equipment")
    eq = []
    n = 1
    replacements = {}
    for line in rows:
        for kind in C.EQUIPMENT_BY_FAMILY[line["family"]]:
            install = max(line["start_date"], d("1998-04-17")) + timedelta(days=int(r.integers(0, 2000)))
            install = min(install, d("2021-12-01")) if line["start_date"] < START else line["start_date"]
            asset = dict(asset_id=f"EQ-{n:04d}", line_id=line["line_id"], plant_id=line["plant_id"],
                         asset_type=kind, manufacturer=C.MANUFACTURERS[r.integers(len(C.MANUFACTURERS))],
                         model=f"{kind[:3].upper()}-{int(r.integers(100, 990))}",
                         serial_no=f"{int(r.integers(10**7, 10**8 - 1))}", install_date=install,
                         decommission_date=None, replaced_by=None,
                         criticality=("A" if kind in ("Freidora continua", "Horno túnel", "Llenadora volumétrica",
                                                      "Envasadora de sachets multipista", "Túnel de congelado espiral",
                                                      "Horno") else "B" if r.random() < 0.6 else "C"))
            eq.append(asset)
            n += 1
    # replacements over time: (line, asset type, date)
    planned = [("L1-01", "Freidora continua", "2023-02-20"), ("L1-02", "Horno túnel", "2024-08-12"),
               ("L1-03", "Llenadora volumétrica", "2022-10-03"), ("L1-05", "Túnel de congelado espiral", "2025-03-17"),
               ("SUR-01", "Controladora de peso", "2024-10-07"),  # story 15: overfill until the tare fix
               ("L1-06", "Rebanadora", "2023-06-05"), ("L1-04", "Envasadora doypack", "2024-02-19"),
               ("DEL-01", "Llenadora volumétrica", "2025-06-09"), ("L1-01", "Detector de metales", "2022-05-16"),
               ("SUR-02", "Etiquetadora", "2025-09-01"), ("L1-03", "Tapadora", "2026-02-02"),
               ("L1-05", "Detector de metales", "2024-11-04")]
    for line_id, kind, when in planned:
        old = next(e for e in eq if e["line_id"] == line_id and e["asset_type"] == kind and e["decommission_date"]
                   is None)
        new = dict(old, asset_id=f"EQ-{n:04d}", install_date=d(when), decommission_date=None, replaced_by=None,
                   manufacturer=C.MANUFACTURERS[r.integers(len(C.MANUFACTURERS))],
                   model=f"{kind[:3].upper()}-{int(r.integers(100, 990))}",
                   serial_no=f"{int(r.integers(10**7, 10**8 - 1))}")
        old["decommission_date"] = d(when) - timedelta(days=1)
        old["replaced_by"] = new["asset_id"]
        eq.append(new)
        replacements[(line_id, kind)] = (old["asset_id"], new["asset_id"], d(when))
        n += 1
    # cold storage units are assets too
    storage = []
    for unit, plant, kind, desc, start in [
        ("FRZ-N1", "P1", "CONGELADO", "Cámara de congelados N°1", "2015-04-01"),
        ("FRZ-N2", "P1", "CONGELADO", "Cámara de congelados N°2", "2018-11-01"),
        ("CH-N1", "P1", "REFRIGERADO", "Cámara de frío materias primas", "2004-02-01"),
        ("CH-N2", "P1", "REFRIGERADO", "Cámara de frío lácteos y huevo", "2015-04-01"),
        ("FRZ-S1", "P2", "CONGELADO", "Cámara de congelados Sur", "2022-10-15"),
        ("CH-S1", "P2", "REFRIGERADO", "Cámara refrigerada Sur", "2022-06-20"),
        ("FRZ-D1", "P3", "CONGELADO", "Cámara congelados Delta", "2011-01-15"),
        ("CH-D1", "P3", "REFRIGERADO", "Cámara frío Delta", "2009-09-01"),
    ]:
        eq.append(dict(asset_id=f"EQ-{n:04d}", line_id=None, plant_id=plant, asset_type=f"Cámara {kind.lower()}",
                       manufacturer="Frío Industrial Austral", model=f"CAM-{int(r.integers(100, 990))}",
                       serial_no=f"{int(r.integers(10**7, 10**8 - 1))}", install_date=d(start),
                       decommission_date=None, replaced_by=None, criticality="A"))
        storage.append(dict(unit_id=unit, plant_id=plant, zone=kind, description=desc, asset_id=f"EQ-{n:04d}",
                            in_service=d(start)))
        n += 1
    w["equipment"] = pd.DataFrame(eq)
    w["storage_units"] = pd.DataFrame(storage)
    w.story["equipment_replacements"] = replacements

    # storage locations (warehouses and bins)
    locs = []
    for plant, wh in (("P1", "N"), ("P2", "S"), ("P3", "D")):
        locs += [dict(location_id=f"{wh}-MP-AMB", plant_id=plant, warehouse=f"ALM-{wh}-MP", zone="AMBIENTE",
                      unit_id=None, kind="MATERIA PRIMA"),
                 dict(location_id=f"{wh}-PT-AMB", plant_id=plant, warehouse=f"ALM-{wh}-PT", zone="AMBIENTE",
                      unit_id=None, kind="PRODUCTO TERMINADO"),
                 dict(location_id=f"{wh}-EMB", plant_id=plant, warehouse=f"ALM-{wh}-MP", zone="AMBIENTE",
                      unit_id=None, kind="EMBALAJE")]
    for s in storage:
        wh = s["unit_id"].split("-")[1][0]
        locs.append(dict(location_id=s["unit_id"], plant_id=s["plant_id"],
                         warehouse=f"ALM-{wh}-{'PT' if s['zone'] == 'CONGELADO' else 'MP'}", zone=s["zone"],
                         unit_id=s["unit_id"], kind="CAMARA"))
    w["storage_locations"] = pd.DataFrame(locs)

    sensors = []
    for s in storage:
        sensors.append(dict(sensor_id=f"TS-{s['unit_id']}", unit_id=s["unit_id"], plant_id=s["plant_id"],
                            vendor="IoT Gateway A" if s["plant_id"] != "P3" else "Delta Telemetría",
                            setpoint_c=-20.0 if s["zone"] == "CONGELADO" else 3.0,
                            alarm_high_c=-12.0 if s["zone"] == "CONGELADO" else 7.0,
                            installed=max(d(str(s["in_service"])), START) if s["plant_id"] != "P3" else P3_ACQUIRED))
    w["sensors"] = pd.DataFrame(sensors)


# ---- Raw materials -----------------------------------------------------------------------------------------------------

def build_raw_materials(w: World):
    rows = []
    for code, desc, cat, uom, alg, store, life, cost, lead, every in C.RAW:
        rows.append(dict(rm_code=code, description=desc, category=cat, uom=uom, allergens=alg, storage=store,
                         shelf_life_days=life, std_cost_usd=cost, lead_days=lead, order_every_days=every,
                         lot_traced=code != "RM-AGUA", kind="INGREDIENTE"))
    for code, desc, uom, cost in C.PACK:
        rows.append(dict(rm_code=code, description=desc, category="PACKAGING", uom=uom, allergens="",
                         storage="AMBIENTE", shelf_life_days=1095, std_cost_usd=cost, lead_days=12,
                         order_every_days=21, lot_traced=code not in ("PK-CAJA",), kind="ENVASE"))
    w["raw_materials"] = pd.DataFrame(rows)


# ---- Products ----------------------------------------------------------------------------------------------------------

FAMILY_PREFIX = {"CHIPS": "01", "CRACKERS": "02", "SAUCE_JAR": "03", "SAUCE_SACHET": "04", "FROZEN": "05",
                 "BAKERY": "06"}


def pack_label(fam, g, label=None):
    if label == "sachet":
        return f"Sachet {g} g"
    if label == "doypack":
        return f"Doypack {g // 1000} kg" if g >= 1000 else f"Doypack {g} g"
    if fam == "FROZEN" and g in (2880, 960):
        return "Food Service"
    if g >= 1000:
        return f"{g / 1000:g} kg".replace(".", ",")
    return f"{g} g"


def build_products(w: World):
    """Every product GATR ever made or sold, keyed by product_key. sku_code is the ERP code (reused once), p3_code the
    legacy code at Planta Delta."""
    r = rng("products")
    rows = []
    seq = {k: 100 for k in FAMILY_PREFIX.values()}
    seq.update({"07": 100, "08": 100, "09": 100, "11": 100})

    def code(prefix):
        seq[prefix] += int(r.integers(1, 9))
        return f"{prefix}{seq[prefix]:04d}"

    def add(**k):
        k.setdefault("p3_code", None)
        k.setdefault("segment_only", None)
        k.setdefault("private_label_chain", None)
        k.setdefault("export_market", None)
        k.setdefault("promo", False)
        k.setdefault("old_description", None)
        k.setdefault("renamed_on", None)
        k["product_key"] = f"PRD-{len(rows) + 1:04d}"
        rows.append(k)
        return k

    def launch_before():
        return d("2008-01-01") + timedelta(days=int(r.integers(0, 4900)))

    new_launch = {"PAP-TRU": d("2024-04-08"), "MAY-OLI": d("2023-05-15"), "SAL-SOJ": d("2022-09-05"),
                  "CRK-ORE": d("2023-09-04"), "LOC-CRI": d("2022-05-02"), "PAN-MUL": d("2022-03-07")}
    for f in C.F:
        packs = [(g, upc, None) for g, upc in f["packs"]]
        if f["code"] in C.SACHET_PACKS:
            packs += C.SACHET_PACKS[f["code"]]
        for i, (g, upc, label) in enumerate(packs):
            fam = "SAUCE_SACHET" if label in ("sachet", "doypack") else f["family"]
            brand = C.B_FS if fam == "SAUCE_SACHET" else f["brand"]
            share = 1.0 if len(packs) == 1 else [0.25, 0.45, 0.2, 0.1, 0.08][min(i, 4)]
            if label == "sachet":
                share = 0.6
            launch = new_launch.get(f["code"], launch_before())
            add(sku_code=code(FAMILY_PREFIX[fam]), formula_code=f["code"],
                description=f"{f['name']} {pack_label(fam, g, label)}", brand=brand, family=fam, pack_g=g,
                units_per_case=upc, launch_date=launch, retire_date=None, popularity=f["pop"] * share,
                allergens=f["allergens"], kind="BASE")

    by_desc = {p["description"]: p for p in rows}

    # Retired SKUs during the span (some replaced by a smaller pack: shrinkflation)
    retire = [("Papas Fritas Clásicas 400 g", "2024-06-30", None),
              ("Salsa Criolla 250 g", "2023-06-30", None),
              ("Papas Limón 80 g", "2024-12-31", None),
              ("Crackers Romero 200 g", "2025-03-31", None),
              ("Guiso de Lentejas 350 g", "2023-10-31", None),
              ("Galletas Saladas Familiares 400 g", "2025-08-31", None),
              ("Papas Fritas Clásicas 150 g", "2023-05-14", 135),
              ("Papas Onduladas 150 g", "2023-05-14", 135),
              ("Mayonesa Clásica 500 g", "2024-02-11", 475),
              ("Ketchup 500 g", "2024-02-11", 475),
              ("Crackers Clásicas 200 g", "2023-10-01", 180),
              ("Pan Lactal Blanco 550 g", "2024-09-01", 500),
              ("Ravioles de Ricota 500 g", "2025-04-06", 450)]
    for desc, when, smaller in retire:
        p = by_desc[desc]
        p["retire_date"] = d(when)
        if smaller:
            fam = p["family"]
            add(sku_code=code(FAMILY_PREFIX[fam]), formula_code=p["formula_code"],
                description=desc.replace(f" {p['pack_g']} g", f" {smaller} g"), brand=p["brand"], family=fam,
                pack_g=smaller, units_per_case=p["units_per_case"], launch_date=d(when) + timedelta(days=1),
                retire_date=None, popularity=p["popularity"], allergens=p["allergens"], kind="SHRINK",
                replaces=p["product_key"])

    # Old SKUs retired before or early in the span that still show up (migrated history, a few sales)
    for f in r.choice(len(C.F), 55, replace=False):
        f = C.F[int(f)]
        g = int(r.choice([60, 90, 120, 180, 350, 750, 900]))
        fam = f["family"]
        add(sku_code=code(FAMILY_PREFIX[fam]), formula_code=f["code"],
            description=f"{f['name']} {pack_label(fam, g)}", brand=f["brand"], family=fam, pack_g=g,
            units_per_case=int(r.choice([12, 20, 24])), launch_date=launch_before(),
            retire_date=START + timedelta(days=int(r.integers(20, 420))), popularity=f["pop"] * 0.08,
            allergens=f["allergens"], kind="LEGACY")

    # Re-codes: same product, new code (the bakery family moved to its own code range in 2023)
    for p in [p for p in rows if p["family"] == "BAKERY" and p["kind"] == "BASE" and p["retire_date"] is None][:8]:
        p["retire_date"] = d("2023-01-31")
        add(sku_code=code("11"), formula_code=p["formula_code"], description=p["description"], brand=p["brand"],
            family="BAKERY", pack_g=p["pack_g"], units_per_case=p["units_per_case"], launch_date=d("2023-02-01"),
            retire_date=None, popularity=p["popularity"], allergens=p["allergens"], kind="RECODE",
            replaces=p["product_key"])

    # Promotional packs, each sold for a few months
    promos = [("Papas Fritas Clásicas", 250, "Pack Mundial", "2022-10-17", "2022-12-31"),
              ("Papas Barbacoa", 250, "Pack Mundial", "2022-10-17", "2022-12-31"),
              ("Chizitos Queso", 120, "Pack Mundial", "2022-10-17", "2022-12-31"),
              ("Mayonesa Clásica", 1000, "+20% gratis", "2023-11-06", "2024-02-28"),
              ("Ketchup", 1000, "+20% gratis", "2023-11-06", "2024-02-28"),
              ("Crackers Clásicas", 300, "Promo 3x2", "2023-03-01", "2023-05-31"),
              ("Pizza Muzzarella", 1100, "Promo 2x1", "2024-06-03", "2024-08-31"),
              ("Empanadas de Carne x12", 720, "Promo Aniversario", "2024-10-01", "2024-10-31"),
              ("Mayonesa Clásica", 250, "Promo Aniversario", "2024-10-01", "2024-10-31"),
              ("Salsa Golf", 250, "Promo Aniversario", "2024-10-01", "2024-10-31"),
              ("Papas Fritas Clásicas", 80, "Promo Aniversario", "2024-10-01", "2024-10-31"),
              ("Pan Lactal Blanco", 390, "Promo Vuelta al Cole", "2025-02-17", "2025-03-31"),
              ("Hamburguesas Clásicas x4", 320, "Promo Verano", "2025-12-01", "2026-02-28"),
              ("Papas Onduladas", 250, "Promo Verano", "2025-12-01", "2026-02-28"),
              ("Salsa Barbacoa", 500, "Promo Verano", "2025-12-01", "2026-02-28"),
              ("Mayonesa Light", 500, "Promo Verano", "2026-01-05", "2026-02-28"),
              ("Ravioles de Ricota", 1000, "Promo Invierno", "2023-06-01", "2023-08-31"),
              ("Lasaña Bolognesa", 1000, "Promo Invierno", "2025-06-02", "2025-08-31"),
              ("Ñoquis de Papa", 1000, "Promo 29 de cada mes", "2024-04-29", "2024-12-31"),
              ("Galletas de Agua", 300, "Promo 3x2", "2025-03-03", "2025-05-31"),
              ("Papas Fritas Clásicas", 400, "Mega Pack Fiestas", "2022-12-01", "2023-01-15"),
              ("Papas Fritas Clásicas", 400, "Mega Pack Fiestas", "2023-12-01", "2024-01-15"),
              ("Papas Fritas Clásicas", 250, "Mega Pack Fiestas", "2024-12-01", "2025-01-15"),
              ("Papas Fritas Clásicas", 250, "Mega Pack Fiestas", "2025-12-01", "2026-01-15")]
    for name, g, promo, a, b in promos:
        f = next(f for f in C.F if f["name"] == name)
        fam = f["family"]
        promo_promo = f["pop"] * (1.4 if promo == "Promo Aniversario" else 0.6)
        add(sku_code=code("09"), formula_code=f["code"], description=f"{name} {pack_label(fam, g)} {promo}",
            brand=f["brand"], family=fam, pack_g=g, units_per_case=12 if g >= 500 else 20,
            launch_date=d(a), retire_date=d(b), popularity=promo_promo, allergens=f["allergens"], kind="PROMO",
            promo=True)

    # Multipacks and food-service sizes of the regular range
    for fc, g, n_units, label in [("PAP-CLA", 30, 6, "Multipack x6"), ("PAP-QUE", 80, 3, "Multipack x3"),
                                  ("CHZ-QUE", 60, 6, "Multipack x6"), ("PAP-OND", 30, 6, "Multipack x6"),
                                  ("CRK-CLA", 100, 4, "Pack x4"), ("GAL-AGU", 100, 4, "Pack x4"),
                                  ("CRK-QUE", 100, 3, "Pack x3"), ("TOS-ARR", 100, 3, "Pack x3"),
                                  ("PAL-SAL", 30, 12, "Display x12"), ("MAN-SAL", 30, 12, "Display x12"),
                                  ("LAS-BOL", 2500, 1, "Food Service"), ("CAN-VER", 2500, 1, "Food Service"),
                                  ("MIL-POL", 5000, 1, "Food Service"), ("PAT-POL", 3000, 1, "Food Service"),
                                  ("NOQ-PAP", 3000, 1, "Food Service"), ("PAN-LAC", 1200, 1, "Food Service"),
                                  ("PAN-HAM", 1320, 1, "Food Service x24"), ("PAN-RAL", 5000, 1, "Food Service"),
                                  ("POM-CLA", 3000, 1, "Food Service"), ("SAL-PIZ", 3000, 1, "Food Service"),
                                  ("CHI-CLA", 1000, 1, "Food Service"), ("MOS-CLA", 2900, 1, "Food Service"),
                                  ("SAL-GOL", 2900, 1, "Food Service"), ("KET-CLA", 2900, 1, "Food Service")]:
        f = C.FORMULAS[fc]
        fam = f["family"]
        add(sku_code=code(FAMILY_PREFIX[fam]), formula_code=fc,
            description=f"{f['name']} {pack_label(fam, g) if n_units == 1 else str(g) + ' g'} {label}",
            brand=f["brand"], family=fam, pack_g=g * n_units, units_per_case=4 if g * n_units >= 2000 else 10,
            launch_date=launch_before() if r.random() < 0.7 else START + timedelta(days=int(r.integers(30, 1200))),
            retire_date=None, popularity=f["pop"] * 0.12, allergens=f["allergens"],
            kind="FOODSERVICE" if label.startswith("Food") else "MULTIPACK")

    # Private label for four chains
    for chain, items in [("LAESTRELLA", [("PAP-CLA", 150), ("PAP-CLA", 400), ("CRK-CLA", 200), ("MAY-CLA", 500),
                                         ("MAY-CLA", 1000), ("KET-CLA", 500), ("POM-CLA", 520), ("PAN-LAC", 550),
                                         ("PIZ-MUZ", 550), ("EMP-CAR", 720), ("GAL-AGU", 200), ("SAL-GOL", 250)]),
                         ("DELPLATA", [("PAP-CLA", 150), ("MAY-CLA", 500), ("CRK-CLA", 200), ("KET-CLA", 250),
                                       ("PAN-LAC", 390), ("MIL-POL", 500), ("NOQ-PAP", 500), ("MOS-CLA", 250)]),
                         ("NANDU", [("PAP-CLA", 150), ("MAY-CLA", 500), ("KET-CLA", 500), ("CRK-CLA", 200),
                                    ("GAL-AGU", 200), ("PAN-LAC", 550), ("EMP-CAR", 720), ("PIZ-MUZ", 550),
                                    ("POM-CLA", 340), ("SAL-GOL", 500)]),
                         ("PAMPAGRANDE", [("PAP-CLA", 250), ("MAY-CLA", 1000), ("CRK-INT", 200), ("KET-CLA", 250),
                                          ("HAM-CLA", 320), ("MIL-POL", 1000), ("PAN-INT", 550),
                                          ("SAL-PIZ", 340)])]:
        label = {"LAESTRELLA": "Estrella", "DELPLATA": "Del Plata", "NANDU": "Ñandú",
                 "PAMPAGRANDE": "Pampa Grande"}[chain]
        for fc, g in items:
            f = C.FORMULAS[fc]
            fam = f["family"]
            add(sku_code=code("08"), formula_code=fc, description=f"{f['name']} {pack_label(fam, g)} Marca {label}",
                brand=f"Marca {label}", family=fam, pack_g=g, units_per_case=12 if g >= 500 else 24,
                launch_date={"LAESTRELLA": d("2021-03-01"), "DELPLATA": d("2023-04-03"), "NANDU": d("2020-08-03"),
                             "PAMPAGRANDE": d("2024-11-04")}[chain], retire_date=None,
                popularity=f["pop"] * 0.5, allergens=f["allergens"], kind="PRIVATE", private_label_chain=chain)

    # Export SKUs: labels per market
    exports = [("PAP-CLA", 150), ("PAP-CLA", 250), ("PAP-OND", 150), ("PAP-BBQ", 150), ("CRK-CLA", 200),
               ("CRK-INT", 200), ("GAL-AGU", 200), ("MAY-CLA", 500), ("MAY-CLA", 1000), ("KET-CLA", 500),
               ("SAL-GOL", 500), ("CHI-CLA", 250), ("POM-CLA", 520), ("EMP-CAR", 720), ("NOQ-PAP", 500),
               ("TOS-ARR", 100), ("MAN-SAL", 250), ("MOS-CLA", 250), ("SAL-BBQ", 500), ("PIZ-MUZ", 550)]
    for fc, g in exports:
        f = C.FORMULAS[fc]
        for market in (["MERCOSUR", "CHILE", "ANDINO"] if r.random() < 0.6 else ["MERCOSUR", "ANDINO"]) + (
                ["USA"] if fc in ("CHI-CLA", "MAY-CLA", "EMP-CAR", "PAP-CLA", "SAL-GOL") else []):
            fam = f["family"]
            add(sku_code=code("07" if fam != "FROZEN" else "05"), formula_code=fc,
                description=f"{f['name']} {pack_label(fam, g)} EXP {market}", brand=f["brand"], family=fam,
                pack_g=g, units_per_case=12 if g >= 500 else 24, launch_date=launch_before() if market != "USA" else
                d("2023-08-07"), retire_date=None, popularity=f["pop"] * 0.3, allergens=f["allergens"], kind="EXPORT",
                export_market=market)

    # P3 products: legacy codes from the start of the legacy history; ERP codes from the go-live
    p3seq = 1000
    for fam, fcs in C.P3_FORMULAS.items():
        for fc in fcs:
            f = C.FORMULAS[fc]
            packs = f["packs"][:2] if fam != "FROZEN" else f["packs"][:1]
            for g, upc in packs:
                p3seq += int(r.integers(1, 6))
                name = f"{f['name']} {pack_label(fam, g)} {C.P3_BRANDS[fam]}"
                add(sku_code=code("07"), formula_code=fc, description=name, brand=C.P3_BRANDS[fam], family=fam,
                    pack_g=g, units_per_case=upc, launch_date=d("2010-01-01") + timedelta(days=int(r.integers(0, 4000))),
                    retire_date=None, popularity=f["pop"] * 0.5, allergens=f["allergens"], kind="P3",
                    p3_code=f"{p3seq}", erp_from=P3_GO_LIVE)
    # P3 articles never migrated (discontinued at the acquisition or kept only in the legacy system)
    for fc, g, fam in [("EMP-CAR", 2880, "FROZEN"), ("PAN-LAC", 700, "BAKERY"), ("MAY-CLA", 2900, "SAUCE_JAR"),
                       ("TAR-JYQ", 1000, "FROZEN"), ("PAN-HAM", 660, "BAKERY"), ("KET-CLA", 2900, "SAUCE_JAR"),
                       ("PRE-PIZ", 800, "BAKERY"), ("EMP-JYQ", 2880, "FROZEN")]:
        f = C.FORMULAS[fc]
        p3seq += int(r.integers(1, 6))
        add(sku_code=None, formula_code=fc, description=f"{f['name']} {pack_label(fam, g)} {C.P3_BRANDS[fam]}",
            brand=C.P3_BRANDS[fam], family=fam, pack_g=g, units_per_case=4 if g > 1000 else 10,
            launch_date=d("2012-01-01"), retire_date=None, popularity=f["pop"] * 0.25, allergens=f["allergens"],
            kind="P3", p3_code=f"{p3seq}")

    df = pd.DataFrame(rows)
    df["erp_from"] = df.get("erp_from")
    df["replaces"] = df.get("replaces")

    # Renames: the description an order line carries changes on this date
    for desc, new, when in [("Papas Fritas Clásicas 80 g", "Papas Clásicas Crocante Sur 80 g", "2024-03-04"),
                            ("Papas Fritas Clásicas 30 g", "Papas Clásicas Crocante Sur 30 g", "2024-03-04"),
                            ("Papas Fritas Clásicas 250 g", "Papas Clásicas Crocante Sur 250 g", "2024-03-04"),
                            ("Salsa Pomarola 340 g", "Pomarola La Ribera 340 g", "2023-08-01"),
                            ("Chimichurri 250 g", "Chimichurri Casero 250 g", "2025-01-06"),
                            ("Galletas de Agua 200 g", "Galletitas de Agua 200 g", "2022-09-01"),
                            ("Hamburguesas Clásicas x4 320 g", "Hamburguesas de Carne x4 320 g", "2024-05-06")]:
        i = df.index[df["description"] == desc][0]
        df.loc[i, "old_description"] = desc
        df.loc[i, "description"] = new
        df.loc[i, "renamed_on"] = d(when)

    # SKU reuse: a retired code handed to a new product
    criolla = df.index[(df["description"] == "Salsa Criolla 250 g")][0]
    reused_code = df.loc[criolla, "sku_code"]
    f = C.FORMULAS["MAY-OLI"]
    df.loc[len(df)] = dict(df.loc[criolla], product_key=f"PRD-{len(df) + 1:04d}", formula_code="MAY-OLI",
                           description="Mayonesa con Oliva 500 g", pack_g=500, units_per_case=12,
                           launch_date=d("2024-09-02"), retire_date=None, popularity=2.2, allergens=f["allergens"],
                           kind="REUSE", old_description=None, renamed_on=None)
    chips_legacy = df[(df["kind"] == "LEGACY") & (df["family"] == "CHIPS")].index[0]
    reused2 = df.loc[chips_legacy, "sku_code"]
    df.loc[len(df)] = dict(df.loc[chips_legacy], product_key=f"PRD-{len(df) + 1:04d}", formula_code="CRK-SEM",
                           description="Crackers Semillas 300 g", family="CRACKERS", brand=C.B_CR, pack_g=300,
                           units_per_case=16, launch_date=d("2025-02-03"), retire_date=None, popularity=1.0,
                           allergens="gluten,sésamo", kind="REUSE", old_description=None, renamed_on=None)
    w.story["sku_reuse"] = [dict(sku_code=reused_code, old=df.loc[criolla, "product_key"],
                                 new=df.iloc[-2]["product_key"]),
                            dict(sku_code=reused2, old=df.loc[chips_legacy, "product_key"],
                                 new=df.iloc[-1]["product_key"])]

    # Case pack changes over time (units per case), effective dates
    changes = [("Mayonesa Clásica 250 g", 24, 30, "2023-09-04"), ("Papas Sal Marina 150 g", 20, 24, "2024-03-04"),
               ("Pan Lactal Integral 550 g", 10, 12, "2022-11-07"), ("Ketchup 250 g", 24, 30, "2023-09-04"),
               ("Crackers Clásicas 100 g", 40, 48, "2024-07-01"), ("Pizza Muzzarella 550 g", 12, 10, "2025-03-03"),
               ("Empanadas de Pollo x12 720 g", 10, 8, "2024-05-06")]
    hist = []
    for desc, old, new, when in changes:
        i = df.index[df["description"] == desc][0]
        df.loc[i, "units_per_case"] = old  # the initial one; history below
        hist.append(dict(product_key=df.loc[i, "product_key"], valid_from=START, valid_to=d(when) - timedelta(1),
                         units_per_case=old))
        hist.append(dict(product_key=df.loc[i, "product_key"], valid_from=d(when), valid_to=None, units_per_case=new))
    w["pack_history"] = pd.DataFrame(hist)
    df["net_weight_g"] = df["pack_g"]
    df["shelf_life_days"] = df["family"].map({"CHIPS": 180, "CRACKERS": 270, "SAUCE_JAR": 300, "SAUCE_SACHET": 270,
                                              "FROZEN": 365, "BAKERY": 12})
    w["products"] = df


def units_per_case(w: World, product_key: str, day) -> int:
    h = w.story.setdefault("_pack_cache", {})
    if not h:
        for _, x in w["pack_history"].iterrows():
            h.setdefault(x["product_key"], []).append((x["valid_from"], x["valid_to"], x["units_per_case"]))
    if product_key in h:
        for a, b, u in h[product_key]:
            if a <= day and (b is None or day <= b):
                return u
    return int(w.story["_upc"][product_key])


# ---- Recipes -----------------------------------------------------------------------------------------------------------

def build_recipes(w: World):
    """Versioned formulas: kg of each ingredient per kg of finished product. True validity ranges never overlap; the
    recorded ones do (recorded.py)."""
    r = rng("recipes")
    heads, lines = [], []
    rid = 5000
    for f in C.F:
        base = C.BASE_RECIPES[C.BASE_OF[f["code"]]]
        comp = {k: v for k, v in base}
        for k, v in C.EXTRA.get(f["code"], []):
            comp[k] = comp.get(k, 0) + v
        # version history inside the span
        n_changes = int(r.choice([0, 1, 1, 2, 2, 3]))
        dates = sorted(START + timedelta(days=int(x)) for x in r.integers(60, 1650, n_changes))
        versions = [(d("2019-03-01"), comp)]
        for when in dates:
            new = {k: v * float(r.uniform(0.95, 1.05)) for k, v in versions[-1][1].items()}
            versions.append((when, new))
        if f["code"] == "MAY-CLA":
            v3 = versions[-1][1]
            cheap = dict(v3)
            cheap["RM-HUEV"] = v3["RM-HUEV"] * 0.78
            cheap["RM-VINA"] = v3["RM-VINA"] * 0.80
            cheap["RM-SORB"] = v3["RM-SORB"] * 0.50
            cheap["RM-ACGI"] = v3["RM-ACGI"] * 0.97
            cheap["RM-AGUA"] = v3["RM-AGUA"] + 0.035
            cheap["RM-GXAN"] = v3["RM-GXAN"] * 1.4
            versions = [v for v in versions if v[0] < d("2024-06-03")]
            versions.append((d("2024-06-03"), cheap))
            versions.append((d("2025-02-10"), dict(v3)))
            w.story["recipe_costcut"] = dict(formula="MAY-CLA", start=d("2024-06-03"), end=d("2025-02-09"))
        for i, (when, comp) in enumerate(versions):
            rid += 1
            valid_to = versions[i + 1][0] - timedelta(days=1) if i + 1 < len(versions) else None
            heads.append(dict(recipe_id=rid, formula_code=f["code"], version=i + 1, valid_from=when,
                              valid_to=valid_to, batch_size_kg=1000,
                              cost_cut=(f["code"] == "MAY-CLA" and when == d("2024-06-03"))))
            for j, (rm, q) in enumerate(comp.items()):
                lines.append(dict(recipe_id=rid, line_no=10 * (j + 1), rm_code=rm, kg_per_kg=q))
    w["recipes"] = pd.DataFrame(heads)
    w["recipe_lines"] = pd.DataFrame(lines)


# ---- Suppliers ---------------------------------------------------------------------------------------------------------

def build_suppliers(w: World):
    r = rng("suppliers")
    counts = {"TUBERCULOS": 24, "ACEITES": 6, "HARINAS": 8, "ESPECIAS": 9, "LACTEOS": 10, "CARNES": 12,
              "VARIOS": 32, "PACKAGING": 30, "MRO": 30}
    rows, names = [], set()
    sid = 200100
    for cat, n in counts.items():
        for _ in range(n):
            while True:
                tmpl = C.SUPPLIER_CATEGORY_NAMES[cat][r.integers(len(C.SUPPLIER_CATEGORY_NAMES[cat]))]
                nm = tmpl.format(b=C.COMPANY_WORDS_B[r.integers(len(C.COMPANY_WORDS_B))],
                                 c=C.COMPANY_WORDS_C[r.integers(len(C.COMPANY_WORDS_C))])
                if nm not in names:
                    names.add(nm)
                    break
            legal = "" if nm.startswith(("Establecimiento", "Chacra", "Campo")) else " " + C.LEGAL[r.integers(4)]
            sid += int(r.integers(1, 30))
            prov, city = place(r)
            rows.append(dict(supplier_id=sid, entity_id=sid, supplier_name=nm + legal, tax_id=fake_tax_id(r),
                             category=cat, country="AR", province=prov, city=city,
                             created=d("2019-03-01") if r.random() < 0.6 else d("2019-03-01") + timedelta(
                                 days=int(r.integers(0, 2400))), payment_terms=int(r.choice([0, 15, 30, 45, 60])),
                             status="ACTIVO", origin="ERP"))
    # a few import suppliers (quoted in lb / USD)
    for nm, country in [("Spice Trading Corp.", "US"), ("Andean Seeds Export S.A.C.", "PE"),
                        ("Pacific Packaging Ltd.", "CN")]:
        sid += 17
        rows.append(dict(supplier_id=sid, entity_id=sid, supplier_name=nm, tax_id=None, category="ESPECIAS" if
                         "Spice" in nm else "VARIOS" if "Seeds" in nm else "PACKAGING", country=country,
                         province=None, city=None, created=d("2020-06-01"), payment_terms=60, status="ACTIVO",
                         origin="ERP"))
    df = pd.DataFrame(rows)

    # assign raw materials to supplier entities (1-3 each)
    rm = w["raw_materials"]
    assign = []
    for _, m in rm.iterrows():
        if m["rm_code"] == "RM-AGUA":
            continue
        cat = "PACKAGING" if m["kind"] == "ENVASE" else C.RM_SUPPLIER_CATEGORY[m["category"]]
        pool = df[df["category"] == cat]["supplier_id"].tolist()
        k = 1 if m["category"] in ("AROMAS", "ADITIVOS") else int(r.choice([1, 2, 2, 3]))
        if m["rm_code"] == "RM-PAPA":
            k = 10
        chosen = list(r.choice(pool, min(k, len(pool)), replace=False))
        shares = r.dirichlet(np.ones(len(chosen)) * 2)
        for s, sh in zip(chosen, shares):
            assign.append(dict(rm_code=m["rm_code"], supplier_id=int(s), share=float(sh)))
    # imports: pepper/cumin from the US supplier, chia/flax from Peru
    imp = df[df["country"] != "AR"].set_index("supplier_name")["supplier_id"]
    for rm_code, s in [("RM-COMI", "Spice Trading Corp."), ("RM-CHIA", "Andean Seeds Export S.A.C."),
                       ("RM-LINO", "Andean Seeds Export S.A.C."), ("PK-FSAC", "Pacific Packaging Ltd.")]:
        assign.append(dict(rm_code=rm_code, supplier_id=int(imp[s]), share=0.35))
    a = pd.DataFrame(assign)
    a["share"] = a["share"] / a.groupby("rm_code")["share"].transform("sum")

    # Story 5: the oil supplier whose receipts get inflated, and story 2: the spice supplier that changes owners
    oil_pool = df[df["category"] == "ACEITES"]["supplier_id"].astype(int).tolist()
    a = a[a["rm_code"] != "RM-ACGI"]
    a = pd.concat([a, pd.DataFrame([dict(rm_code="RM-ACGI", supplier_id=s_, share=1.0) for s_ in oil_pool[:3]])],
                  ignore_index=True)
    oil = a[a["rm_code"] == "RM-ACGI"]
    fraud_supplier = int(oil.iloc[0]["supplier_id"])
    a.loc[a["rm_code"] == "RM-ACGI", "share"] = a.loc[a["rm_code"] == "RM-ACGI", "supplier_id"].map(
        lambda s: 0.55 if s == fraud_supplier else 0.45 / max(1, len(oil) - 1))
    df.loc[df["supplier_id"] == fraud_supplier, "supplier_name"] = "Aceitera del Paraná S.A."
    w.story["receiving_fraud"] = dict(supplier_id=fraud_supplier, rm_code="RM-ACGI", plant="P1",
                                      start=d("2023-06-05"), end=END)

    spice_sup = int(df[(df["category"] == "ESPECIAS") & (df["country"] == "AR")].iloc[2]["supplier_id"])
    df.loc[df["supplier_id"] == spice_sup, "supplier_name"] = "Especias del Norte Grande S.R.L."
    # it supplies most of the dried herbs and spices
    spice_rms = ["RM-PIMA", "RM-OREG", "RM-AJIM", "RM-AJOP", "RM-CEBP", "RM-PERE", "RM-ROME"]
    a = a[~((a["rm_code"].isin(spice_rms)) & (a["supplier_id"] == spice_sup))]
    a = pd.concat([a, pd.DataFrame([dict(rm_code=x, supplier_id=spice_sup, share=0.5) for x in spice_rms])])
    a["share"] = a["share"] / a.groupby("rm_code")["share"].transform("sum")
    new_id = int(df["supplier_id"].max()) + 31
    old = df[df["supplier_id"] == spice_sup].iloc[0].to_dict()
    change = d("2024-08-01")
    new = dict(old, supplier_id=new_id, supplier_name="Especias Norte Grande S.A.U.", created=change - timedelta(5),
               payment_terms=30)
    df.loc[len(df)] = new
    w.story["ownership_change"] = dict(old_id=spice_sup, new_id=new_id, tax_id=old["tax_id"], change=change)

    # Story 1: the supplier whose smoked paprika lot carries sesame
    pim = a[(a["rm_code"] == "RM-PIMA") & (a["supplier_id"] != spice_sup)]
    sesame_sup = int(pim.iloc[0]["supplier_id"])
    df.loc[df["supplier_id"] == sesame_sup, "supplier_name"] = "Molinos Especieros Cuyo S.R.L."
    w.story["sesame"] = dict(supplier_id=sesame_sup, rm_code="RM-PIMA")
    w["suppliers"] = df
    w["supplier_rm"] = a.reset_index(drop=True)


# ---- Customers ---------------------------------------------------------------------------------------------------------

def build_customers(w: World, employees: pd.DataFrame):
    r = rng("customers")
    reps = employees[employees["role"] == "VENDEDOR"]
    rows, hier = [], []
    cid = 400

    def new_id():
        nonlocal cid
        cid += int(r.integers(1, 4))
        return cid

    def rep_for(seg):
        pool = reps[reps["department"] == ("EXPORTACION" if seg == "EXPORTACION" else "VENTAS")]
        return int(pool.iloc[r.integers(len(pool))]["employee_id"])

    def created(seg):
        if r.random() < 0.8:
            return d("2019-03-01") - timedelta(days=int(r.integers(0, 3000)))  # migrated
        if r.random() < 0.4:
            return d("2019-03-01") + timedelta(days=int(r.integers(0, 1000)))
        return START + timedelta(days=int(r.integers(0, (END - START).days - 120)))

    chain_hq = {}
    for code, name, stores, bias in C.CHAINS:
        hq = new_id()
        prov, city = place(r, bias)
        chain_hq[code] = hq
        rows.append(dict(customer_id=hq, customer_name=name, segment="SUPERMERCADO", channel="CADENA HQ",
                         currency="ARS", province=prov, city=city, created=d("2019-03-01"), closed=None,
                         tax_id=fake_tax_id(r), sales_rep_id=rep_for("SUPERMERCADO"), chain=code, size=0.0,
                         ships=False, order_every=None, country="AR"))
        rep = rep_for("SUPERMERCADO")
        short = name.split(" S.")[0]
        for s in range(stores):
            prov, city = place(r, bias)
            sid = new_id()
            opened = d("2019-03-01") if r.random() < 0.8 else START + timedelta(days=int(r.integers(0, 1500)))
            closed = None
            if r.random() < 0.05:
                closed = opened + timedelta(days=int(r.integers(200, 1200)))
                if closed >= END:
                    closed = None
            rows.append(dict(customer_id=sid, customer_name=f"{short} - Suc. {city} {s + 1:03d}",
                             segment="SUPERMERCADO", channel="SUCURSAL", currency="ARS", province=prov, city=city,
                             created=opened, closed=closed, tax_id=None, sales_rep_id=rep, chain=code,
                             size=float(r.lognormal(0, 0.45)), ships=True, order_every=int(r.choice([14, 21, 21, 28])),
                             country="AR"))
            hier.append(dict(child_id=sid, parent_id=hq, level="CADENA", valid_from=opened, valid_to=None))

    used = {x["customer_name"] for x in rows}

    def company(kind):
        while True:
            if kind == "FS":
                nm = f"{C.FS_KINDS[r.integers(len(C.FS_KINDS))]} {C.COMPANY_WORDS_C[r.integers(len(C.COMPANY_WORDS_C))]}"
                nm += f" {C.LEGAL[r.integers(len(C.LEGAL))]}" if r.random() < 0.6 else ""
            else:
                nm = (f"{C.COMPANY_WORDS_A[r.integers(len(C.COMPANY_WORDS_A))]} "
                      f"{(C.COMPANY_WORDS_B if r.random() < 0.5 else C.COMPANY_WORDS_C)[r.integers(20)]} "
                      f"{C.LEGAL[r.integers(len(C.LEGAL))]}")
            if nm not in used:
                used.add(nm)
                return nm

    for seg, n, every, size in [("DISTRIBUIDOR", 320, [7, 10, 14], 1.0), ("MAYORISTA", 60, [7, 14], 1.6),
                                ("FOOD SERVICE", 520, [21, 28, 35], 0.35)]:
        for _ in range(n):
            prov, city = place(r)
            c = created(seg)
            closed = None
            if r.random() < (0.3 if seg == "FOOD SERVICE" else 0.12):
                if seg == "FOOD SERVICE" and r.random() < 0.6:  # restaurants that closed in the 2024 recession
                    closed = d("2024-01-15") + timedelta(days=int(r.integers(0, 420)))
                else:
                    closed = max(c, START) + timedelta(days=int(r.integers(120, 1500)))
                if closed >= END or closed <= max(c, START):
                    closed = None
            rows.append(dict(customer_id=new_id(), customer_name=company("FS" if seg == "FOOD SERVICE" else "D"),
                             segment=seg, channel=seg, currency="ARS", province=prov, city=city, created=c,
                             closed=closed, tax_id=fake_tax_id(r), sales_rep_id=rep_for(seg), chain=None,
                             size=float(r.lognormal(0, 0.8)) * size, ships=True, order_every=int(r.choice(every)),
                             country="AR"))
    for cc, country, names in C.EXPORT_CUSTOMERS:
        for nm in names:
            rows.append(dict(customer_id=new_id(), customer_name=nm, segment="EXPORTACION", channel="EXPORTACION",
                             currency="USD", province=None, city=country, created=created("EXPORTACION"),
                             closed=None, tax_id=None, sales_rep_id=rep_for("EXPORTACION"), chain=None,
                             size=float(r.lognormal(0, 0.5)), ships=True, order_every=int(r.choice([45, 60, 75])),
                             country=cc))
    df = pd.DataFrame(rows)
    # US export customers start buying in 2023-08
    df.loc[df["country"] == "US", "created"] = d("2023-08-01")
    df.loc[df["segment"] == "EXPORTACION", "created"] = df.loc[df["segment"] == "EXPORTACION", "created"].map(
        lambda x: min(x, d("2023-08-01")))

    # Story 10: La Estrella acquires Costa Azul. Costa Azul's stores keep their IDs until they are re-created as La
    # Estrella stores; La Estrella's own stores get regional DCs in between.
    acq = d("2025-04-01")
    switch = d("2025-09-15")
    est, costa = chain_hq["LAESTRELLA"], chain_hq["COSTAAZUL"]
    dcs = []
    for region in ("AMBA", "Centro", "Litoral", "Cuyo"):
        dc = new_id()
        dcs.append(dc)
        df.loc[len(df)] = dict(customer_id=dc, customer_name=f"La Estrella CD {region}", segment="SUPERMERCADO",
                               channel="CENTRO DISTRIBUCION", currency="ARS", province=None, city=None, created=acq,
                               closed=None, tax_id=None, sales_rep_id=rep_for("SUPERMERCADO"), chain="LAESTRELLA",
                               size=0.0, ships=False, order_every=None, country="AR")
        hier.append(dict(child_id=dc, parent_id=est, level="CD", valid_from=acq, valid_to=None))
    h = pd.DataFrame(hier)
    est_stores = h[(h["parent_id"] == est) & (h["level"] == "CADENA")].index
    h.loc[est_stores, "valid_to"] = acq - timedelta(days=1)
    moved = []
    for i in est_stores:
        moved.append(dict(child_id=h.loc[i, "child_id"], parent_id=dcs[i % 4], level="CD", valid_from=acq,
                          valid_to=None))
    costa_stores = h[h["parent_id"] == costa]
    h.loc[costa_stores.index, "valid_to"] = switch - timedelta(days=1)
    moved.append(dict(child_id=costa, parent_id=est, level="GRUPO", valid_from=acq, valid_to=None))
    successor = {}
    rep_est = int(df[df["customer_id"].isin(h[h["parent_id"] == est]["child_id"])]["sales_rep_id"].iloc[0])
    for i, (_, s) in enumerate(costa_stores.iterrows()):
        old = df[df["customer_id"] == s["child_id"]].iloc[0].to_dict()
        if old["closed"] is not None and old["closed"] < switch:
            continue
        nid = new_id()
        successor[old["customer_id"]] = nid
        df.loc[len(df)] = dict(old, customer_id=nid, customer_name=old["customer_name"].replace(
            "Hipermercados Costa Azul", "La Estrella") + " (ex Costa Azul)", created=switch, closed=None,
            sales_rep_id=rep_est, chain="LAESTRELLA")
        df.loc[df["customer_id"] == old["customer_id"], "closed"] = switch
        moved.append(dict(child_id=nid, parent_id=dcs[i % 4], level="CD", valid_from=switch, valid_to=None))
    h = pd.concat([h, pd.DataFrame(moved)], ignore_index=True)
    w.story["consolidation"] = dict(acquirer=est, acquired=costa, acquired_on=acq, switch=switch, dcs=dcs,
                                    successor=successor)
    df["customer_id"] = df["customer_id"].astype(int)
    w["customers"] = df
    w["customer_hierarchy"] = h


# ---- Employees ---------------------------------------------------------------------------------------------------------

def build_employees(w: World):
    """Everyone who worked at GATR over the span. Hourly crews per line and shift, plus salaried staff. Turnover
    replaces leavers; P3's staff arrive with the acquisition."""
    r = rng("employees")
    used = set()
    rows = []
    eid = 1000

    def hire(plant, role, dept, start, line=None, shift=None, supervisor=None, hourly=True, crew=None):
        nonlocal eid
        eid += int(r.integers(1, 3))
        rows.append(dict(employee_id=eid, full_name=person(r, used), role=role, department=dept, plant_id=plant,
                         line_id=line, shift=shift, crew=crew, hire_date=start, termination_date=None,
                         supervisor_id=supervisor, hourly=hourly))
        return eid

    def opening(plant):
        p = next(p for p in C.PLANTS if p[0] == plant)
        if plant == "P1":
            return lambda: d("2005-01-01") + timedelta(days=int(r.integers(0, 6200)))
        if plant == "P2":
            return lambda: d("2022-05-02") + timedelta(days=int(r.integers(0, 50)))
        return lambda: d(p[4]) + timedelta(days=int(r.integers(0, 5000)))

    plant_mgr = {}
    for plant in ("P1", "P2", "P3"):
        start = opening(plant)
        plant_mgr[plant] = hire(plant, "GERENTE DE PLANTA", "PRODUCCION", start(), hourly=False)
    # salaried central staff at P1 (HQ)
    hq = opening("P1")
    sales_mgr = hire("P1", "GERENTE COMERCIAL", "VENTAS", hq(), hourly=False)
    for _ in range(14):
        hire("P1", "VENDEDOR", "VENTAS", hq(), supervisor=sales_mgr, hourly=False)
    exp_mgr = hire("P1", "JEFE DE EXPORTACION", "EXPORTACION", hq(), supervisor=sales_mgr, hourly=False)
    for _ in range(3):
        hire("P1", "VENDEDOR", "EXPORTACION", hq(), supervisor=exp_mgr, hourly=False)
    for role, dept, n in [("ANALISTA ADMINISTRATIVO", "ADMINISTRACION", 18), ("COMPRADOR", "COMPRAS", 6),
                          ("PLANIFICADOR", "PLANEAMIENTO", 5), ("ANALISTA DE CALIDAD", "CALIDAD", 6),
                          ("ANALISTA RRHH", "RRHH", 5), ("ANALISTA SISTEMAS", "SISTEMAS", 6)]:
        for _ in range(n):
            hire("P1", role, dept, hq(), hourly=False)

    crews = {}
    for plant in ("P1", "P2", "P3"):
        start = opening(plant)
        mgr = plant_mgr[plant]
        lines = [x for x in C.LINES if x[1] == plant]
        if plant == "P2":
            crew_names = ["A", "B", "C", "D"]  # 12-hour rotation: two crews on, two off
        else:
            crew_names = [s for s, _ in C.SHIFTS[plant]]
        for line in lines:
            line_start = d(line[8])
            for crew in crew_names:
                st = (max(line_start - timedelta(days=30), d("2022-05-02")) if plant == "P2"
                      else start())
                sup = hire(plant, "SUPERVISOR DE PRODUCCION", "PRODUCCION", st, line=line[0], shift=crew,
                           supervisor=mgr, hourly=False, crew=crew)
                n_ops = {"CHIPS": 9, "CRACKERS": 8, "SAUCE_JAR": 9, "SAUCE_SACHET": 7, "FROZEN": 12,
                         "BAKERY": 9}[line[3]]
                ids = [hire(plant, "OPERARIO" if k else "LIDER DE LINEA", "PRODUCCION",
                            st if plant == "P2" else start(), line=line[0], shift=crew, supervisor=sup, crew=crew)
                       for k in range(n_ops)]
                crews[(line[0], crew)] = dict(supervisor=sup, members=ids)
        for role, dept, n in [("TECNICO DE MANTENIMIENTO", "MANTENIMIENTO", {"P1": 18, "P2": 10, "P3": 9}[plant]),
                              ("AUXILIAR DE DEPOSITO", "LOGISTICA", {"P1": 16, "P2": 9, "P3": 8}[plant]),
                              ("RECEPCIONISTA DE MATERIALES", "LOGISTICA", {"P1": 3, "P2": 2, "P3": 2}[plant]),
                              ("TECNICO DE CALIDAD", "CALIDAD", {"P1": 8, "P2": 5, "P3": 4}[plant]),
                              ("AUTOELEVADORISTA", "LOGISTICA", {"P1": 6, "P2": 4, "P3": 3}[plant])]:
            for k in range(n):
                shift = (C.SHIFTS[plant][k % len(C.SHIFTS[plant])][0] if role != "RECEPCIONISTA DE MATERIALES"
                         else C.SHIFTS[plant][0][0])
                hire(plant, role, dept, start(), shift=shift, supervisor=mgr,
                     hourly=role not in ("TECNICO DE CALIDAD",), crew=shift if plant != "P2" else "ABCD"[k % 4])
    emp = pd.DataFrame(rows)

    # turnover: hourly staff leave at ~26%/year and are replaced in the same slot
    out = []
    for _, e in emp.iterrows():
        e = e.to_dict()
        plant_start = {"P1": START, "P2": P2_OPEN, "P3": P3_ACQUIRED}[e["plant_id"]]
        cur = e
        rate = 0.26 if e["hourly"] else 0.08
        t = max(plant_start, cur["hire_date"])
        while True:
            stay = int(r.exponential(365 / rate))
            leave = t + timedelta(days=stay)
            if leave >= END:
                out.append(cur)
                break
            cur["termination_date"] = leave
            out.append(cur)
            eid += int(r.integers(1, 3))
            t = leave + timedelta(days=int(r.integers(5, 40)))
            if t >= END:
                break
            cur = dict(cur, employee_id=eid, full_name=person(r, used), hire_date=t, termination_date=None)
    # seasonal temps (eventuales): summer peak for snacks and sauces, winter peak for frozen
    for year in range(START.year - 1, END.year + 1):
        for plant, lines, start_md, months, n in [("P1", ["L1-01", "L1-03", "L1-04"], "11-03", 4, 42),
                                                  ("P2", ["SUR-01", "SUR-02"], "11-10", 3, 26),
                                                  ("P1", ["L1-05"], "05-05", 3, 14), ("P2", ["SUR-03"], "05-12", 3, 10)]:
            st = d(f"{year}-{start_md}")
            if st < START - timedelta(days=60) or st > END or (plant == "P2" and st < P2_OPEN):
                continue
            for k in range(n):
                line = lines[k % len(lines)]
                crew_list = ["A", "B", "C", "D"] if plant == "P2" else [s_ for s_, _ in C.SHIFTS[plant]]
                crew = crew_list[k % len(crew_list)]
                eid += int(r.integers(1, 3))
                a = st + timedelta(days=int(r.integers(0, 10)))
                b = a + timedelta(days=int(months * 30 + r.integers(-15, 20)))
                out.append(dict(employee_id=eid, full_name=person(r, used), role="OPERARIO", department="PRODUCCION",
                                plant_id=plant, line_id=line, shift=crew, crew=crew, hire_date=a,
                                termination_date=b if b < END else None,
                                supervisor_id=crews[(line, crew)]["supervisor"], hourly=True, temp=True))
    emp = pd.DataFrame(out)
    emp["temp"] = emp["temp"].fillna(False).astype(bool)
    emp["employment_type"] = np.where(emp["temp"], "EVENTUAL", np.where(emp["hourly"], "EFECTIVO", "MENSUALIZADO"))
    # supervisors who left were replaced: re-point subordinates to whoever holds the slot (approximation: keep id)
    emp["legajo"] = emp.apply(lambda e: str(e["employee_id"]) if e["plant_id"] == "P1" else
                              f"S{e['employee_id'] % 10000:04d}" if e["plant_id"] == "P2" else
                              f"D-{e['employee_id'] % 10000:04d}", axis=1)
    w["employees"] = emp
    w.story["crews"] = crews

    # Story 5: the receiving clerk at P1 on the day shift, employed through the whole scheme
    clerks = emp[(emp["plant_id"] == "P1") & (emp["role"] == "RECEPCIONISTA DE MATERIALES")]
    long = clerks[(clerks["hire_date"] <= d("2023-01-01")) & (clerks["termination_date"].isna())]
    if long.empty:
        i = clerks.index[0]
        emp.loc[i, "termination_date"] = None
        emp.loc[i, "hire_date"] = min(emp.loc[i, "hire_date"], d("2019-05-06"))
        long = emp.loc[[i]]
    w.story["receiving_fraud"]["clerk_id"] = int(long.iloc[0]["employee_id"])

    # Story 12: one supervisor on P2's frozen line and the six operators whose overtime they inflate
    sup_row = emp[(emp["line_id"] == "SUR-03") & (emp["role"] == "SUPERVISOR DE PRODUCCION")].iloc[0]
    sup = int(sup_row["employee_id"])
    emp.loc[emp["employee_id"] == sup, ["termination_date"]] = None
    team = emp[(emp["supervisor_id"] == sup) & (emp["role"] == "OPERARIO")]
    team = team[(team["hire_date"] <= d("2024-03-01"))]
    chosen = team.head(6)["employee_id"].astype(int).tolist()
    emp.loc[emp["employee_id"].isin(chosen), "termination_date"] = None
    w["employees"] = emp
    w.story["overtime"] = dict(supervisor_id=sup, crew=sup_row["crew"], members=chosen, start=d("2024-03-04"),
                               end=d("2025-12-19"), line="SUR-03")


# ---- FX and prices -----------------------------------------------------------------------------------------------------

def build_fx(w: World):
    """ARS per USD (official, BNA-style seller rate) on business days, and monthly inflation."""
    r = rng("fx")
    anchors = [("2022-01-03", 103.0), ("2022-06-30", 125.2), ("2022-12-30", 177.1), ("2023-04-28", 222.4),
               ("2023-08-11", 287.0), ("2023-08-14", 365.5), ("2023-11-30", 366.5), ("2023-12-13", 800.0),
               ("2024-06-28", 912.0), ("2024-12-30", 1032.0), ("2025-04-11", 1097.0), ("2025-04-14", 1230.0),
               ("2025-07-31", 1365.0), ("2025-10-24", 1490.0), ("2025-12-30", 1455.0), ("2026-06-30", 1520.0),
               ("2026-09-30", 1585.0)]
    ad = [(d(a) - START).days for a, _ in anchors]
    av = np.log([v for _, v in anchors])
    days = pd.date_range(START, END, freq="D")
    base = np.interp(np.arange(len(days)), ad, av)
    noise = np.cumsum(r.normal(0, 0.0025, len(days)))
    noise -= np.interp(np.arange(len(days)), [0, len(days) - 1], [noise[0], noise[-1]])
    rate = np.exp(base + noise * (days >= pd.Timestamp("2025-04-14")) + noise * 0.3)
    from .common import HOLIDAYS
    biz = [(x.weekday() < 5 and x.date() not in HOLIDAYS) for x in days]
    fx = pd.DataFrame({"rate_date": days.date, "ars_per_usd": np.round(rate, 2), "business_day": biz})
    w["fx_daily"] = fx
    # monthly CPI inflation (fictional, shaped like the period)
    infl = {}
    for m in pd.period_range("2021-12", "2026-09", freq="M"):
        y, mo = m.year, m.month
        if y <= 2022:
            v = 0.052 + 0.012 * (mo >= 7)
        elif y == 2023:
            v = 0.066 if mo < 8 else {8: 0.124, 9: 0.127, 10: 0.083, 11: 0.128, 12: 0.255}[mo]
        elif y == 2024:
            v = {1: 0.206, 2: 0.132, 3: 0.11, 4: 0.088, 5: 0.042}.get(mo, 0.035 - 0.0015 * (mo - 6))
        elif y == 2025:
            v = 0.024 + 0.004 * (mo in (3, 4))
        else:
            v = 0.021
        infl[str(m)] = v
    w.story["inflation"] = infl
    cpi = {}
    level = 100.0
    for k, v in infl.items():
        level *= 1 + v
        cpi[k] = level
    w.story["cpi"] = cpi


def build_master(w: World):
    build_plants(w)
    build_raw_materials(w)
    build_products(w)
    w.story["_upc"] = dict(zip(w["products"]["product_key"], w["products"]["units_per_case"]))
    build_recipes(w)
    build_suppliers(w)
    build_employees(w)
    build_customers(w, w["employees"])
    build_fx(w)
    return w


__all__ = ["World", "build_master", "units_per_case", "ERP_UPGRADE", "P3_LEGACY_HISTORY_FROM"]
