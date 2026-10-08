"""The static facts of the business, written by hand: plants, lines, product formulas and packs, raw materials, and the
name pools that fictional companies and people are drawn from. Nothing here is random; master.py draws from it."""

# ---- Plants and lines --------------------------------------------------------------------------------------------------

PLANTS = [
    # plant_id, name, city, province, opened, erp_from, acquired
    ("P1", "Planta Norte", "San Lorenzo", "Santa Fe", "1998-04-17", "2019-03-01", None),
    ("P2", "Planta Sur", "Zárate", "Buenos Aires", "2022-07-01", "2022-07-01", None),
    ("P3", "Planta Delta", "Gualeguaychú", "Entre Ríos", "2009-10-05", "2025-05-05", "2024-03-01"),
]

# line_id, plant, name, family, kg_per_hour (nameplate), batch_hours, shifts per day, shift hours, start, legacy name
LINES = [
    ("L1-01", "P1", "Papas fritas", "CHIPS", 450, 4.0, 3, 8, "1998-04-17", None),
    ("L1-02", "P1", "Galletitas crackers", "CRACKERS", 600, 4.0, 3, 8, "2001-06-01", None),
    ("L1-03", "P1", "Salsas en frasco", "SAUCE_JAR", 2400, 2.0, 3, 8, "2004-03-01", None),
    ("L1-04", "P1", "Sachets y doypacks", "SAUCE_SACHET", 600, 4.0, 3, 8, "2012-08-01", None),
    ("L1-05", "P1", "Congelados", "FROZEN", 700, 2.0, 3, 8, "2015-05-01", None),
    ("L1-06", "P1", "Panificados", "BAKERY", 800, 3.0, 3, 8, "2017-09-01", None),
    ("SUR-01", "P2", "Snacks Sur", "CHIPS", 450, 4.0, 2, 12, "2022-07-01", None),
    ("SUR-02", "P2", "Envasado salsas Sur", "SAUCE_JAR", 1900, 2.0, 2, 12, "2022-07-01", None),
    ("SUR-03", "P2", "Congelados Sur", "FROZEN", 600, 2.0, 2, 12, "2022-11-01", None),
    ("SUR-04", "P2", "Crackers Sur", "CRACKERS", 450, 4.0, 2, 12, "2023-03-01", None),
    ("DEL-01", "P3", "Salsas Delta", "SAUCE_JAR", 1500, 2.0, 3, 8, "2009-10-05", "LINEA 1"),
    ("DEL-02", "P3", "Congelados Delta", "FROZEN", 500, 2.0, 3, 8, "2011-02-01", "LINEA 2"),
    ("DEL-03", "P3", "Panadería Delta", "BAKERY", 600, 3.0, 3, 8, "2014-08-01", "LINEA 3"),
]

# Shift start hours per plant (local time). P2's local team runs two 12-hour shifts.
SHIFTS = {"P1": [("M", 6), ("T", 14), ("N", 22)], "P2": [("D", 7), ("N", 19)], "P3": [("M", 5), ("T", 13), ("N", 21)]}

# True availability / performance / first-pass quality by line (before story effects)
LINE_PERF = {
    "L1-01": (0.86, 0.93, 0.965), "L1-02": (0.84, 0.92, 0.975), "L1-03": (0.85, 0.94, 0.985),
    "L1-04": (0.62, 0.93, 0.98),  # the hidden-downtime line: really the worst
    "L1-05": (0.83, 0.91, 0.97), "L1-06": (0.85, 0.92, 0.96),
    "SUR-01": (0.80, 0.90, 0.955), "SUR-02": (0.82, 0.92, 0.98), "SUR-03": (0.80, 0.90, 0.96),
    "SUR-04": (0.79, 0.90, 0.965),
    "DEL-01": (0.78, 0.89, 0.975), "DEL-02": (0.77, 0.88, 0.955), "DEL-03": (0.80, 0.90, 0.95),
}

# Typical batch yield (good output / theoretical) by family: the product-mix side of the Simpson's paradox
FAMILY_YIELD = {"CHIPS": 0.862, "CRACKERS": 0.897, "SAUCE_JAR": 0.984, "SAUCE_SACHET": 0.974, "FROZEN": 0.931,
                "BAKERY": 0.878}
# Yield gap of each plant against P1 on the same product (negative: worse). P2 is newer and worse on everything.
PLANT_YIELD_GAP = {"P1": 0.0, "P2": -0.011, "P3": -0.016}

# ---- Product formulas and packs --------------------------------------------------------------------------------------
# formula, name, family of the jar/bag packs, brand, packs [(grams, units per case, label)], popularity, allergens,
# lines able to make it
F = []


def formula(code, name, family, brand, packs, pop, allergens="", plants=("P1",)):
    F.append(dict(code=code, name=name, family=family, brand=brand, packs=packs, pop=pop, allergens=allergens,
                  plants=plants))


B_CH, B_CR, B_SA, B_FS, B_FR, B_BK = ("Crocante Sur", "Tres Ríos", "La Ribera", "La Ribera Food Service",
                                       "Tres Ríos Congelados", "Horno Norte")
for code, name, packs, pop, alg in [
    ("PAP-CLA", "Papas Fritas Clásicas", [(30, 60), (80, 30), (150, 20), (250, 12), (400, 10)], 10, ""),
    ("PAP-OND", "Papas Onduladas", [(80, 30), (150, 20), (250, 12)], 5, ""),
    ("PAP-SAL", "Papas Sal Marina", [(80, 30), (150, 20)], 3, ""),
    ("PAP-JAM", "Papas sabor Jamón", [(80, 30), (150, 20)], 2.5, "leche"),
    ("PAP-QUE", "Papas sabor Queso", [(80, 30), (150, 20)], 3, "leche"),
    ("PAP-KET", "Papas sabor Ketchup", [(80, 30), (150, 20)], 1.5, ""),
    ("PAP-LIM", "Papas Limón", [(80, 30)], 1, ""),
    ("PAP-PIM", "Papas Pimentón", [(80, 30), (150, 20)], 2, ""),
    ("PAP-BBQ", "Papas Barbacoa", [(80, 30), (150, 20), (250, 12)], 3, ""),
    ("BAT-FRI", "Batatas Fritas", [(80, 30), (150, 20)], 1.5, ""),
    ("MIX-SNK", "Mix Snack", [(150, 20), (300, 12)], 2, "maní,gluten"),
    ("PAL-SAL", "Palitos Salados", [(80, 30), (150, 20)], 2.5, "gluten"),
    ("CHZ-QUE", "Chizitos Queso", [(60, 40), (120, 24)], 3, "leche"),
    ("NAC-TOR", "Nachos Tortilla", [(150, 20), (300, 12)], 2, ""),
    ("MAN-SAL", "Maní Salado", [(100, 30), (250, 12)], 2, "maní"),
    ("TOS-MAI", "Tostaditas de Maíz", [(100, 30)], 1, ""),
    ("PAP-ACA", "Papas Acanaladas Provenzal", [(80, 30), (150, 20)], 1.5, ""),
    ("PAP-CHE", "Papas Cheddar", [(80, 30), (150, 20)], 1.5, "leche"),
]:
    formula(code, name, "CHIPS", B_CH, packs, pop, alg, ("P1", "P2"))
