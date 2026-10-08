"""Plant floor in the true world: maintenance work orders, which shifts were staffed and who clocked in, energy
meters, and cold-storage temperatures every five minutes.

Stories planted here: the line whose downtime isn't logged (4: its breakdowns are all in maintenance), the freezer's
night-time defrost excursions (6), the supervisor's inflated overtime (12)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from . import catalog as C
from .common import END, HOLIDAYS, P2_OPEN, P3_ACQUIRED, START, in_shutdown, rng
from .master import World

LINE_KW = {"CHIPS": 820, "CRACKERS": 610, "SAUCE_JAR": 260, "SAUCE_SACHET": 190, "FROZEN": 720, "BAKERY": 540}
BASE_KW = {"P1": 950, "P2": 620, "P3": 430}
BREAKDOWN_TYPES = {"FALLA MECANICA": 0.85, "FALLA ELECTRICA": 0.9, "FALLA DE SERVICIOS (VAPOR/AIRE/FRIO)": 0.7}


def shift_bounds(plant, day: date):
    """[(shift, start, end)] of a plant's production day."""
    out = []
    n = len(C.SHIFTS[plant])
    length = 24 // n
    for s, h in C.SHIFTS[plant]:
        st = datetime.combine(day, datetime.min.time()) + timedelta(hours=h)
        out.append((s, st, st + timedelta(hours=length)))
    return out


def p2_crew(day: date, shift: str) -> str:
    """P2's 4x4 rotation: each crew works two days, then two nights, then four days off."""
    k = (day - P2_OPEN).days % 8
    for i, crew in enumerate("ABCD"):
        pos = (k - 2 * i) % 8
        if shift == "D" and pos in (0, 1):
            return crew
        if shift == "N" and pos in (2, 3):
            return crew
    return "A"


