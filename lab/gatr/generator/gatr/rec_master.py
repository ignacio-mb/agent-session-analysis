"""The recorded layer, master data: what the ERP (and its neighbours) hold about plants, lines, equipment, products,
materials, recipes, suppliers, customers, employees, prices and exchange rates."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from .common import END, ERP_UPGRADE, P2_OPEN, P3_GO_LIVE, d, rng
from .mess import comma, excel_serial, iso_date, iso_ts, mojibake, num, strip_accents

ERP_FAMILY = {"CHIPS": "SNACKS-PAPAS", "CRACKERS": "SNACKS-GALLETITAS", "SAUCE_JAR": "SALSAS-FRASCO",
              "SAUCE_SACHET": "SALSAS-SACHET", "FROZEN": "CONGELADOS", "BAKERY": "PANIFICADOS"}
SEGMENT_CODE = {"SUPERMERCADO": "SUP", "DISTRIBUIDOR": "DIS", "MAYORISTA": "MAY", "FOOD SERVICE": "FS",
                "EXPORTACION": "EXP"}


class Rec:
    """Context shared by the recorded-layer builders."""

    def __init__(self, w, reg):
        self.w = w
        self.reg = reg
        self.out: dict[str, pd.DataFrame] = {}
        self.cust_id = lambda i: None if i is None or pd.isna(i) else f"{int(i):06d}"
        p = w["products"]
        self.sku = dict(zip(p["product_key"], p["sku_code"]))
        self.p = p.set_index("product_key")

    def put(self, name, df):
        self.out[name] = df.astype(object).where(pd.notna(df), None)


def build_plants(R: Rec):
    w = R.w
    rows = []
    for _, x in w["plants"].iterrows():
        rows.append(dict(plant_id=x["plant_id"], plant_name=x["plant_name"].upper() if x["plant_id"] != "P2"
                         else "Planta Sur ", city=x["city"], province=x["province"], opened_on=x["opened_date"],
                         erp_go_live=x["erp_go_live"], acquired_on=x["acquired_date"],
                         status="ACTIVA"))
    R.put("plants", pd.DataFrame(rows))


def build_lines(R: Rec):
    w = R.w
    rows = []
    for _, x in w["lines"].iterrows():
        plant = x["plant_id"]
        if plant == "P1":
            cap, uom = x["rated_kg_per_hour"], "KG/H"
        elif plant == "P2":
            # the local team keyed capacity in cases per hour of its reference product
            ref = {"CHIPS": (150, 20), "SAUCE_JAR": (500, 12), "FROZEN": (500, 12), "CRACKERS": (200, 24)}[x["family"]]
            cap, uom = round(x["rated_kg_per_hour"] / (ref[0] / 1000) / ref[1]), "CJ/H"
        else:
            cap, uom = x["rated_kg_per_hour"], "kg/h"
        rows.append(dict(line_id=x["line_id"], plant_id=plant, line_name=x["line_name"],
                         product_family=ERP_FAMILY[x["family"]], rated_capacity=cap, capacity_uom=uom,
                         shifts_per_day=x["shifts_per_day"], shift_hours=x["shift_hours"],
                         commissioned_on=x["start_date"] if x["start_date"] >= d("2019-03-01") else
                         date(1900, 1, 1), legacy_code=x["legacy_name"], status="ACTIVA"))
    R.put("production_lines", pd.DataFrame(rows))
    R.reg.rule("M01", "production_lines", "rated_capacity, capacity_uom", "ERP config per plant",
               "Rated capacity is kg/h at P1 and P3 but cases/h of a reference product at P2 (its local team's "
               "setup); the dictionary says units/hour.").affected = 4
    R.reg.count("M01", 0, 13)


def build_equipment(R: Rec):
    w = R.w
    r = rng("rec_equipment")
    E = w["equipment"].copy()
    n_mig = 0
    rows = []
    for _, x in E.iterrows():
        inst = x["install_date"]
        if inst < d("2019-03-01") and r.random() < 0.7:
            inst = date(1900, 1, 1)
            n_mig += 1
        dec = x["decommission_date"]
        dec_s = iso_date(dec) if dec is not None and not pd.isna(dec) else ("9999-12-31" if r.random() < 0.5 else
                                                                            None)
        rows.append(dict(asset_id=x["asset_id"], line_id=x["line_id"], plant_id=x["plant_id"],
                         asset_type=x["asset_type"], manufacturer=x["manufacturer"], model=x["model"],
                         serial_number=x["serial_no"], install_date=iso_date(inst), decommission_date=dec_s,
                         replaced_by_asset_id=x["replaced_by"], criticality=x["criticality"],
                         status="BAJA" if dec is not None and not pd.isna(dec) else "OPERATIVO"))
    R.put("equipment", pd.DataFrame(rows))
    R.reg.rule("N01", "equipment, products, suppliers, customers, production_lines", "install/launch/created dates",
               "ERP migration (2019)", "Records migrated from the pre-2019 system got 1900-01-01 for unknown dates.")
    R.reg.count("N01", n_mig, len(E))
    R.reg.rule("N02", "equipment, recipes, employees", "decommission_date, effective_to, termination_date",
               "ERP", "Open-ended dates are NULL in some rows and 9999-12-31 in others, in the same column.")


def build_products(R: Rec):
    w = R.w
    r = rng("rec_products")
    p = w["products"]
    rows = []
    mig = 0
    for _, x in p.iterrows():
        if x["sku_code"] is None or pd.isna(x["sku_code"]):
            continue
        launch = x["launch_date"]
        created = max(launch, d("2019-03-01")) if launch >= d("2019-03-01") else d("2019-03-01")
        if x["kind"] == "P3":
            launch = P3_GO_LIVE
            created = P3_GO_LIVE - timedelta(days=int(r.integers(1, 10)))
        if launch < d("2019-03-01") and r.random() < 0.6:
            launch_s = "1900-01-01"
            mig += 1
        else:
            launch_s = iso_date(launch)
        retire = x["retire_date"]
        new_style = created >= ERP_UPGRADE
        if retire is not None and not pd.isna(retire) and retire <= END:
            status = "DISC" if new_style else "D"
            active = "N"
        else:
            status = "ACT" if new_style else "A"
            active = "Y"
        g = x["pack_g"]
        if x["family"] in ("SAUCE_JAR",) and x["kind"] == "P3":
            weight = comma(g / 1000, 3) + " kg"  # keyed by the P3 team after the migration
        elif r.random() < 0.04:
            weight = f"{g}g"
        else:
            weight = str(g)
        alg = x["allergens"] or ""
        style = r.random()
        if alg and style < 0.5:
            alg_s = ", ".join(a for a in alg.split(","))
        elif alg and style < 0.8:
            alg_s = ";".join(a.upper() for a in alg.split(","))
        elif alg:
            alg_s = "contiene: " + " y ".join(alg.split(","))
        else:
            alg_s = None if r.random() < 0.7 else "N/A"
        rows.append(dict(sku_code=x["sku_code"], description=x["description"], brand=x["brand"],
                         family=ERP_FAMILY[x["family"]], pack_size=g, net_weight=weight,
                         units_per_case=x["units_per_case"], launch_date=launch_s,
                         discontinued_date=iso_date(retire) if retire is not None and not pd.isna(retire) else None,
                         status=status, is_active=active, allergens=alg_s,
                         shelf_life_days=x["shelf_life_days"], created_at=iso_ts(datetime.combine(created, datetime.min.time()) + timedelta(hours=float(r.uniform(9, 18)))),
                         updated_at=None))
    # the current units per case: the last change wins
    hist = w["pack_history"]
    for pk, g in hist.groupby("product_key"):
        cur = int(g.sort_values("valid_from").iloc[-1]["units_per_case"])
        code = R.sku[pk]
        for row in rows:
            if row["sku_code"] == code and row["description"] == R.p.at[pk, "description"]:
                row["units_per_case"] = cur
                row["updated_at"] = iso_ts(datetime.combine(g["valid_from"].max(), datetime.min.time()) +
                                           timedelta(hours=10))
    # test and training SKUs
    for code, desc in [("TEST-001", "PRODUCTO PRUEBA NO USAR"), ("ZZZ999", "zzz prueba capacitación"),
                       ("099999", "ARTICULO GENERICO"), ("TEST-P3", "Prueba migracion Delta")]:
        rows.append(dict(sku_code=code, description=desc, brand=None, family="VARIOS", pack_size=0, net_weight="0",
                         units_per_case=1, launch_date="2019-03-01" if "P3" not in code else "2025-05-02",
                         discontinued_date=None, status="A", is_active="Y", allergens=None, shelf_life_days=0,
                         created_at="2019-03-01 10:00:00" if "P3" not in code else "2025-05-02 15:20:00",
                         updated_at=None))
    df = pd.DataFrame(rows)
    R.put("products", df)
    R.reg.count("N01", mig, len(df))
    R.reg.rule("M02", "products", "status", "ERP upgrade 2024-07-01",
               "Product status codes A/D (before the upgrade) and ACT/DISC (products created after it) coexist; "
               "the dictionary documents only A/I/D.")
    R.reg.count("M02", (df["status"].isin(["ACT", "DISC"])).sum(), len(df))
    R.reg.rule("M03", "products", "sku_code", "ERP", "A retired code was reassigned to a new product: sku_code is "
                                                       "not unique (2 codes, 4 rows); joins without date logic mix "
                                                       "them.")
    R.reg.count("M03", 4, len(df))
    R.reg.rule("M04", "products", "net_weight", "ERP / P3 team", "net_weight is grams as text, sometimes '150g', "
                                                                   "and kilograms with a decimal comma ('0,5 kg') "
                                                                   "for P3's sauces.")
    R.reg.count("M04", (~df["net_weight"].astype(str).str.fullmatch(r"\d+")).sum(), len(df))
    R.reg.rule("M05", "products", "allergens", "ERP free text", "Allergens written three ways (comma list, upper "
                                                                 "case with ';', 'contiene: x y z'); none is NULL or "
                                                                 "'N/A'.")
    R.reg.rule("T01", "products, customers, suppliers, sales_orders, production_orders", "*", "Training / go-live",
               "Test and training records mixed in: TEST-001, ZZZ999, CLIENTE PRUEBA SA, PROVEEDOR PRUEBA, orders "
               "marked PRUEBA during the P3 go-live.")
    R.reg.count("T01", 4, len(df))

    # unit conversions, case pack history (one change never recorded)
    conv = []
    for _, x in df.iterrows():
        if x["sku_code"].startswith(("TEST", "ZZZ")):
            continue
        conv.append(dict(sku_code=x["sku_code"], from_uom="CJ", to_uom="UN", factor=x["units_per_case"],
                         valid_from="2019-03-01", valid_to=None))
    missing = None
    for pk, g in hist.groupby("product_key"):
        g = g.sort_values("valid_from")
        code = R.sku[pk]
        if "Empanadas de Pollo" in R.p.at[pk, "description"]:
            missing = code
            continue
        for c in conv:
            if c["sku_code"] == code:
                c["valid_from"] = iso_date(g.iloc[-1]["valid_from"])
        conv.append(dict(sku_code=code, from_uom="CJ", to_uom="UN", factor=int(g.iloc[0]["units_per_case"]),
                         valid_from="2019-03-01", valid_to=iso_date(g.iloc[0]["valid_to"])))
    # some conversions keyed in kilograms per case too
    for _, x in df.sample(60, random_state=7).iterrows():
        if x["sku_code"].startswith(("TEST", "ZZZ")):
            continue
        conv.append(dict(sku_code=x["sku_code"], from_uom="CJ", to_uom="KG",
                         factor=round(float(x["pack_size"]) * float(x["units_per_case"]) / 1000, 3),
                         valid_from="2019-03-01", valid_to=None))
    R.put("product_uom_conversions", pd.DataFrame(conv))
    R.reg.rule("U01", "product_uom_conversions, sales_order_lines", "factor", "ERP",
               "Case pack sizes changed for 7 SKUs; the conversion history has 6 of them. The change for "
               f"{missing} (Empanadas de Pollo x12, 10 -> 8 per case, 2024-05-06) was overwritten: its only "
               "CJ->UN factor is the current one, valid since 2019.")
    R.reg.count("U01", 1, 7)


def build_raw_materials(R: Rec):
    w = R.w
    r = rng("rec_rm")
    rm = w["raw_materials"]
    fx = w["fx_daily"]["ars_per_usd"].iloc[-1]
    rows = []
    flags = {"gluten": "contiene_gluten", "leche": "contiene_leche", "huevo": "contiene_huevo",
             "soja": "contiene_soja", "sésamo": "contiene_sesamo", "maní": "contiene_mani",
             "mostaza": "contiene_mostaza", "pescado": "contiene_pescado", "frutos secos": "contiene_frutos_secos"}
    for _, x in rm.iterrows():
        if x["rm_code"] == "RM-AGUA":
            continue  # water is consumed in batches but was never set up as a material
        row = dict(material_code=x["rm_code"], description=x["description"],
                   category=x["category"] if r.random() > 0.15 else x["category"].title(),
                   base_uom={"kg": r.choice(["KG", "kg", "Kg"], p=[0.6, 0.3, 0.1]), "un": "UN"}[x["uom"]],
                   storage_condition=x["storage"], shelf_life=f"{x['shelf_life_days']} días",
                   std_cost=comma(x["std_cost_usd"] * fx, 2), std_cost_currency="ARS",
                   is_active="true" if r.random() > 0.03 else "false")
        for a, col in flags.items():
            row[col] = "X" if a in (x["allergens"] or "") else None
        if x["rm_code"] == "RM-COMI":
            row["base_uom"] = "LB"  # bought from the US supplier by the pound
        rows.append(row)
    R.put("raw_materials", pd.DataFrame(rows))
    R.reg.rule("B01", "raw_materials", "contiene_*", "ERP", "Allergen flags are 'X' or NULL.")
    R.reg.rule("U02", "raw_materials, goods_receipts, recipe_lines", "base_uom, uom", "ERP",
               "Unit spellings KG/kg/Kg; cumin (RM-COMI) is set up and received in LB while recipes use g; spices "
               "in recipe lines are in g, the rest in kg.")
    R.reg.rule("F01", "raw_materials", "shelf_life, std_cost", "ERP", "Numbers stored as text with units ('180 "
                                                                       "días') and decimal commas.")
    R.reg.rule("O01", "batch_consumption", "material_code", "ERP backflush",
               "RM-AGUA (process water) is consumed in every batch that uses it but is not in raw_materials.")


def build_recipes(R: Rec):
    w = R.w
    r = rng("rec_recipes")
    H = w["recipes"]
    L = w["recipe_lines"]
    rm_uom = w["raw_materials"].set_index("rm_code")
    emps = w["employees"]
    approvers = emps[emps["role"] == "ANALISTA DE CALIDAD"]["employee_id"].tolist()
    heads, lines = [], []
    overlap = gap = 0
    for f, g in H.groupby("formula_code"):
        g = g.sort_values("version")
        for i, (_, x) in enumerate(g.iterrows()):
            vf, vt = x["valid_from"], x["valid_to"]
            vf_s = iso_date(vf)
            vt_s = iso_date(vt) if vt is not None and not pd.isna(vt) else "9999-12-31"
            nxt = g.iloc[i + 1] if i + 1 < len(g) else None
            if nxt is not None:
                u = r.random()
                if u < 0.25:
                    vt_s = "9999-12-31"  # never closed when the next version was released: overlap
                    overlap += 1
                elif u < 0.4:
                    vt_s = iso_date(vt - timedelta(days=int(r.integers(10, 40))))  # closed early: a gap
                    gap += 1
            heads.append(dict(recipe_id=x["recipe_id"], product_code=f"F-{f}", version=x["version"],
                              effective_from=vf_s if vf >= d("2019-03-01") else "2019-03-01", effective_to=vt_s,
                              batch_size_kg=x["batch_size_kg"], status="APROBADA",
                              approved_by=approvers[int(r.integers(len(approvers)))],
                              notes="Optimización de costos (aprobado Compras/Calidad)" if x["cost_cut"] else
                              ("Restituye fórmula anterior" if f == "MAY-CLA" and vf == d("2025-02-10") else None),
                              created_at=iso_ts(datetime.combine(min(vf, END), datetime.min.time()) -
                                                timedelta(days=int(r.integers(3, 20)), hours=-11))))
        for _, x in L[L["recipe_id"].isin(g["recipe_id"])].iterrows():
            q = x["kg_per_kg"] * 1000
            cat = rm_uom.at[x["rm_code"], "category"] if x["rm_code"] in rm_uom.index else ""
            if cat in ("ESPECIAS", "ADITIVOS", "AROMAS"):
                qty, uom = round(q * 1000, 1), "g"
            else:
                qty, uom = round(q, 3), "kg"
            lines.append(dict(recipe_id=x["recipe_id"], line_no=x["line_no"], material_code=x["rm_code"],
                              quantity=qty, uom=uom, scrap_pct=0 if r.random() < 0.8 else
                              round(float(r.uniform(0.5, 3)), 1)))
    R.put("recipes", pd.DataFrame(heads))
    R.put("recipe_lines", pd.DataFrame(lines))
    R.reg.rule("V01", "recipes", "effective_from, effective_to", "ERP", "Some versions were never closed when the "
                                                                        "next was released (overlapping ranges, "
                                                                        "9999-12-31), others were closed early "
                                                                        "(gaps with no version in force).")
    R.reg.count("V01", overlap + gap, len(heads))


def build_suppliers(R: Rec):
    """ERP supplier master: P2's purchasing created its own records for suppliers it shared with P1; the spice
    supplier's new owner got a new id; records created through the supplier portal have mojibake."""
    w = R.w
    r = rng("rec_suppliers")
    S = w["suppliers"]
    own = w.story["ownership_change"]
    rows = []
    dup_map = {}
    portal = 0
    for _, x in S.iterrows():
        created = x["created"]
        cs = iso_ts(datetime.combine(created, datetime.min.time()) + timedelta(hours=10)) if created >= d(
            "2019-03-02") or r.random() > 0.4 else "1900-01-01 00:00:00"
        name = x["supplier_name"]
        tax = x["tax_id"]
        if x["supplier_id"] == own["new_id"]:
            tax = tax.replace("-", "")  # keyed without dashes by the new owner's onboarding
        elif tax and r.random() < 0.12:
            tax = tax.replace("-", "")
        if created >= d("2024-01-01") and r.random() < 0.8:
            name = mojibake(name)
            portal += 1
        rows.append(dict(supplier_id=int(x["supplier_id"]), supplier_name=name, tax_id=tax, country=x["country"],
                         province=x["province"], city=x["city"], category=x["category"],
                         payment_terms_days=x["payment_terms"], created_at=cs,
                         status="ACTIVO" if x["supplier_id"] != own["old_id"] else "INACTIVO",
                         is_deleted="false" if r.random() > 0.3 else None))
    # P2 duplicates: same tax id, a variant of the name, created when P2 opened
    shared = S[S["category"].isin(["PACKAGING", "VARIOS", "ACEITES", "HARINAS", "LACTEOS"]) & S["tax_id"].notna()]
    shared = shared.sample(14, random_state=11)
    nid = 300_100
    for _, x in shared.iterrows():
        nid += int(r.integers(3, 40))
        variant = r.choice(["upper", "noacc", "legal", "space"])
        name = x["supplier_name"]
        if variant == "upper":
            name = name.upper()
        elif variant == "noacc":
            name = strip_accents(name).replace(" S.A.", " SA").replace(" S.R.L.", " SRL")
        elif variant == "legal":
            name = name.replace(" S.A.", " Sociedad Anónima").replace(" S.R.L.", " Soc. de Resp. Ltda.")
        else:
            name = name + "  "
        rows.append(dict(supplier_id=nid, supplier_name=name, tax_id=x["tax_id"].replace("-", ""),
                         country="AR", province=x["province"], city=x["city"], category=x["category"],
                         payment_terms_days=x["payment_terms"],
                         created_at=iso_ts(datetime.combine(P2_OPEN - timedelta(days=int(r.integers(5, 40))),
                                                            datetime.min.time()) + timedelta(hours=14)),
                         status="ACTIVO", is_deleted=None))
        dup_map[int(x["supplier_id"])] = nid
    rows.append(dict(supplier_id=299_999, supplier_name="PROVEEDOR PRUEBA", tax_id="30-00000000-0", country="AR",
                     province=None, city=None, category="VARIOS", payment_terms_days=0,
                     created_at="2019-02-20 09:00:00", status="ACTIVO", is_deleted="false"))
    R.put("suppliers", pd.DataFrame(rows))
    R.dup_supplier = dup_map
    R.reg.rule("D01", "suppliers, purchase_orders, goods_receipts", "supplier_id", "P2 purchasing (2022)",
               "P2's buyers created their own records for 14 suppliers P1 already had: a name variant (upper case, "
               "no accents, legal form spelled out, trailing spaces), the same tax id without dashes; P2's orders "
               "and receipts use those ids.")
    R.reg.count("D01", 14, len(rows))
    R.reg.rule("D02", "suppliers", "supplier_id, supplier_name, tax_id", "Supplier onboarding (2024-08)",
               "A supplier sold to a new owner got a new id and a slightly different name; its tax id is the same "
               "but keyed without dashes.")
    R.reg.count("D02", 1, len(rows))
    R.reg.rule("E01", "suppliers, customer_complaints, shipments", "names, free text", "Supplier portal export / CRM",
               "UTF-8 text decoded as Latin-1 in one export path: ñ -> Ã±, á -> Ã¡.")
    R.reg.count("E01", portal, len(rows))
    R.reg.rule("W01", "suppliers, customers", "supplier_name, customer_name", "ERP",
               "Trailing whitespace and inconsistent casing in names.")