formula("PAP-TRU", "Papas Trufa", "CHIPS", B_CH, [(100, 24)], 0.8, "", ("P1",))

for code, name, packs, pop, alg in [
    ("CRK-CLA", "Crackers Clásicas", [(100, 40), (200, 24), (300, 16)], 8, "gluten"),
    ("CRK-INT", "Crackers Integrales", [(200, 24), (300, 16)], 4, "gluten"),
    ("CRK-SES", "Crackers con Sésamo", [(200, 24)], 2.5, "gluten,sésamo"),
    ("CRK-SAL", "Crackers Salvado", [(200, 24)], 2, "gluten"),
    ("CRK-QUE", "Crackers Queso", [(100, 40), (200, 24)], 2.5, "gluten,leche"),
    ("CRK-SSA", "Crackers Sin Sal", [(200, 24)], 1, "gluten"),
    ("GRI-CLA", "Grisines", [(150, 20), (300, 12)], 2.5, "gluten"),
    ("TOS-ARR", "Tostadas de Arroz", [(100, 30), (150, 20)], 1.5, ""),
    ("GAL-AGU", "Galletas de Agua", [(200, 24), (300, 16)], 4, "gluten"),
    ("CRK-SEM", "Crackers Semillas", [(200, 24)], 1.5, "gluten,sésamo"),
    ("CRK-ROM", "Crackers Romero", [(200, 24)], 1, "gluten"),
    ("GAL-SAL", "Galletas Saladas Familiares", [(400, 12)], 2, "gluten"),
    ("CRK-ORE", "Crackers Orégano y Oliva", [(200, 24)], 1, "gluten"),
]:
    formula(code, name, "CRACKERS", B_CR, packs, pop, alg, ("P1", "P2"))

for code, name, packs, pop, alg, plants in [
    ("MAY-CLA", "Mayonesa Clásica", [(250, 24), (500, 12), (1000, 8), (2900, 4)], 14, "huevo", ("P1", "P2", "P3")),
    ("MAY-LIG", "Mayonesa Light", [(250, 24), (500, 12)], 4, "huevo", ("P1", "P2")),
    ("MAY-AJO", "Mayonesa con Ajo", [(250, 24)], 1.5, "huevo", ("P1",)),
    ("KET-CLA", "Ketchup", [(250, 24), (500, 12), (1000, 8)], 7, "", ("P1", "P2", "P3")),
    ("MOS-CLA", "Mostaza", [(250, 24), (500, 12)], 4, "mostaza", ("P1", "P2")),
    ("SAL-GOL", "Salsa Golf", [(250, 24), (500, 12)], 4, "huevo", ("P1", "P2", "P3")),
    ("SAL-BBQ", "Salsa Barbacoa", [(250, 24), (500, 12)], 3, "", ("P1", "P2")),
    ("SAL-CRI", "Salsa Criolla", [(250, 24)], 1.5, "", ("P1",)),
    ("CHI-CLA", "Chimichurri", [(250, 24)], 2.5, "", ("P1", "P2")),
    ("SAL-TAR", "Salsa Tártara", [(250, 24)], 1, "huevo", ("P1",)),
    ("POM-CLA", "Salsa Pomarola", [(340, 24), (520, 12)], 5, "", ("P1", "P2", "P3")),
    ("SAL-PIZ", "Salsa Pizza", [(340, 24)], 3, "", ("P1", "P2", "P3")),
    ("SAL-BOL", "Salsa Bolognesa", [(340, 24)], 2.5, "", ("P1",)),
    ("PES-CLA", "Pesto", [(190, 24)], 1, "leche,frutos secos", ("P1",)),
    ("SAL-PIC", "Salsa Picante", [(190, 24)], 1, "", ("P1",)),
    ("ADE-CES", "Aderezo César", [(250, 24)], 1.5, "huevo,leche,pescado", ("P1",)),
    ("SAL-FIL", "Salsa Filetto", [(340, 24)], 2, "", ("P1", "P2")),
    ("SAL-4QU", "Salsa Cuatro Quesos", [(340, 24)], 1, "leche", ("P1",)),
    ("MAY-OLI", "Mayonesa con Oliva", [(250, 24)], 1, "huevo", ("P1",)),
]:
    formula(code, name, "SAUCE_JAR", B_SA, packs, pop, alg, plants)

# Sachet and doypack packs of sauce formulas, on L1-04 (family SAUCE_SACHET at SKU level)
SACHET_PACKS = {
    "MAY-CLA": [(8, 196, "sachet"), (500, 20, "doypack"), (1000, 12, "doypack")],
    "KET-CLA": [(8, 196, "sachet"), (500, 20, "doypack")],
    "MOS-CLA": [(8, 196, "sachet")],
    "SAL-GOL": [(8, 196, "sachet"), (500, 20, "doypack")],
    "ADE-CES": [(12, 144, "sachet")],
    "SAL-BBQ": [(8, 196, "sachet"), (1000, 12, "doypack")],
    "MAY-LIG": [(8, 196, "sachet")],
    "SAL-SOJ": [(8, 196, "sachet")],
}
formula("SAL-SOJ", "Salsa de Soja", "SAUCE_SACHET", B_FS, [], 1.0, "soja,gluten", ("P1",))