def build_maintenance(w: World):
    r = rng("maintenance")
    DT = w["downtime_true"]
    eq = w["equipment"]
    emps = w["employees"]
    techs = {pl: emps[(emps["plant_id"] == pl) & (emps["role"] == "TECNICO DE MANTENIMIENTO")] for pl in
             ("P1", "P2", "P3")}
    lines = w["lines"].set_index("line_id")
    rows = []
    wo = 60_000

    tech_cache = {}

    def tech(plant, day):
        key = (plant, day)
        if key not in tech_cache:
            t = techs[plant]
            tech_cache[key] = t[(t["hire_date"] <= day) & (t["termination_date"].isna() |
                                                           (t["termination_date"] >= day))]["employee_id"].tolist()
        ids = tech_cache[key]
        return int(ids[r.integers(len(ids))]) if ids else None

    def _unused(plant, day):
        t = techs[plant]
        t = t[(t["hire_date"] <= day) & (t["termination_date"].isna() | (t["termination_date"] >= day))]
        return int(t.iloc[r.integers(len(t))]["employee_id"]) if len(t) else None

    eq_by_line = {k: g for k, g in eq.groupby("line_id")}

    def asset_on(line_id, day, kinds=None):
        a = eq_by_line[line_id]
        a = a[(a["install_date"] <= day) & (a["decommission_date"].isna() | (a["decommission_date"] >= day))]
        if kinds:
            a2 = a[a["asset_type"].isin(kinds)]
            a = a2 if len(a2) else a
        return a.iloc[r.integers(len(a))]["asset_id"]

    br = DT[DT["reason"].isin(list(BREAKDOWN_TYPES)) & (DT["minutes"] >= 20)]
    for x in br.itertuples(index=False):
        if r.random() > BREAKDOWN_TYPES[x.reason]:
            continue
        plant = lines.at[x.line_id, "plant_id"]
        st = pd.Timestamp(x.start_ts).to_pydatetime()
        wo += 1
        rep = st + timedelta(minutes=float(r.uniform(2, 15)))
        start = rep + timedelta(minutes=float(r.uniform(3, 25)))
        end = st + timedelta(minutes=x.minutes + float(r.uniform(-8, 25)))
        end = max(end, start + timedelta(minutes=5))
        rows.append(dict(wo_id=wo, kind="CORRECTIVO", plant_id=plant, line_id=x.line_id,
                         asset_id=asset_on(x.line_id, st.date()), reported_at=rep, started_at=start,
                         completed_at=end, true_downtime_min=x.minutes,
                         tech_estimate_min=round(x.minutes * float(r.uniform(0.75, 1.3))),
                         description=x.reason.title(), technician_id=tech(plant, st.date()), priority=1 if
                         x.minutes > 120 else 2))
    # preventive maintenance on every asset, every 2-6 weeks, on Sundays or in shutdowns
    for a in eq.itertuples(index=False):
        plant = a.plant_id
        first = max(START, a.install_date, {"P1": START, "P2": P2_OPEN, "P3": P3_ACQUIRED}[plant])
        last = END if a.decommission_date is None or pd.isna(a.decommission_date) else a.decommission_date
        every = {"A": 14, "B": 28, "C": 42}[a.criticality]
        t = first + timedelta(days=int(r.integers(0, every)))
        while t <= last:
            day = t + timedelta(days=(6 - t.weekday()))
            st = datetime.combine(day, datetime.min.time()) + timedelta(hours=float(r.uniform(7, 12)))
            dur = float(r.lognormal(np.log(110), 0.4))
            wo += 1
            rows.append(dict(wo_id=wo, kind="PREVENTIVO", plant_id=plant, line_id=a.line_id, asset_id=a.asset_id,
                             reported_at=st - timedelta(days=7), started_at=st, completed_at=st + timedelta(minutes=dur),
                             true_downtime_min=0, tech_estimate_min=round(dur), description="Plan preventivo",
                             technician_id=tech(plant, day), priority=3))
            t += timedelta(days=every)
    # special jobs behind the stories and red herrings
    su = w["storage_units"].set_index("unit_id")
    specials = [("FRZ-N2", "2025-01-14 09:40", 260, "Reemplazo de controlador de deshielo cámara N°2",
                 "CORRECTIVO"),
                ("FRZ-N2", "2025-08-05 10:15", 95, "Ajuste de parámetros de ciclo de deshielo (reclamo calidad)",
                 "CORRECTIVO"),
                ("CH-S1", "2024-05-20 08:30", 75, "Calibración de sensor de temperatura (desvío +1,2 °C)",
                 "PREVENTIVO"),
                ("SUR-01", "2024-10-07 07:30", 480, "Instalación controladora de peso nueva", "MEJORA"),
                ("SUR-01", "2025-03-14 08:00", 60, "Ajuste de tara controladora de peso (auditoría de calidad)",
                 "CORRECTIVO")]
    for target, when, minutes, desc, kind in specials:
        wo += 1
        st = datetime.fromisoformat(when)
        if target in su.index:
            plant, line, asset = su.at[target, "plant_id"], None, su.at[target, "asset_id"]
        else:
            plant, line = lines.at[target, "plant_id"], target
            asset = asset_on(target, st.date(), ["Controladora de peso"])
        rows.append(dict(wo_id=wo, kind=kind, plant_id=plant, line_id=line, asset_id=asset, reported_at=st -
                         timedelta(hours=3), started_at=st, completed_at=st + timedelta(minutes=minutes),
                         true_downtime_min=0, tech_estimate_min=minutes, description=desc,
                         technician_id=tech(plant, st.date()), priority=1, special=target))
    WO = pd.DataFrame(rows).sort_values("reported_at").reset_index(drop=True)
    w["work_orders"] = WO