def build_customers(R: Rec):
    w = R.w
    r = rng("rec_customers")
    Cu = w["customers"]
    rows = []
    n_mig = 0
    for _, x in Cu.iterrows():
        created = x["created"]
        if created < d("2019-03-01"):
            cs = "1900-01-01" if r.random() < 0.65 else "2019-03-01"
            n_mig += cs == "1900-01-01"
        else:
            cs = iso_date(created)
        closed = x["closed"]
        name = x["customer_name"]
        if r.random() < 0.05:
            name = name.upper()
        if r.random() < 0.03:
            name = name + " "
        seg = SEGMENT_CODE.get(x["segment"], x["segment"])
        if x["channel"] == "EX DELTA":
            seg = "DISTRIB"  # keyed by the migration consultants, not with the ERP's codes
        rows.append(dict(customer_id=R.cust_id(x["customer_id"]), customer_name=name, segment=seg,
                         channel=x["channel"], currency=x["currency"], tax_id=x["tax_id"], province=x["province"],
                         city=x["city"], country=x["country"], sales_rep_id=x["sales_rep_id"], created_on=cs,
                         closed_on=iso_date(closed) if closed is not None and not pd.isna(closed) else None,
                         status="INACTIVO" if closed is not None and not pd.isna(closed) else "ACTIVO",
                         is_deleted="0"))
    # test customers, and a few customers deleted but still referenced
    for i, (nm, seg, cur) in enumerate([("CLIENTE PRUEBA SA", "SUP", "ARS"), ("PRUEBA EXPO", "EXP", "USD"),
                                        ("Cliente Prueba Delta", "DISTRIB", "ARS")]):
        rows.append(dict(customer_id=f"{999990 + i:06d}", customer_name=nm, segment=seg, channel=None, currency=cur,
                         tax_id=None, province=None, city=None, country="AR", sales_rep_id=None,
                         created_on="2019-03-01" if i < 2 else "2025-05-02", closed_on=None, status="ACTIVO",
                         is_deleted="0"))
    df = pd.DataFrame(rows)
    dele = df.sample(25, random_state=5).index
    df.loc[dele, "is_deleted"] = "1"
    R.put("customers", df)
    R.reg.count("N01", n_mig, len(df))
    R.reg.rule("S01", "customers, sales_orders, production_batches, returns", "is_deleted, status, deleted_at",
               "ERP", "Soft deletes not applied consistently: 25 customers flagged is_deleted=1 still have orders "
                      "after the flag; annulled orders (ANUL) keep active lines; deleted batches keep "
                      "consumption.")
    R.reg.count("S01", 25, len(df))

    H = w["customer_hierarchy"]
    hr = []
    for _, x in H.iterrows():
        hr.append(dict(child_customer_id=R.cust_id(x["child_id"]), parent_customer_id=R.cust_id(x["parent_id"]),
                       relation_type=x["level"], valid_from=iso_date(x["valid_from"]) if x["valid_from"] >= d(
                           "2019-03-01") else "2019-03-01",
                       valid_to=iso_date(x["valid_to"]) if x["valid_to"] is not None and not pd.isna(x["valid_to"])
                       else None, updated_by="migracion" if x["valid_from"] < d("2019-03-02") else
                       "jlopez" if x["valid_from"] < d("2025-01-01") else "consultora.cd"))
    R.put("customer_hierarchy", pd.DataFrame(hr))
    R.reg.rule("H01", "customer_hierarchy", "parent_customer_id, relation_type", "ERP / consultants (2025)",
               "Before 2025-04-01 a store's parent is its chain (CADENA). From then, La Estrella's stores point to a "
               "regional distribution centre (CD) whose parent is the chain, and the acquired chain's HQ points to "
               "La Estrella (GRUPO): a one-level rollup by parent changes meaning.")