for code, name, packs, pop, alg, plants in [
    ("LAS-BOL", "Lasaña Bolognesa", [(500, 12), (1000, 6)], 4, "gluten,leche,huevo", ("P1", "P2")),
    ("CAN-VER", "Canelones de Verdura", [(500, 12)], 2.5, "gluten,leche,huevo", ("P1", "P2")),
    ("RAV-RIC", "Ravioles de Ricota", [(500, 12), (1000, 6)], 3, "gluten,leche,huevo", ("P1", "P2")),
    ("EMP-CAR", "Empanadas de Carne x12", [(720, 10), (2880, 4)], 6, "gluten", ("P1", "P2", "P3")),
    ("EMP-POL", "Empanadas de Pollo x12", [(720, 10)], 4, "gluten", ("P1", "P2", "P3")),
    ("EMP-JYQ", "Empanadas Jamón y Queso x12", [(720, 10)], 5, "gluten,leche", ("P1", "P2", "P3")),
    ("EMP-VER", "Empanadas de Verdura x12", [(720, 10)], 2, "gluten,leche", ("P1",)),
    ("PIZ-MUZ", "Pizza Muzzarella", [(550, 12), (1100, 6)], 7, "gluten,leche", ("P1", "P2", "P3")),
    ("PIZ-NAP", "Pizza Napolitana", [(580, 12)], 3, "gluten,leche", ("P1", "P2")),
    ("PIZ-JYM", "Pizza Jamón y Morrones", [(600, 12)], 2, "gluten,leche", ("P1",)),
    ("MIL-POL", "Milanesas de Pollo", [(500, 12), (1000, 6)], 5, "gluten,huevo", ("P1", "P2")),
    ("MIL-SOJ", "Milanesas de Soja", [(380, 16)], 2, "soja,gluten", ("P1",)),
    ("HAM-CLA", "Hamburguesas Clásicas x4", [(320, 20), (960, 8)], 6, "soja", ("P1", "P2")),
    ("MED-MER", "Medallones de Merluza", [(400, 16)], 2, "pescado,gluten", ("P1",)),
    ("TAR-JYQ", "Tarta Jamón y Queso", [(500, 12)], 2, "gluten,leche,huevo", ("P1", "P3")),
    ("TAR-VER", "Tarta de Verdura", [(500, 12)], 1.5, "gluten,leche,huevo", ("P1", "P3")),
    ("PAS-PAP", "Pastel de Papa", [(600, 12)], 2, "leche", ("P1",)),
    ("NOQ-PAP", "Ñoquis de Papa", [(500, 12), (1000, 6)], 3, "gluten,huevo", ("P1", "P2")),
    ("ARR-POL", "Arroz con Pollo", [(350, 16)], 1.5, "", ("P1",)),
    ("GUI-LEN", "Guiso de Lentejas", [(350, 16)], 1, "", ("P1",)),
    ("SOR-JYQ", "Sorrentinos Jamón y Queso", [(500, 12)], 2, "gluten,leche,huevo", ("P1", "P2")),
    ("PAT-POL", "Patitas de Pollo", [(400, 16)], 3, "gluten", ("P1", "P2")),
    ("BOC-ESP", "Bocaditos de Espinaca", [(400, 16)], 1.5, "gluten,leche", ("P1",)),
    ("LOC-CRI", "Locro Criollo", [(500, 12)], 0.8, "", ("P1",)),
]:
    formula(code, name, "FROZEN", B_FR, packs, pop, alg, plants)

for code, name, packs, pop, alg, plants in [
    ("PAN-LAC", "Pan Lactal Blanco", [(390, 12), (550, 10)], 8, "gluten,soja", ("P1", "P3")),
    ("PAN-INT", "Pan Lactal Integral", [(390, 12), (550, 10)], 4, "gluten,soja", ("P1", "P3")),
    ("PAN-MUL", "Pan Multicereal", [(450, 10)], 2, "gluten,sésamo", ("P1",)),
    ("PAN-HAM", "Pan de Hamburguesa x4", [(220, 20), (660, 8)], 3, "gluten,sésamo", ("P1", "P3")),
    ("PAN-PAN", "Pan de Pancho x6", [(300, 16)], 3, "gluten", ("P1", "P3")),
    ("BUD-VAI", "Budín de Vainilla", [(250, 18)], 2, "gluten,huevo,leche", ("P1",)),
    ("BUD-LIM", "Budín de Limón", [(250, 18)], 1.5, "gluten,huevo,leche", ("P1",)),
    ("MAG-VAI", "Magdalenas x6", [(180, 20)], 1.5, "gluten,huevo,leche", ("P1",)),
    ("PAN-RAL", "Pan Rallado", [(500, 20), (1000, 10)], 2, "gluten", ("P1", "P3")),
    ("PRE-PIZ", "Prepizzas x2", [(400, 16)], 2, "gluten", ("P1", "P3")),
    ("TOS-CLA", "Tostadas Clásicas", [(200, 24)], 1.5, "gluten", ("P1",)),
]:
    formula(code, name, "BAKERY", B_BK, packs, pop, alg, plants)

FORMULAS = {f["code"]: f for f in F}

# P3's own brands, before and after the acquisition
P3_BRANDS = {"SAUCE_JAR": "Delta Sabores", "FROZEN": "Cocina del Delta", "BAKERY": "Panificados Delta"}
P3_FORMULAS = {
    "SAUCE_JAR": ["MAY-CLA", "KET-CLA", "SAL-GOL", "POM-CLA", "SAL-PIZ"],
    "FROZEN": ["EMP-CAR", "EMP-POL", "EMP-JYQ", "PIZ-MUZ", "TAR-JYQ", "TAR-VER"],
    "BAKERY": ["PAN-LAC", "PAN-INT", "PAN-HAM", "PAN-PAN", "PAN-RAL", "PRE-PIZ"],
}

# ---- Ingredients of each formula (kg of ingredient per kg of finished product) --------------------------------------
# The factor > 1 for chips is the water fried out of the potato.
BASE_RECIPES = {
    "CHIPS": [("RM-PAPA", 3.6), ("RM-ACGI", 0.34), ("RM-SALF", 0.015)],
    "CHIPS_CORN": [("RM-HMAI", 0.82), ("RM-ACGI", 0.25), ("RM-SALF", 0.015)],
    "CRACKERS": [("RM-HT00", 0.72), ("RM-ACGI", 0.12), ("RM-SALF", 0.018), ("RM-LEVA", 0.012), ("RM-EXMA", 0.02),
                 ("RM-AZUC", 0.03)],
    "MAYO": [("RM-ACGI", 0.74), ("RM-HUEV", 0.07), ("RM-VINA", 0.05), ("RM-SALF", 0.012), ("RM-AZUC", 0.015),
             ("RM-JLIM", 0.006), ("RM-SORB", 0.0012), ("RM-GXAN", 0.002), ("RM-AGUA", 0.10)],
    "TOMATO": [("RM-PTOM", 0.42), ("RM-AZUC", 0.08), ("RM-VINA", 0.06), ("RM-SALF", 0.018), ("RM-CEBP", 0.01),
               ("RM-AGUA", 0.38)],
    "FROZEN_PASTA": [("RM-HT00", 0.35), ("RM-HUEV", 0.06), ("RM-QMUZ", 0.12), ("RM-PTOM", 0.10), ("RM-SALF", 0.01),
                     ("RM-AGUA", 0.35)],
    "FROZEN_EMP": [("RM-HT00", 0.42), ("RM-MARG", 0.08), ("RM-SALF", 0.012), ("RM-CEBO", 0.12), ("RM-AGUA", 0.15)],
    "FROZEN_PIZZA": [("RM-HT00", 0.45), ("RM-LEVA", 0.01), ("RM-QMUZ", 0.22), ("RM-PTOM", 0.12), ("RM-ACGI", 0.03),
                     ("RM-SALF", 0.01), ("RM-AGUA", 0.18)],
    "FROZEN_MEAT": [("RM-PAN-RAL", 0.18), ("RM-HUEV", 0.08), ("RM-SALF", 0.012), ("RM-AJOP", 0.004)],
    "BREAD": [("RM-HT00", 0.62), ("RM-AGUA", 0.33), ("RM-LEVA", 0.015), ("RM-SALF", 0.012), ("RM-AZUC", 0.03),
              ("RM-MARG", 0.03), ("RM-LECS", 0.004)],
    "CAKE": [("RM-HT00", 0.30), ("RM-AZUC", 0.28), ("RM-HUEV", 0.20), ("RM-MARG", 0.15), ("RM-LECP", 0.04),
             ("RM-ESVA", 0.003)],
}