def staffed_shifts(w: World):
    """Each (line, day, shift) where any batch, changeover or stop fell inside the shift."""
    B = w["batches"]
    lines = w["lines"].set_index("line_id")
    out = set()
    for b in B.itertuples(index=False):
        plant = b.plant_id
        t = pd.Timestamp(b.start_ts).to_pydatetime()
        end = pd.Timestamp(b.end_ts).to_pydatetime()
        first_h = C.SHIFTS[plant][0][1]
        while t < end:
            day = (t - timedelta(hours=first_h)).date()
            for s, st, en in shift_bounds(plant, day):
                if st <= t < en:
                    out.add((b.line_id, day, s))
                    t = en
                    break
            else:
                t += timedelta(hours=1)
    S = pd.DataFrame(sorted(out), columns=["line_id", "day", "shift"])
    S["plant_id"] = S["line_id"].map(lines["plant_id"])
    w["staffed"] = S


def build_timeclock(w: World):
    r = rng("timeclock")
    S = w["staffed"]
    emps = w["employees"]
    ot = w.story["overtime"]
    # the last batch end per (line, day, shift): crews stay when a batch runs past the shift
    rows = []
    by_slot = defaultdict(list)
    for e in emps[emps["line_id"].notna() & emps["hourly"]].itertuples(index=False):
        by_slot[(e.line_id, e.crew)].append(e)
    sup_by_slot = {}
    for e in emps[emps["role"] == "SUPERVISOR DE PRODUCCION"].itertuples(index=False):
        sup_by_slot.setdefault((e.line_id, e.crew), []).append(e)

    def present(e, day):
        return e.hire_date <= day and (e.termination_date is None or pd.isna(e.termination_date) or
                                       e.termination_date >= day)

    for x in S.itertuples(index=False):
        plant = x.plant_id
        crew = p2_crew(x.day, x.shift) if plant == "P2" else x.shift
        bounds = {s: (a, b) for s, a, b in shift_bounds(plant, x.day)}
        st, en = bounds[x.shift]
        is_sunday = x.day.weekday() == 6
        sup = [e for e in sup_by_slot.get((x.line_id, crew), []) if present(e, x.day)]
        approver = int(sup[0].employee_id) if sup else None
        for e in by_slot.get((x.line_id, crew), []):
            if not present(e, x.day) or r.random() < 0.045:
                continue
            pin = st - timedelta(minutes=float(r.uniform(4, 22)))
            pout = en + timedelta(minutes=float(r.uniform(0, 14)))
            ot_min = 0.0
            if r.random() < 0.035:  # the shift ran long: legit overtime
                ext = float(r.uniform(30, 150))
                pout += timedelta(minutes=ext)
                ot_min = ext
            if is_sunday:
                ot_min = (pout - pin).total_seconds() / 60
            inflated = False
            if (int(e.employee_id) in ot["members"] and ot["start"] <= x.day <= ot["end"] and
                    r.random() < 0.42):
                ext = float(r.uniform(120, 240))
                pout += timedelta(minutes=ext)
                ot_min += ext
                inflated = True
            rows.append((int(e.employee_id), plant, x.line_id, x.day, x.shift, pin, pout, round(ot_min),
                         approver if ot_min > 0 else None, inflated))
    # story 12: extra shifts on days off, approved by the supervisor, with nothing extra produced
    members = emps[emps["employee_id"].isin(ot["members"])]
    day = ot["start"]
    while day <= ot["end"]:
        if day.weekday() < 6 and day not in HOLIDAYS:
            on_today = {p2_crew(day, "D"), p2_crew(day, "N")}
            if ot["crew"] not in on_today and r.random() < 0.3:
                for e in members.itertuples(index=False):
                    if r.random() < 0.8:
                        pin = datetime.combine(day, datetime.min.time()) + timedelta(hours=7, minutes=float(
                            r.uniform(-15, 5)))
                        pout = pin + timedelta(hours=float(r.uniform(7.5, 8.5)))
                        rows.append((int(e.employee_id), "P2", ot["line"], day, "D", pin, pout,
                                     round((pout - pin).total_seconds() / 60), ot["supervisor_id"], True))
        day += timedelta(days=1)
    # support staff: maintenance, warehouse, receiving, forklifts, QA techs (Mon-Sat on the plant's days)
    support = emps[emps["line_id"].isna() & emps["hourly"]]
    plant_from = {"P1": START, "P2": P2_OPEN, "P3": P3_ACQUIRED}
    for e in support.itertuples(index=False):
        a = max(e.hire_date, plant_from[e.plant_id])
        b = END if e.termination_date is None or pd.isna(e.termination_date) else min(END, e.termination_date)
        day = a
        while day <= b:
            work = day.weekday() < 5 or (day.weekday() == 5 and e.role != "RECEPCIONISTA DE MATERIALES" and
                                         r.random() < 0.5)
            if work and day not in HOLIDAYS and not in_shutdown(e.plant_id, day) and r.random() > 0.04:
                if e.plant_id == "P2":
                    s, h = ("D", 7) if p2_crew(day, "D") == e.crew or e.role == "RECEPCIONISTA DE MATERIALES" else (
                        "N", 19)
                    length = 12
                    if e.role != "RECEPCIONISTA DE MATERIALES" and p2_crew(day, "D") != e.crew and \
                            p2_crew(day, "N") != e.crew:
                        day += timedelta(days=1)
                        continue
                else:
                    s = e.shift if e.shift else C.SHIFTS[e.plant_id][0][0]
                    h = dict(C.SHIFTS[e.plant_id]).get(s, C.SHIFTS[e.plant_id][0][1])
                    length = 8
                st = datetime.combine(day, datetime.min.time()) + timedelta(hours=h)
                pin = st - timedelta(minutes=float(r.uniform(3, 20)))
                pout = st + timedelta(hours=length, minutes=float(r.uniform(0, 15)))
                otm = 0
                if e.role == "TECNICO DE MANTENIMIENTO" and r.random() < 0.09:
                    ext = float(r.uniform(60, 240))
                    pout += timedelta(minutes=ext)
                    otm = round(ext)
                rows.append((int(e.employee_id), e.plant_id, None, day, s, pin, pout, otm,
                             int(e.supervisor_id) if otm and not pd.isna(e.supervisor_id) else None, False))
            day += timedelta(days=1)
    T = pd.DataFrame(rows, columns=["employee_id", "plant_id", "line_id", "day", "shift", "punch_in", "punch_out",
                                    "ot_minutes", "ot_approved_by", "inflated"])
    w["timeclock"] = T.sort_values(["punch_in", "employee_id"]).reset_index(drop=True)


