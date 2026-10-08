"""Quality in the true world: release tests on every batch, customer complaints, credit notes and returns, recalls.

Stories planted here: the sesame recall and its real blast radius (1), the cost-cut mayonnaise (3), the freezer's
night-time excursions (6), quarter-end channel stuffing coming back as returns (14), the overfilling checkweigher (15).
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from .common import END, d, rng
from .master import World

EXCURSION = (d("2025-01-14"), d("2025-08-05"))  # FRZ-N2 defrost misconfigured after a maintenance job
RECALL_DAY = d("2023-10-20")
LABEL_RECALL_DAY = d("2025-08-13")

COMPLAINT_TEXT = {
    "CUERPO EXTRAÑO": ["Encontraron un pedazo de plástico dentro del paquete", "Cliente reporta un pelo en el producto",
                       "Apareció un trozo de metal pequeño", "Hay un hilo azul dentro del envase",
                       "Encontré algo duro, parece una piedrita"],
    "SABOR / OLOR": ["El producto tiene gusto rancio", "Olor raro al abrir", "Sabor amargo, distinto al habitual",
                     "Tiene gusto a quemado"],
    "ENVASE": ["Llegaron varios paquetes abiertos", "Frasco con la tapa floja", "Bolsa pinchada, producto blando",
               "Cajas aplastadas en la entrega", "Etiqueta mal pegada"],
    "PESO": ["El paquete parece tener menos producto", "Peso menor al declarado"],
    "TEXTURA": ["Producto blando, no está crocante", "Textura gomosa", "Muy seco y quebradizo"],
    "VENCIMIENTO": ["Llegó producto con poca vida útil", "Fecha de vencimiento ilegible"],
}
STORY_TEXT = {
    "mayo": ["Frasco inflado, la tapa está abombada", "Al abrir el frasco salió con presión y olor ácido",
             "Mayonesa con olor a fermentado", "Sachets hinchados en la caja", "Tapa abombada en varios frascos del lote",
             "El producto burbujea y tiene gusto agrio", "Clientes devuelven frascos con la tapa levantada"],
    "frozen": ["Tiene cristales de hielo, parece que se descongeló", "La pizza llegó con escarcha y blanda",
               "Producto quemado por frío, textura seca", "Las empanadas estaban pegadas entre sí, como recongeladas",
               "Mucho hielo dentro de la bolsa", "Se nota que se cortó la cadena de frío"],
    "sesame": ["Consumidor con alergia al sésamo tuvo una reacción después de comer el producto",
               "Reacción alérgica (sésamo) en un niño, el envase no declara sésamo",
               "Cliente alérgico al sésamo reporta urticaria tras consumir", "Posible presencia de sésamo no declarado",
               "Denuncia de consumidor por reacción alérgica, pide información del lote"],
}


def excursion_nights(r) -> set[date]:
    """Nights (by the date the excursion starts after midnight) when FRZ-N2 warmed up during its defrost."""
    out = set()
    x = EXCURSION[0] + timedelta(days=1)
    while x <= EXCURSION[1]:
        if r.random() < 0.86:
            out.add(x)
        x += timedelta(days=1)
    return out


def build_batch_tests(w: World):
    r = rng("batch_tests")
    B = w["batches"]
    p = w["products"].set_index("product_key")
    recipes = w["recipes"].set_index("recipe_id")
    cheap_ids = set(recipes[recipes["cost_cut"]].index)
    rows = []
    for b in B.itertuples(index=False):
        fam = p.at[b.product_key, "family"]
        g = float(p.at[b.product_key, "pack_g"])
        t0 = b.end_ts
        cheap = b.recipe_id in cheap_ids
        overfill = b.line_id == "SUR-01" and d("2024-10-07") <= b.start_ts.date() <= d("2025-03-14")

        def add(test, value, lo, hi, unit, hours, b=b, t0=t0):
            ok = (lo is None or value >= lo) and (hi is None or value <= hi)
            rows.append((b.batch_id, b.plant_id, b.line_id, b.lot_code, test, round(float(value), 3), lo, hi, unit,
                         t0 + timedelta(hours=hours + float(r.uniform(0, 2))), not ok))

        # net weight: average of a sample, as % of the declared weight
        bias = 0.045 if overfill else 0.012
        add("PESO NETO", g * (1 + bias + float(r.normal(0, 0.006))), round(g * 0.97, 1), round(g * 1.06, 1), "g", 0.5)
        if fam in ("CHIPS", "CRACKERS", "BAKERY"):
            m = {"CHIPS": (1.4, 0.25, 2.0), "CRACKERS": (3.2, 0.4, 4.5), "BAKERY": (36.0, 1.2, 39.0)}[fam]
            add("HUMEDAD", float(r.normal(m[0], m[1])), None, m[2], "%", 1)
        if fam in ("CHIPS", "CRACKERS"):
            add("HERMETICIDAD", float(r.normal(0.6, 0.25)) + (0.0), None, 1.5, "mbar/min", 1)
        if fam in ("SAUCE_JAR", "SAUCE_SACHET"):
            ph = float(r.normal(3.85, 0.09)) + (0.10 if cheap else 0)
            add("PH", ph, None, 4.2, "pH", 1)
            p_micro = 0.024 if cheap else 0.009
            cfu = float(r.lognormal(2.0, 0.8)) if r.random() > p_micro else float(r.uniform(150, 2000))
            add("LEVADURAS Y HONGOS", cfu, None, 100.0, "UFC/g", 72)
            p_seal = 0.02 if cheap else 0.005
            vac = float(r.normal(-0.42, 0.05)) if r.random() > p_seal else float(r.uniform(-0.18, 0.02))
            add("VACIO / SELLADO", vac, None, -0.25, "bar", 2)
        if fam == "FROZEN":
            add("TEMPERATURA NUCLEO", float(r.normal(-21.5, 1.2)), None, -18.0, "°C", 0.2)
            if r.random() < 0.34:
                add("MICROBIOLOGIA (ENTEROBACTERIAS)", float(r.lognormal(1.2, 0.9)), None, 100.0, "UFC/g", 48)
        if fam in ("CHIPS", "BAKERY", "CRACKERS") and r.random() < 0.1:
            add("MICROBIOLOGIA (RECUENTO TOTAL)", float(r.lognormal(4.5, 1.0)), None, 10000.0, "UFC/g", 48)
    T = pd.DataFrame(rows, columns=["batch_id", "plant_id", "line_id", "lot_code", "test", "value", "spec_min",
                                    "spec_max", "unit", "tested_at", "failed"])
    w["batch_tests"] = T


def affected_by_sesame(w: World):
    """Batches that physically consumed the contaminated paprika, at P1 and P2."""
    lots = {x["internal_lot"] for x in w.story["sesame"]["receipts"]}
    c = w["consumption"]
    hit = c[c["internal_lot"].isin(lots)]
    return set(hit["batch_id"].astype(int))


def build_complaints_returns(w: World):
    r = rng("complaints")
    B = w["batches"].set_index("batch_id")
    p = w["products"].set_index("product_key")
    S = w["shipments_true"]
    OL = w["order_lines"]
    cut_ids = set(w["recipes"][w["recipes"]["cost_cut"]]["recipe_id"])
    nights = excursion_nights(rng("freezer"))
    w.story["freezer"] = dict(unit="FRZ-N2", start=EXCURSION[0], end=EXCURSION[1], nights=sorted(nights),
                              wo_cause_day=EXCURSION[0], wo_fix_day=EXCURSION[1])
    night_arr = np.array(sorted(nights), dtype="datetime64[D]")
    sesame_batches = affected_by_sesame(w)
    w.story["sesame"]["batches"] = sorted(sesame_batches)

    S = S.copy()
    S["family"] = p.loc[S["product_key"], "family"].to_numpy()
    S["customer_id"] = OL.loc[S["order_line_idx"], "customer_id"].to_numpy()
    S["segment"] = OL.loc[S["order_line_idx"], "segment"].to_numpy()
    S["recipe_id"] = B.loc[S["batch_id"], "recipe_id"].to_numpy()
    S["location"] = B.loc[S["batch_id"], "location"].to_numpy()
    S["produced"] = B.loc[S["batch_id"], "end_ts"].to_numpy()

    base = {"CHIPS": 1 / 130_000, "CRACKERS": 1 / 160_000, "SAUCE_JAR": 1 / 200_000,
            "SAUCE_SACHET": 1 / 1_300_000, "FROZEN": 1 / 48_000, "BAKERY": 1 / 100_000}
    lam = S["units"].to_numpy() * S["family"].map(base).to_numpy()
    # story 3: cost-cut mayonnaise complains weeks later
    cheap = S["recipe_id"].isin(cut_ids).to_numpy()
    lam_mayo = np.where(cheap, S["units"].to_numpy() * np.where(S["family"] == "SAUCE_SACHET", 1 / 700_000,
                                                                1 / 90_000), 0)
    # story 6: frozen lots that sat in FRZ-N2 through excursion nights
    in_n2 = (S["location"] == "FRZ-N2").to_numpy()
    prod_d = pd.to_datetime(S["produced"]).to_numpy().astype("datetime64[D]")
    ship_d = pd.to_datetime(S["ship_date"]).to_numpy().astype("datetime64[D]")
    exposure = np.searchsorted(night_arr, ship_d, side="right") - np.searchsorted(night_arr, prod_d, side="right")
    exposure = np.where(in_n2, exposure, 0)
    S["exposure_nights"] = exposure
    lam_frz = S["units"].to_numpy() * (1 / 48_000) * 1.6 * np.minimum(exposure, 12) / 4.0
    # story 1: sesame
    ses = S["batch_id"].isin(sesame_batches).to_numpy()
    lam_ses = np.where(ses, S["units"].to_numpy() / 55_000, 0)

    rows = []
    cid = 0
    for lam_, kind_name, delay in [(lam, "base", (3, 45)), (lam_mayo, "mayo", (28, 56)), (lam_frz, "frozen", (5, 40)),
                                   (lam_ses, "sesame", (8, 30))]:
        n = r.poisson(lam_)
        for i in np.flatnonzero(n):
            for _ in range(int(n[i])):
                s = S.iloc[i]
                delay_days = int(r.integers(*delay))
                when = s["ship_date"] + timedelta(days=delay_days)
                if kind_name == "sesame":
                    when = min(when, s["ship_date"] + timedelta(days=60))
                if when > END:
                    continue
                if kind_name == "base":
                    cat = r.choice(list(COMPLAINT_TEXT), p=[0.22, 0.2, 0.25, 0.1, 0.15, 0.08])
                    text = COMPLAINT_TEXT[cat][int(r.integers(len(COMPLAINT_TEXT[cat])))]
                else:
                    cat = {"mayo": "ENVASE" if r.random() < 0.6 else "SABOR / OLOR", "frozen": "TEXTURA",
                           "sesame": "ALERGENOS"}[kind_name]
                    text = STORY_TEXT[kind_name][int(r.integers(len(STORY_TEXT[kind_name])))]
                cid += 1
                rows.append(dict(complaint_key=cid, received=when, customer_id=int(s["customer_id"]),
                                 product_key=s["product_key"], lot_code=s["lot_code"], batch_id=int(s["batch_id"]),
                                 category=cat, text=text, cause=kind_name, ship_date=s["ship_date"],
                                 plant_id=s["plant_id"], units_affected=int(r.integers(1, 24)),
                                 exposure_nights=int(s["exposure_nights"])))
    Cm = pd.DataFrame(rows).sort_values(["received", "complaint_key"]).reset_index(drop=True)
    Cm["complaint_key"] = np.arange(1, len(Cm) + 1)
    w["complaints"] = Cm
    w["_ship_ext"] = S[["order_id", "order_line_idx", "lot_code", "batch_id", "product_key", "units", "ship_date",
                        "warehouse", "plant_id", "customer_id", "exposure_nights"]]

    # ---- Recall (story 1): what the company traced, and what really went out
    hit = S[S["batch_id"].isin(sesame_batches)]
    w.story["sesame"].update(
        true_lots=sorted(hit["lot_code"].unique()), true_customers=sorted(hit["customer_id"].unique().tolist()),
        true_units=int(hit["units"].sum()), recall_day=RECALL_DAY)

    # ---- Credit notes and returns
    rr = []
    crn = 0
    price = OL["unit_price"].to_numpy()
    disc = OL["discount"].to_numpy()
    first_line = {}
    for lot_, cust_, ol_ in zip(S["lot_code"], S["customer_id"], S["order_line_idx"]):
        first_line.setdefault((lot_, cust_), ol_)
    for c in Cm.itertuples(index=False):
        if c.cause == "sesame" or r.random() < 0.45:
            ol = first_line.get((c.lot_code, c.customer_id))
            if ol is None:
                continue
            ol = int(ol)
            crn += 1
            rr.append(dict(credit_key=crn, issued=c.received + timedelta(days=int(r.integers(2, 20))),
                           customer_id=c.customer_id, order_id=int(OL.at[ol, "order_id"]), order_line_idx=ol,
                           product_key=c.product_key, lot_code=c.lot_code, units=c.units_affected,
                           unit_price=float(price[ol]) * (1 - float(disc[ol])), currency=OL.at[ol, "currency"],
                           reason="RECLAMO CALIDAD", physical_return=bool(r.random() < 0.5),
                           complaint_key=c.complaint_key))
    # channel stuffing: what was pushed at quarter end comes back 3-7 weeks later
    pushed = OL[OL["push"]]
    for ol, x in pushed.iterrows():
        if x["units_shipped"] <= 0 or r.random() > 0.8:
            continue
        frac = float(r.uniform(0.35, 0.7))
        crn += 1
        rr.append(dict(credit_key=crn, issued=x["ship_date"] + timedelta(days=int(r.integers(21, 50))),
                       customer_id=int(x["customer_id"]), order_id=int(x["order_id"]), order_line_idx=int(ol),
                       product_key=x["product_key"], lot_code=None, units=int(x["units_shipped"] * frac),
                       unit_price=float(x["unit_price"]) * (1 - float(x["discount"])), currency=x["currency"],
                       reason=r.choice(["SOBRESTOCK", "ROTACION LENTA", "VENCIMIENTO PROXIMO"]),
                       physical_return=True, complaint_key=None))
    # bread comes back unsold from supermarkets (a normal practice), damaged deliveries everywhere
    fam = p.loc[OL["product_key"], "family"].to_numpy()
    bread = np.flatnonzero((fam == "BAKERY") & (OL["segment"].to_numpy() == "SUPERMERCADO") &
                           (OL["units_shipped"].to_numpy() > 0))
    for ol in bread[r.random(len(bread)) < 0.12]:
        x = OL.iloc[ol]
        crn += 1
        rr.append(dict(credit_key=crn, issued=x["ship_date"] + timedelta(days=int(r.integers(6, 12))),
                       customer_id=int(x["customer_id"]), order_id=int(x["order_id"]), order_line_idx=int(ol),
                       product_key=x["product_key"], lot_code=None,
                       units=max(1, int(x["units_shipped"] * float(r.uniform(0.03, 0.12)))),
                       unit_price=float(x["unit_price"]) * (1 - float(x["discount"])), currency=x["currency"],
                       reason="DEVOLUCION PAN VENCIDO", physical_return=True, complaint_key=None))
    shipped = np.flatnonzero(OL["units_shipped"].to_numpy() > 0)
    for ol in shipped[r.random(len(shipped)) < 0.006]:
        x = OL.iloc[ol]
        crn += 1
        rr.append(dict(credit_key=crn, issued=x["ship_date"] + timedelta(days=int(r.integers(1, 10))),
                       customer_id=int(x["customer_id"]), order_id=int(x["order_id"]), order_line_idx=int(ol),
                       product_key=x["product_key"], lot_code=None,
                       units=max(1, int(x["units_shipped"] * float(r.uniform(0.02, 0.2)))),
                       unit_price=float(x["unit_price"]) * (1 - float(x["discount"])), currency=x["currency"],
                       reason="ROTURA EN TRANSPORTE", physical_return=False, complaint_key=None))
    # recall returns: every customer who got a recalled lot returns what they still have
    for x in hit.itertuples(index=False):
        if x.ship_date + timedelta(days=45) < RECALL_DAY:
            continue
        crn += 1
        ol = int(x.order_line_idx)
        rr.append(dict(credit_key=crn, issued=max(RECALL_DAY, x.ship_date) + timedelta(days=int(r.integers(3, 25))),
                       customer_id=int(x.customer_id), order_id=int(x.order_id), order_line_idx=ol,
                       product_key=x.product_key, lot_code=x.lot_code, units=int(x.units * float(r.uniform(0.2, 0.6))),
                       unit_price=float(price[ol]) * (1 - float(disc[ol])), currency=OL.at[ol, "currency"],
                       reason="RETIRO DE MERCADO", physical_return=True, complaint_key=None, recall=True))
    Rt = pd.DataFrame(rr)
    Rt = Rt[Rt["issued"] <= END].sort_values(["issued", "credit_key"]).reset_index(drop=True)
    Rt["credit_key"] = np.arange(1, len(Rt) + 1)
    Rt["amount"] = (Rt["units"] * Rt["unit_price"]).round(2)
    w["credit_notes"] = Rt


def run_quality(w: World):
    build_batch_tests(w)
    build_complaints_returns(w)