# extra ingredients per formula, on top of its base
EXTRA = {
    "PAP-SAL": [("RM-SALM", 0.012)], "PAP-JAM": [("RM-SJAM", 0.05)], "PAP-QUE": [("RM-SQUE", 0.06)],
    "PAP-KET": [("RM-SKET", 0.05)], "PAP-LIM": [("RM-SLIM", 0.04)], "PAP-PIM": [("RM-PIMA", 0.03)],
    "PAP-BBQ": [("RM-SBBQ", 0.045), ("RM-PIMA", 0.012)], "BAT-FRI": [], "MIX-SNK": [("RM-MANI", 0.25)],
    "PAL-SAL": [("RM-HT00", 0.5)], "CHZ-QUE": [("RM-SQUE", 0.08), ("RM-HMAI", 0.5)], "NAC-TOR": [("RM-HMAI", 0.5)],
    "MAN-SAL": [("RM-MANI", 0.9)], "TOS-MAI": [("RM-HMAI", 0.5)], "PAP-ACA": [("RM-AJOP", 0.01), ("RM-PERE", 0.004)],
    "PAP-CHE": [("RM-SQUE", 0.06)], "PAP-TRU": [("RM-STRU", 0.03)],
    "CRK-INT": [("RM-HTIN", 0.4)], "CRK-SES": [("RM-SESA", 0.06)], "CRK-SAL": [("RM-SALV", 0.15)],
    "CRK-QUE": [("RM-SQUE", 0.06)], "GRI-CLA": [], "TOS-ARR": [("RM-ARRO", 0.9)], "CRK-SEM": [("RM-SESA", 0.03),
                                                                                         ("RM-CHIA", 0.03),
                                                                                         ("RM-LINO", 0.03)],
    "CRK-ROM": [("RM-ROME", 0.004)], "CRK-ORE": [("RM-OREG", 0.004), ("RM-ACOL", 0.03)],
    "MAY-AJO": [("RM-AJOP", 0.01)], "MAY-OLI": [("RM-ACOL", 0.15)], "MOS-CLA": [("RM-MOSG", 0.18)],
    "SAL-GOL": [("RM-PTOM", 0.08)], "SAL-BBQ": [("RM-HUMO", 0.004), ("RM-PIMA", 0.012)],
    "SAL-CRI": [("RM-CEBO", 0.25), ("RM-MORR", 0.15)],
    "CHI-CLA": [("RM-OREG", 0.03), ("RM-AJIM", 0.02), ("RM-PIMA", 0.015), ("RM-AJOP", 0.02)],
    "SAL-TAR": [("RM-PEPI", 0.08)], "SAL-PIZ": [("RM-OREG", 0.006)], "SAL-BOL": [("RM-CARN", 0.18)],
    "PES-CLA": [("RM-ALBA", 0.2), ("RM-QREG", 0.1), ("RM-NUEZ", 0.05)], "SAL-PIC": [("RM-AJIM", 0.06)],
    "ADE-CES": [("RM-QREG", 0.06), ("RM-ANCH", 0.01)], "SAL-4QU": [("RM-QREG", 0.12), ("RM-QMUZ", 0.08)],
    "SAL-SOJ": [("RM-SOJA", 0.6)],
    "LAS-BOL": [("RM-CARN", 0.15)], "CAN-VER": [("RM-ESPI", 0.18)], "RAV-RIC": [("RM-RICO", 0.2)],
    "EMP-CAR": [("RM-CARN", 0.22), ("RM-PIMA", 0.006), ("RM-COMI", 0.003)], "EMP-POL": [("RM-POLL", 0.22)],
    "EMP-JYQ": [("RM-JAMO", 0.12), ("RM-QMUZ", 0.12)], "EMP-VER": [("RM-ESPI", 0.2), ("RM-QREG", 0.03)],
    "PIZ-NAP": [("RM-TOMF", 0.08), ("RM-AJOP", 0.004)], "PIZ-JYM": [("RM-JAMO", 0.08), ("RM-MORR", 0.06)],
    "MIL-POL": [("RM-POLL", 0.7)], "MIL-SOJ": [("RM-PTSO", 0.55)], "HAM-CLA": [("RM-CARN", 0.85), ("RM-PTSO", 0.05)],
    "MED-MER": [("RM-MERL", 0.7)], "TAR-JYQ": [("RM-JAMO", 0.12), ("RM-QMUZ", 0.12), ("RM-HUEV", 0.05)],
    "TAR-VER": [("RM-ACEL", 0.25), ("RM-HUEV", 0.06)], "PAS-PAP": [("RM-PAPA", 0.5), ("RM-CARN", 0.25),
                                                                   ("RM-PIMA", 0.004)],
    "NOQ-PAP": [("RM-PAPA", 0.7)], "ARR-POL": [("RM-ARRO", 0.35), ("RM-POLL", 0.2), ("RM-PIMA", 0.004)],
    "GUI-LEN": [("RM-LENT", 0.3), ("RM-CARN", 0.08), ("RM-PIMA", 0.005)], "SOR-JYQ": [("RM-JAMO", 0.1),
                                                                                      ("RM-QMUZ", 0.1)],
    "PAT-POL": [("RM-POLL", 0.75)], "BOC-ESP": [("RM-ESPI", 0.5), ("RM-QMUZ", 0.1)],
    "LOC-CRI": [("RM-MAIZ", 0.25), ("RM-CARN", 0.1), ("RM-ZAPA", 0.15), ("RM-PIMA", 0.01)],
    "PAN-INT": [("RM-HTIN", 0.3)], "PAN-MUL": [("RM-SESA", 0.03), ("RM-LINO", 0.03), ("RM-GIRA", 0.03)],
    "PAN-HAM": [("RM-SESA", 0.01)], "BUD-LIM": [("RM-JLIM", 0.02)], "PAN-RAL": [], "TOS-CLA": [],
}