def build_employees(R: Rec):
    w = R.w
    r = rng("rec_employees")
    E = w["employees"]
    rows = []
    missing_term = 0
    for _, x in E.iterrows():
        term = x["termination_date"]
        if term is not None and not pd.isna(term):
            if r.random() < 0.06:
                term_s = None  # left, never processed
                missing_term += 1
            else:
                term_s = iso_date(term)
        else:
            term_s = None if r.random() < 0.7 else "9999-12-31"
        hire = x["hire_date"]
        rows.append(dict(employee_id=int(x["employee_id"]), legajo=x["legajo"], full_name=x["full_name"],
                         role=x["role"], department=x["department"], plant_id=x["plant_id"], line_id=x["line_id"],
                         crew=x["crew"], hire_date=iso_date(hire) if hire >= d("2019-03-01") or r.random() < 0.5
                         else "1900-01-01", termination_date=term_s,
                         supervisor_id=int(x["supervisor_id"]) if not pd.isna(x["supervisor_id"]) else None,
                         employment_type=x["employment_type"]))
    R.put("employees", pd.DataFrame(rows))
    R.reg.rule("S02", "employees", "termination_date", "HR process",
               "About 6% of people who left have no termination_date (still look active).")
    R.reg.count("S02", missing_term, len(rows))