def build_energy(w: World):
    r = rng("energy")
    B = w["batches"]
    lines = w["lines"].set_index("line_id")
    hours = pd.date_range(START, END + timedelta(days=1), freq="h", inclusive="left")
    idx0 = hours[0]
    n_h = len(hours)
    run_kwh = {pl: np.zeros(n_h) for pl in ("P1", "P2", "P3")}
    line_day = defaultdict(float)
    for b in B.itertuples(index=False):
        kw = LINE_KW[lines.at[b.line_id, "family"]] * lines.at[b.line_id, "rated_kg_per_hour"] / {
            "CHIPS": 450, "CRACKERS": 600, "SAUCE_JAR": 2400, "SAUCE_SACHET": 600, "FROZEN": 700,
            "BAKERY": 800}[lines.at[b.line_id, "family"]]
        st = pd.Timestamp(b.start_ts)
        en = pd.Timestamp(b.end_ts)
        total = (en - st).total_seconds() / 60
        run_frac = b.run_minutes / total if total else 1
        t = st
        while t < en:
            h = t.floor("h")
            nxt = min(en, h + pd.Timedelta(hours=1))
            mins = (nxt - t).total_seconds() / 60
            kwh = kw * mins / 60 * (run_frac + (1 - run_frac) * 0.25)
            i = int((h - idx0).total_seconds() // 3600)
            if 0 <= i < n_h:
                run_kwh[b.plant_id][i] += kwh
            line_day[(b.line_id, h.date())] += kwh
            t = nxt
    hod = hours.hour.to_numpy()
    doy = hours.dayofyear.to_numpy()
    summer = 1 + 0.28 * np.cos(2 * np.pi * (doy - 20) / 365)
    rows = []
    for pl in ("P1", "P2", "P3"):
        from_ = {"P1": START, "P2": P2_OPEN, "P3": P3_ACQUIRED}[pl]
        base = BASE_KW[pl] * summer * (1 + 0.06 * ((hod >= 8) & (hod <= 20))) * r.normal(1, 0.03, n_h)
        shut = np.array([in_shutdown(pl, x.date()) for x in hours])
        base = np.where(shut, base * 0.55, base)
        kwh = base + run_kwh[pl]
        mask = hours.date >= from_
        rows.append(pd.DataFrame({"meter_id": f"MED-{pl}-GENERAL", "plant_id": pl, "ts": hours[mask],
                                  "kwh": np.round(kwh[mask], 1)}))
    w["energy_hourly"] = pd.concat(rows, ignore_index=True)
    ld = pd.Series(line_day).rename_axis(["line_id", "day"]).reset_index(name="kwh")
    ld["kwh"] = ld["kwh"].round(1)
    w["energy_line_daily"] = ld


def build_sensors(w: World):
    """Temperature every five minutes, local time, per cold-storage unit."""
    r = rng("sensors")
    sens = w["sensors"]
    nights = set(w.story["freezer"]["nights"])
    out = []
    for s in sens.itertuples(index=False):
        first = s.installed if isinstance(s.installed, date) else s.installed.date()
        first = max(first, START)
        ts = pd.date_range(datetime.combine(first, datetime.min.time()), datetime.combine(END, datetime.max.time()),
                           freq="5min")
        n = len(ts)
        setp = s.setpoint_c
        # AR(1) noise
        e = r.normal(0, 0.18, n)
        noise = np.empty(n)
        acc = 0.0
        for i in range(n):  # fast enough at ~500k
            acc = 0.92 * acc + e[i]
            noise[i] = acc
        temp = setp + noise
        hod = ts.hour.to_numpy()
        minute = ts.minute.to_numpy()
        wd = ts.weekday.to_numpy()
        # door openings during working hours
        working = (hod >= 6) & (hod < 22) & (wd < 6)
        doors = working & (r.random(n) < 0.035)
        bump = np.zeros(n)
        for i in np.flatnonzero(doors):
            amp = float(r.uniform(1.0, 3.5)) if setp < 0 else float(r.uniform(0.8, 2.5))
            for k in range(4):
                if i + k < n:
                    bump[i + k] += amp * (0.6 ** k)
        temp += bump
        # defrost cycles at 02:00, 10:00, 18:00 for freezers: 20 minutes, a few degrees
        if setp < 0:
            for hh in (2, 10, 18):
                m = (hod == hh) & (minute < 25)
                temp[m] += 2.6 + 0.08 * minute[m]
        # story 6: FRZ-N2's defrost runs 90 minutes on most nights after the January 2025 job
        if s.unit_id == "FRZ-N2":
            dates = ts.date
            exc = np.array([x in nights for x in dates]) & (((hod == 2) | (hod == 3)) | ((hod == 4) & (minute < 30)))
            mins = (hod - 2) * 60 + minute
            shape = np.where(mins < 60, mins / 60 * 13, np.maximum(0, 13 - (mins - 60) / 30 * 13))
            temp = np.where(exc, temp + shape + r.normal(0, 0.6, n), temp)
        out.append(pd.DataFrame({"sensor_id": s.sensor_id, "unit_id": s.unit_id, "ts_local": ts,
                                 "temp_c": np.round(temp, 2)}))
    S = pd.concat(out, ignore_index=True)
    # gateway outages: a few hours missing here and there
    drop = np.zeros(len(S), dtype=bool)
    for _ in range(140):
        i = int(r.integers(len(S)))
        drop[i:i + int(r.integers(6, 72))] = True
    w["sensor_true"] = S[~drop].reset_index(drop=True)


def run_facilities(w: World):
    build_maintenance(w)
    staffed_shifts(w)
    build_timeclock(w)
    build_energy(w)
    build_sensors(w)