BASE_OF = {}
for f in F:
    fam, c = f["family"], f["code"]
    if fam == "CHIPS":
        BASE_OF[c] = "CHIPS_CORN" if c in ("NAC-TOR", "TOS-MAI", "CHZ-QUE") else "CHIPS"
    elif fam == "CRACKERS":
        BASE_OF[c] = "CRACKERS"
    elif fam in ("SAUCE_JAR", "SAUCE_SACHET"):
        BASE_OF[c] = "MAYO" if c.startswith(("MAY", "SAL-GOL", "SAL-TAR", "ADE")) else "TOMATO"
    elif fam == "FROZEN":
        BASE_OF[c] = ("FROZEN_EMP" if c.startswith(("EMP", "TAR")) else "FROZEN_PIZZA" if c.startswith("PIZ")
                      else "FROZEN_MEAT" if c.startswith(("MIL", "HAM", "MED", "PAT", "BOC")) else "FROZEN_PASTA")
    else:
        BASE_OF[c] = "CAKE" if c.startswith(("BUD", "MAG")) else "BREAD"
for c in ("MOS-CLA", "SAL-SOJ", "CHI-CLA", "PES-CLA", "SAL-PIC"):
    BASE_OF[c] = "TOMATO"

# ---- Raw materials -----------------------------------------------------------------------------------------------------
# code, description, category, uom, allergens, storage, shelf life (days), cost USD per uom, lead days, order every days
RAW = [
    ("RM-PAPA", "Papa fresca para industria", "TUBERCULOS", "kg", "", "AMBIENTE", 45, 0.22, 2, 3),
    ("RM-ACGI", "Aceite de girasol refinado (granel)", "ACEITES", "kg", "", "AMBIENTE", 365, 1.35, 5, 7),
    ("RM-ACOL", "Aceite de oliva", "ACEITES", "kg", "", "AMBIENTE", 540, 6.8, 20, 30),
    ("RM-SALF", "Sal fina refinada", "BASICOS", "kg", "", "AMBIENTE", 1800, 0.18, 7, 30),
    ("RM-SALM", "Sal marina en escamas", "BASICOS", "kg", "", "AMBIENTE", 1800, 1.4, 15, 30),
    ("RM-AZUC", "Azúcar refinada", "BASICOS", "kg", "", "AMBIENTE", 720, 0.72, 7, 14),
    ("RM-HT00", "Harina de trigo 000", "HARINAS", "kg", "gluten", "AMBIENTE", 180, 0.38, 4, 7),
    ("RM-HTIN", "Harina de trigo integral", "HARINAS", "kg", "gluten", "AMBIENTE", 150, 0.52, 7, 14),
    ("RM-HMAI", "Harina de maíz nixtamalizada", "HARINAS", "kg", "", "AMBIENTE", 180, 0.61, 10, 14),
    ("RM-ARRO", "Arroz largo fino", "BASICOS", "kg", "", "AMBIENTE", 540, 0.9, 10, 30),
    ("RM-LENT", "Lentejas secas", "BASICOS", "kg", "", "AMBIENTE", 540, 1.1, 10, 30),
    ("RM-MAIZ", "Maíz blanco partido", "BASICOS", "kg", "", "AMBIENTE", 540, 0.7, 10, 30),
    ("RM-LEVA", "Levadura prensada", "PANADERIA", "kg", "", "REFRIGERADO", 30, 2.4, 3, 7),
    ("RM-EXMA", "Extracto de malta", "PANADERIA", "kg", "gluten", "AMBIENTE", 365, 2.9, 15, 30),
    ("RM-MARG", "Margarina de hojaldre", "GRASAS", "kg", "leche", "REFRIGERADO", 120, 2.2, 5, 7),
    ("RM-LECP", "Leche en polvo entera", "LACTEOS", "kg", "leche", "AMBIENTE", 365, 4.1, 10, 14),
    ("RM-LECS", "Suero de leche en polvo", "LACTEOS", "kg", "leche", "AMBIENTE", 365, 2.6, 10, 30),
    ("RM-QMUZ", "Queso muzzarella en barra", "LACTEOS", "kg", "leche", "REFRIGERADO", 45, 5.2, 3, 4),
    ("RM-QREG", "Queso reggianito rallado", "LACTEOS", "kg", "leche", "REFRIGERADO", 90, 9.5, 5, 14),
    ("RM-RICO", "Ricota", "LACTEOS", "kg", "leche", "REFRIGERADO", 15, 3.1, 2, 3),
    ("RM-HUEV", "Huevo líquido pasteurizado", "HUEVO", "kg", "huevo", "REFRIGERADO", 21, 2.3, 2, 3),
    ("RM-CARN", "Carne vacuna picada congelada", "CARNES", "kg", "", "CONGELADO", 180, 5.6, 4, 4),
    ("RM-POLL", "Pollo deshuesado congelado", "CARNES", "kg", "", "CONGELADO", 180, 3.4, 4, 4),
    ("RM-JAMO", "Jamón cocido", "CARNES", "kg", "", "REFRIGERADO", 40, 6.1, 3, 4),
    ("RM-MERL", "Merluza en bloque congelada", "PESCADOS", "kg", "pescado", "CONGELADO", 270, 4.3, 10, 14),
    ("RM-ANCH", "Anchoas en salazón", "PESCADOS", "kg", "pescado", "REFRIGERADO", 365, 14.0, 20, 60),
    ("RM-ESPI", "Espinaca congelada", "VEGETALES", "kg", "", "CONGELADO", 365, 1.9, 7, 14),
    ("RM-ACEL", "Acelga congelada", "VEGETALES", "kg", "", "CONGELADO", 365, 1.6, 7, 14),
    ("RM-CEBO", "Cebolla fresca", "VEGETALES", "kg", "", "AMBIENTE", 30, 0.45, 2, 4),
    ("RM-MORR", "Morrón rojo en tiras", "VEGETALES", "kg", "", "REFRIGERADO", 20, 1.9, 2, 4),
    ("RM-ZAPA", "Zapallo en cubos congelado", "VEGETALES", "kg", "", "CONGELADO", 365, 1.2, 7, 30),
    ("RM-TOMF", "Tomate en rodajas", "VEGETALES", "kg", "", "REFRIGERADO", 10, 1.3, 2, 3),
    ("RM-ALBA", "Albahaca fresca", "VEGETALES", "kg", "", "REFRIGERADO", 7, 6.0, 2, 4),
    ("RM-PEPI", "Pepinillos en vinagre", "VEGETALES", "kg", "", "AMBIENTE", 365, 2.2, 15, 30),
    ("RM-PTOM", "Pasta de tomate 28-30 brix", "TOMATE", "kg", "", "AMBIENTE", 540, 1.25, 10, 14),
    ("RM-VINA", "Vinagre de alcohol 5%", "BASICOS", "kg", "", "AMBIENTE", 720, 0.55, 7, 14),
    ("RM-JLIM", "Jugo de limón concentrado", "BASICOS", "kg", "", "REFRIGERADO", 180, 2.8, 10, 30),
    ("RM-MOSG", "Mostaza en grano molida", "ESPECIAS", "kg", "mostaza", "AMBIENTE", 365, 3.9, 20, 30),
    ("RM-SORB", "Sorbato de potasio", "ADITIVOS", "kg", "", "AMBIENTE", 720, 7.5, 20, 60),
    ("RM-GXAN", "Goma xántica", "ADITIVOS", "kg", "", "AMBIENTE", 720, 9.0, 20, 60),
    ("RM-AGUA", "Agua tratada (consumo)", "SERVICIOS", "kg", "", "AMBIENTE", 1, 0.001, 0, 0),
    ("RM-PIMA", "Pimentón ahumado molido", "ESPECIAS", "kg", "", "AMBIENTE", 365, 6.4, 15, 21),
    ("RM-OREG", "Orégano deshidratado", "ESPECIAS", "kg", "", "AMBIENTE", 365, 7.8, 15, 30),
    ("RM-AJIM", "Ají molido", "ESPECIAS", "kg", "", "AMBIENTE", 365, 5.9, 15, 30),
    ("RM-AJOP", "Ajo en polvo", "ESPECIAS", "kg", "", "AMBIENTE", 365, 6.6, 15, 30),
    ("RM-CEBP", "Cebolla en polvo", "ESPECIAS", "kg", "", "AMBIENTE", 365, 5.2, 15, 30),
    ("RM-COMI", "Comino molido", "ESPECIAS", "kg", "", "AMBIENTE", 365, 8.8, 15, 60),
    ("RM-PERE", "Perejil deshidratado", "ESPECIAS", "kg", "", "AMBIENTE", 365, 9.5, 15, 60),
    ("RM-ROME", "Romero deshidratado", "ESPECIAS", "kg", "", "AMBIENTE", 365, 9.1, 15, 60),
    ("RM-ESVA", "Esencia de vainilla", "AROMAS", "kg", "", "AMBIENTE", 720, 18.0, 20, 60),
    ("RM-SJAM", "Saborizante jamón en polvo", "AROMAS", "kg", "leche", "AMBIENTE", 365, 8.3, 20, 30),
    ("RM-SQUE", "Saborizante queso en polvo", "AROMAS", "kg", "leche", "AMBIENTE", 365, 8.9, 20, 30),
    ("RM-SKET", "Saborizante ketchup en polvo", "AROMAS", "kg", "", "AMBIENTE", 365, 8.1, 20, 30),
    ("RM-SLIM", "Saborizante limón en polvo", "AROMAS", "kg", "", "AMBIENTE", 365, 7.7, 20, 30),
    ("RM-SBBQ", "Saborizante barbacoa en polvo", "AROMAS", "kg", "", "AMBIENTE", 365, 8.4, 20, 30),
    ("RM-STRU", "Saborizante trufa", "AROMAS", "kg", "", "AMBIENTE", 365, 32.0, 30, 60),
    ("RM-HUMO", "Humo líquido", "AROMAS", "kg", "", "AMBIENTE", 720, 11.0, 20, 60),
    ("RM-MANI", "Maní blancheado", "FRUTOS SECOS", "kg", "maní", "AMBIENTE", 270, 2.7, 10, 14),
    ("RM-NUEZ", "Nuez mariposa", "FRUTOS SECOS", "kg", "frutos secos", "AMBIENTE", 270, 11.0, 15, 30),
    ("RM-SESA", "Semillas de sésamo descortezado", "SEMILLAS", "kg", "sésamo", "AMBIENTE", 365, 3.8, 15, 30),
    ("RM-CHIA", "Semillas de chía", "SEMILLAS", "kg", "", "AMBIENTE", 365, 5.1, 15, 30),
    ("RM-LINO", "Semillas de lino", "SEMILLAS", "kg", "", "AMBIENTE", 365, 2.4, 15, 30),
    ("RM-GIRA", "Semillas de girasol peladas", "SEMILLAS", "kg", "", "AMBIENTE", 270, 2.9, 15, 30),
    ("RM-SALV", "Salvado de trigo", "HARINAS", "kg", "gluten", "AMBIENTE", 180, 0.4, 10, 30),
    ("RM-SOJA", "Base de salsa de soja", "BASICOS", "kg", "soja,gluten", "AMBIENTE", 540, 2.1, 20, 30),
    ("RM-PTSO", "Proteína texturizada de soja", "BASICOS", "kg", "soja", "AMBIENTE", 365, 2.6, 15, 30),
    ("RM-PAN-RAL", "Pan rallado industrial", "PANADERIA", "kg", "gluten", "AMBIENTE", 180, 1.1, 7, 14),
]