def build_prices(R: Rec):
    """Price lists are uploaded from Excel: dates as Excel serials, prices sometimes with decimal commas."""
    w = R.w
    r = rng("rec_prices")
    Hh = w["price_list_headers"].set_index("price_list_id")
    Lp = w["price_list_lines"]
    p = w["products"].set_index("product_key")
    rows = []
    comma_lists = set(r.choice(Hh.index.to_numpy(), 30, replace=False))
    # next list per segment, to fill effective_to the way the uploads did (sometimes)
    nxt = {}
    for _, g in Hh.reset_index().groupby("segment"):
        g = g.sort_values("valid_from_recorded")
        vals = g["valid_from_recorded"].tolist()
        for i, lid in enumerate(g["price_list_id"]):
            nxt[lid] = vals[i + 1] - timedelta(days=1) if i + 1 < len(vals) else None
    users = ["mgarcia", "pfernandez", "comercial01", "lsuarez"]
    for lid, g in Lp.groupby("price_list_id"):
        h = Hh.loc[lid]
        user = users[int(r.integers(len(users)))]
        with_to = r.random() < 0.6
        for pk, price in zip(g["product_key"], g["unit_price"]):
            code = p.at[pk, "sku_code"]
            if code is None or pd.isna(code):
                continue
            launch, retire = p.at[pk, "launch_date"], p.at[pk, "retire_date"]
            vf = h["valid_from_recorded"]
            if launch > vf + timedelta(days=60) or (retire is not None and not pd.isna(retire) and retire < vf):
                continue
            rows.append(dict(price_list_id=int(lid), segment=h["segment"], currency=h["currency"], sku_code=code,
                             unit_price=comma(price, 2) if lid in comma_lists else num(price, 4),
                             effective_from=excel_serial(vf),
                             effective_to=excel_serial(nxt[lid]) if with_to and nxt[lid] else None,
                             created_by=user, created_at=iso_ts(datetime.combine(h["created"], datetime.min.time())
                                                                + timedelta(hours=16))))
    df = pd.DataFrame(rows)
    R.put("price_lists", df)
    R.reg.rule("F02", "price_lists", "effective_from, effective_to", "Excel upload",
               "Dates are Excel serial numbers (days since 1899-12-30).")
    R.reg.count("F02", len(df), len(df))
    R.reg.rule("F03", "price_lists", "unit_price", "Excel upload", "30 lists were uploaded from a Spanish-locale "
                                                                    "sheet: unit_price has a decimal comma.")
    R.reg.count("F03", df["unit_price"].astype(str).str.contains(",").sum(), len(df))