# Primary (food-contact) packaging is lot-traced in batch_consumption. code, description, uom, cost USD per uom
PACK = [
    ("PK-FMET", "Film metalizado bolsa snacks", "kg", 6.2),
    ("PK-FBOP", "Film BOPP flowpack galletitas", "kg", 4.8),
    ("PK-FR250", "Frasco vidrio 250 g boca 63", "un", 0.21),
    ("PK-FR500", "Frasco vidrio 500 g boca 82", "un", 0.29),
    ("PK-FR1K", "Frasco vidrio 1 kg boca 82", "un", 0.41),
    ("PK-FR340", "Frasco vidrio 340 g boca 63", "un", 0.24),
    ("PK-FR190", "Frasco vidrio 190 g boca 58", "un", 0.19),
    ("PK-BALD", "Balde PP 2,9 kg con tapa", "un", 0.88),
    ("PK-TA63", "Tapa twist-off 63 mm", "un", 0.05),
    ("PK-TA82", "Tapa twist-off 82 mm", "un", 0.07),
    ("PK-TA58", "Tapa twist-off 58 mm", "un", 0.045),
    ("PK-FSAC", "Film laminado sachet", "kg", 7.4),
    ("PK-DOYP", "Doypack laminado con pico", "un", 0.12),
    ("PK-BALU", "Bandeja aluminio", "un", 0.16),
    ("PK-BPET", "Bandeja PET termoformada", "un", 0.09),
    ("PK-FTER", "Film termocontraíble", "kg", 3.9),
    ("PK-BLAC", "Bolsa polietileno pan lactal", "un", 0.03),
    ("PK-CAJA", "Caja corrugada (estándar)", "un", 0.42),
]

# ---- Name pools (all fictional) ----------------------------------------------------------------------------------------
FIRST = ["María", "José", "Juan", "Ana", "Carlos", "Laura", "Jorge", "Silvia", "Luis", "Marta", "Pablo", "Claudia",
         "Diego", "Patricia", "Martín", "Gabriela", "Sergio", "Mónica", "Ricardo", "Andrea", "Fernando", "Valeria",
         "Gustavo", "Lorena", "Hernán", "Romina", "Marcelo", "Natalia", "Alejandro", "Carolina", "Sebastián",
         "Florencia", "Matías", "Daniela", "Facundo", "Micaela", "Nicolás", "Agustina", "Lucas", "Camila", "Ezequiel",
         "Brenda", "Maximiliano", "Rocío", "Gonzalo", "Julieta", "Cristian", "Melina", "Damián", "Yamila", "Ramón",
         "Graciela", "Héctor", "Noemí", "Rubén", "Estela", "Walter", "Liliana", "Omar", "Beatriz", "Iván", "Soledad",
         "Leandro", "Paola", "Emiliano", "Vanesa", "Franco", "Antonella", "Tomás", "Milagros", "Joaquín", "Ludmila",
         "Ángel", "Nélida", "Darío", "Celeste", "Mariano", "Jesica", "Rodrigo", "Tamara"]
LAST = ["González", "Rodríguez", "Gómez", "Fernández", "López", "Díaz", "Martínez", "Pérez", "García", "Sánchez",
        "Romero", "Sosa", "Álvarez", "Torres", "Ruiz", "Ramírez", "Flores", "Acosta", "Benítez", "Medina", "Suárez",
        "Herrera", "Aguirre", "Pereyra", "Gutiérrez", "Giménez", "Molina", "Silva", "Castro", "Rojas", "Ortiz",
        "Núñez", "Luna", "Juárez", "Cabrera", "Ríos", "Ferreyra", "Godoy", "Morales", "Domínguez", "Moreno",
        "Peralta", "Vega", "Carrizo", "Quiroga", "Castillo", "Lescano", "Muñoz", "Ojeda", "Ponce", "Vera", "Vázquez",
        "Villalba", "Cardozo", "Navarro", "Coronel", "Figueroa", "Ibáñez", "Paz", "Maldonado", "Ávila", "Correa",
        "Mansilla", "Ayala", "Escobar", "Arce", "Zárate", "Bustos", "Ojeda", "Miño", "Barrios", "Insaurralde",
        "Cáceres", "Galeano", "Toledo", "Leiva", "Montenegro", "Zalazar", "Peña", "Iñíguez"]

PROVINCES_CITIES = {
    "Buenos Aires": ["La Plata", "Mar del Plata", "Bahía Blanca", "Tandil", "Junín", "Pergamino", "Olavarría",
                     "Campana", "Luján", "San Nicolás", "Zárate", "Necochea", "Azul", "Chivilcoy", "Morón", "Berazategui",
                     "San Isidro", "Lomas de Zamora", "Pilar", "Tigre", "Merlo", "Moreno"],
    "CABA": ["Ciudad Autónoma de Buenos Aires"],
    "Santa Fe": ["Rosario", "Santa Fe", "Rafaela", "Venado Tuerto", "Reconquista", "San Lorenzo", "Villa Constitución",
                 "Esperanza", "Casilda"],
    "Córdoba": ["Córdoba", "Río Cuarto", "Villa María", "San Francisco", "Villa Carlos Paz", "Alta Gracia",
                "Bell Ville"],
    "Entre Ríos": ["Paraná", "Concordia", "Gualeguaychú", "Concepción del Uruguay", "Victoria", "Gualeguay",
                   "Colón", "Chajarí"],
    "Mendoza": ["Mendoza", "San Rafael", "Godoy Cruz", "Maipú", "Luján de Cuyo"],
    "Tucumán": ["San Miguel de Tucumán", "Yerba Buena", "Tafí Viejo"],
    "Corrientes": ["Corrientes", "Goya", "Paso de los Libres"],
    "Misiones": ["Posadas", "Oberá", "Eldorado"],
    "Neuquén": ["Neuquén", "Cutral Có", "San Martín de los Andes"],
    "Salta": ["Salta", "Orán", "Tartagal"],
    "Chaco": ["Resistencia", "Presidencia Roque Sáenz Peña"],
    "La Pampa": ["Santa Rosa", "General Pico"],
    "Río Negro": ["Viedma", "General Roca", "San Carlos de Bariloche"],
    "Chubut": ["Comodoro Rivadavia", "Trelew", "Puerto Madryn"],
    "San Juan": ["San Juan", "Rivadavia"],
    "Jujuy": ["San Salvador de Jujuy", "Palpalá"],
}
PROVINCE_WEIGHT = {"Buenos Aires": 30, "CABA": 9, "Santa Fe": 12, "Córdoba": 10, "Entre Ríos": 6, "Mendoza": 5,
                   "Tucumán": 4, "Corrientes": 3, "Misiones": 2.5, "Neuquén": 2.5, "Salta": 2.5, "Chaco": 2,
                   "La Pampa": 1.5, "Río Negro": 2, "Chubut": 2, "San Juan": 2, "Jujuy": 1.5}

# Supermarket chains (fictional). code, name, stores, province bias
CHAINS = [
    ("LAESTRELLA", "Supermercados La Estrella S.A.", 180, None),
    ("COSTAAZUL", "Hipermercados Costa Azul S.A.", 110, "Buenos Aires"),
    ("DELPLATA", "Almacenes del Plata S.R.L.", 140, "CABA"),
    ("NANDU", "Supermercados Ñandú S.A.", 90, "Santa Fe"),
    ("PAMPAGRANDE", "Mercados Pampa Grande S.A.", 120, "Córdoba"),
    ("ELCEIBO", "Autoservicios El Ceibo S.R.L.", 70, "Entre Ríos"),
    ("CUMBRE", "Super Cumbre S.A.", 60, "Mendoza"),
    ("RIOMANSO", "Cadena Río Manso S.A.", 50, "Neuquén"),
]

COMPANY_WORDS_A = ["Distribuidora", "Comercial", "Mayorista", "Logística", "Alimentos", "Servicios Gastronómicos",
                   "Proveeduría", "Abastecedora", "Grupo", "Comercializadora"]
COMPANY_WORDS_B = ["del Litoral", "Andina", "Patagónica", "del Centro", "Norteña", "Pampeana", "del Oeste",
                   "Mesopotámica", "Cuyana", "del Sur", "Serrana", "del Valle", "Ribereña", "Atlántica", "del Plata",
                   "Chaqueña", "Puntana", "Fueguina", "Calchaquí", "Mediterránea"]