def build_fx(R: Rec):
    w = R.w
    r = rng("rec_fx")
    fx = w["fx_daily"]
    biz = fx[fx["business_day"]]
    rows = []
    fat = {d("2023-05-17"): lambda v: v * 10, d("2024-09-04"): lambda v: v / 10, d("2025-02-11"): lambda v: v * 10,
           d("2022-08-23"): lambda v: float(str(int(v)) [::-1]) if v >= 100 else v}
    for x in biz.itertuples(index=False):
        v = x.ars_per_usd
        if x.rate_date in fat:
            v = round(fat[x.rate_date](v), 2)
        rows.append(dict(rate_date=iso_date(x.rate_date), currency_from="USD", currency_to="ARS", rate=v,
                         source="BNA vendedor"))
    # a few days loaded twice (a correction appended rather than replacing)
    for i in r.choice(len(rows), 6, replace=False):
        rr = dict(rows[i])
        rr["rate"] = round(rr["rate"] * float(r.uniform(0.995, 1.005)), 2)
        rr["source"] = "BNA vendedor (corr.)"
        rows.append(rr)
    df = pd.DataFrame(rows).sort_values("rate_date").reset_index(drop=True)
    R.put("fx_rates", df)
    R.reg.rule("X01", "fx_rates", "rate_date", "Treasury load", "Only business days: no rates on weekends and "
                                                                 "Argentine holidays (a naive join on the order date "
                                                                 "loses those days).")
    R.reg.count("X01", len(fx) - len(biz), len(fx))
    R.reg.rule("X02", "fx_rates", "rate", "Treasury load (manual)",
               "Fat-fingered rates: 2022-08-23 digits reversed, 2023-05-17 x10, 2024-09-04 /10, 2025-02-11 x10.")
    R.reg.count("X02", 4, len(df))
    R.reg.rule("X03", "fx_rates", "rate_date", "Treasury load", "6 dates have two rows (a correction appended).")
    R.reg.count("X03", 6, len(df))


def build_master_recorded(R: Rec):
    build_plants(R)
    build_lines(R)
    build_equipment(R)
    build_products(R)
    build_raw_materials(R)
    build_recipes(R)
    build_suppliers(R)
    build_customers(R)
    build_employees(R)
    build_prices(R)
    build_fx(R)