COMPANY_WORDS_C = ["Los Aromos", "San Cayetano", "El Progreso", "La Unión", "Don Bosco", "Santa Rita", "El Trébol",
                   "Las Acacias", "La Esperanza", "El Molino", "Los Pinos", "San Expedito", "La Cumbre", "El Faro",
                   "Las Lomas", "Tres Hermanos", "La Querencia", "El Ombú", "Los Tilos", "La Amistad", "El Algarrobo",
                   "San Roque", "Santa Lucía", "El Hornero", "Los Robles", "La Tranquera", "El Mirador", "Las Palmas",
                   "San Jorge", "La Estancia", "Monte Verde", "Las Marías", "El Remanso", "Los Cardales"]
LEGAL = ["S.A.", "S.R.L.", "S.A.S.", "SA", "SRL", "e Hijos S.R.L.", "S.C."]
FS_KINDS = ["Restaurante", "Parrilla", "Hotel", "Catering", "Comedor", "Pizzería", "Bar", "Cervecería", "Rotisería",
            "Club", "Concesionaria de comedores", "Hamburguesería", "Cantina"]
EXPORT_CUSTOMERS = [
    ("UY", "Uruguay", ["Distribuidora Oriental Ltda.", "Alimentos del Este S.A.", "Comercial Rambla S.A.",
                       "Importadora Charrúa S.R.L.", "Mercado Ciudad Vieja S.A.", "Provisiones Canelones Ltda.",
                       "Distribuidora Salto Grande S.A.", "Almacenes Colonia S.A."]),
    ("PY", "Paraguay", ["Importadora Guaraní S.A.", "Comercial Asunción S.A.E.", "Distribuidora Itapúa S.R.L.",
                        "Alimentos Chaco Boreal S.A.", "Supermercados Ñemby S.A.", "Proveedora Encarnación S.A."]),
    ("CL", "Chile", ["Comercializadora Andes Sur Ltda.", "Distribuidora Pacífico SpA", "Importadora Cordillera SpA",
                     "Alimentos Valparaíso Ltda.", "Supermercados Austral SpA", "Comercial Biobío Ltda.",
                     "Distribuidora Atacama SpA"]),
    ("BO", "Bolivia", ["Importadora Altiplano S.R.L.", "Comercial Santa Cruz Ltda.", "Distribuidora Illimani S.A.",
                       "Alimentos Chuquisaca S.R.L."]),
    ("BR", "Brasil", ["Importadora Gaúcha Ltda.", "Distribuidora Paraná Alimentos Ltda.",
                      "Comercial Catarinense Ltda.", "Empório Porto Alegre Ltda.", "Atacadista Serra Gaúcha Ltda."]),
    ("US", "Estados Unidos", ["Southern Cone Foods LLC", "Pampas Imports Inc.", "Latin Pantry Distributors LLC",
                              "Gaucho Gourmet Trading Inc.", "Rio Plata Specialty Foods LLC"]),
    ("ES", "España", ["Importaciones Iberoplata S.L.", "Distribuciones Atlántico Sur S.L.",
                      "Comercial Ultramar S.L.", "Sabores del Río S.L."]),
]

SUPPLIER_CATEGORY_NAMES = {
    "TUBERCULOS": ["Establecimiento {c}", "Agropecuaria {c}", "Productores Papa {b}", "Campo {c}",
                   "Cooperativa Papera {b}", "Chacra {c}", "Hortícola {c}"],
    "ACEITES": ["Aceitera {b}", "Oleaginosa {c}", "Refinería {b}", "Aceites {c}"],
    "HARINAS": ["Molino Harinero {c}", "Molinos {b}", "Harinera {c}"],
    "ESPECIAS": ["Especias {b}", "Molinos Especieros {b}", "Condimentos {c}", "Especiería {c}"],
    "LACTEOS": ["Láctea {c}", "Usina Láctea {b}", "Quesería {c}", "Lácteos {b}"],
    "CARNES": ["Frigorífico {c}", "Frigorífico {b}", "Avícola {c}", "Chacinados {c}"],
    "PACKAGING": ["Envases {b}", "Cristalería {c}", "Flexibles {b}", "Cartonera {c}", "Plásticos {c}",
                  "Impresiones Flexográficas {b}", "Tapas y Cierres {b}"],
    "MRO": ["Repuestos Industriales {c}", "Rodamientos {b}", "Servicios Técnicos {c}", "Química Industrial {b}",
            "Electromecánica {c}", "Neumática {b}"],
    "VARIOS": ["Proveeduría {c}", "Insumos Alimenticios {b}", "Aditivos {b}", "Distribuidora de Insumos {c}"],
}

RM_SUPPLIER_CATEGORY = {
    "TUBERCULOS": "TUBERCULOS", "ACEITES": "ACEITES", "HARINAS": "HARINAS", "PANADERIA": "VARIOS",
    "BASICOS": "VARIOS", "GRASAS": "LACTEOS", "LACTEOS": "LACTEOS", "HUEVO": "VARIOS", "CARNES": "CARNES",
    "PESCADOS": "CARNES", "VEGETALES": "VARIOS", "TOMATE": "VARIOS", "ESPECIAS": "ESPECIAS", "ADITIVOS": "VARIOS",
    "AROMAS": "VARIOS", "FRUTOS SECOS": "VARIOS", "SEMILLAS": "VARIOS", "SERVICIOS": None, "PACKAGING": "PACKAGING",
}

EQUIPMENT_BY_FAMILY = {
    "CHIPS": ["Lavadora-peladora", "Rebanadora", "Freidora continua", "Tambor saborizador", "Envasadora vertical",
              "Detector de metales", "Controladora de peso"],
    "CRACKERS": ["Amasadora", "Laminadora", "Horno túnel", "Enfriador", "Envasadora flowpack", "Detector de metales",
                 "Controladora de peso"],
    "SAUCE_JAR": ["Emulsionador", "Marmita", "Llenadora volumétrica", "Tapadora", "Pasteurizador túnel",
                  "Etiquetadora", "Detector de metales"],
    "SAUCE_SACHET": ["Marmita", "Envasadora de sachets multipista", "Envasadora doypack", "Selladora",
                     "Encajonadora", "Controladora de peso"],
    "FROZEN": ["Amasadora", "Formadora", "Horno", "Túnel de congelado espiral", "Envasadora termoformadora",
               "Detector de metales"],
    "BAKERY": ["Amasadora espiral", "Divisora-boleadora", "Cámara de fermentación", "Horno túnel", "Rebanadora",
               "Embolsadora"],
}
MANUFACTURERS = ["Industrias Metalúrgicas Ceres", "Maquinarias Paraná", "TecnoPack Rosario", "Hornos Galván",
                 "Frío Industrial Austral", "Envasadoras del Litoral", "Automatización Pampa", "Inoxidables Rafaela",
                 "Balanzas Precisa", "Termoformados del Sur"]
