"""Generate FreshCart's synthetic source data, exactly as the source systems would deliver it.

The output is deliberately messy. Every problem planted here is one that real retail
feeds have, and every one of them is fixed somewhere in the pipeline:

  POS            dates as strings, ids with leading zeros, returns with positive values,
                 voided lines, training-mode registers, a re-delivered day of data,
                 a few corrupt rows, an item missing from the product master, CAD amounts
  E-commerce     nested JSON, timestamps with UTC offsets, ids without leading zeros,
                 cancelled orders
  ERP products   one row per SKU *per sales organisation*, category codes instead of names,
                 Canada-only items, discontinued items
  Store master   monthly snapshots (history must be derived), codes for region/format/state,
                 US-style MM/DD/YYYY dates
  CRM            personal data (email, date of birth) that must never reach gold,
                 new members missing from the extract
  Finance        fiscal calendar with DD/MM/YYYY dates and text labels ("FY2026", "P01"),
                 FX rates for business days only
  Inventory      daily snapshots with occasional negative stock

Run:  python -m freshcart.generate
"""
from __future__ import annotations

import csv
import json
import math
import random
import shutil
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import config as C

rng = random.Random(C.RANDOM_SEED)


def d(s: str) -> date:
    return date.fromisoformat(s)


def daterange(a: date, b: date):
    x = a
    while x <= b:
        yield x
        x += timedelta(days=1)


# --------------------------------------------------------------------------------------
# Fiscal calendar (4-5-4, year ends on the Saturday nearest 31 January)
# --------------------------------------------------------------------------------------
def fiscal_year_end(fy: int) -> date:
    target = date(fy + 1, 1, 31)
    delta = (5 - target.weekday()) % 7          # days forward to Saturday
    if delta > 3:
        delta -= 7                              # nearer to go back
    return target + timedelta(days=delta)


PERIOD_WEEKS = [4, 5, 4] * 4
PERIOD_NAMES = ["Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan"]


def build_fiscal_calendar(first_fy=2010, last_fy=2027):
    rows = []
    for fy in range(first_fy, last_fy + 1):
        start = fiscal_year_end(fy - 1) + timedelta(days=1)
        end = fiscal_year_end(fy)
        for day in daterange(start, end):
            doy = (day - start).days + 1
            week = (doy - 1) // 7 + 1
            cum, period = 0, 12
            for p, w in enumerate(PERIOD_WEEKS, start=1):
                cum += w
                if week <= cum:
                    period = p
                    break
            rows.append(dict(date=day, fy=fy, quarter=(period - 1) // 3 + 1, period=period,
                             week=week, week_start=start + timedelta(days=(week - 1) * 7), doy=doy))
    return rows


FISCAL = build_fiscal_calendar()
FISCAL_BY_DATE = {r["date"]: r for r in FISCAL}


# --------------------------------------------------------------------------------------
# Master data
# --------------------------------------------------------------------------------------
REGIONS = {"01": "Southeast", "02": "Northeast", "03": "Midwest", "04": "West", "05": "Canada"}
FORMATS = {"SC": "Supercenter", "NM": "Neighborhood Market", "EXP": "Express", "DRK": "Dark Store"}
STATES = {"MA": ("Massachusetts", "US"), "CT": ("Connecticut", "US"), "NJ": ("New Jersey", "US"),
          "GA": ("Georgia", "US"), "IL": ("Illinois", "US"), "CO": ("Colorado", "US"),
          "ON": ("Ontario", "CA")}

# store_nbr, name, city, state, format, region, open, close, sqft, utc offset (winter), txn/day base
STORES = [
    ("01042", "FreshCart Boston Seaport", "Boston", "MA", "NM", "02", "2016-05-14", None, 38000, -5, 7),
    ("01107", "FreshCart Hartford West", "West Hartford", "CT", "EXP", "02", "2019-10-05", None, 9500, -5, 4),
    ("09001", "FreshCart Newark Fulfilment Center", "Newark", "NJ", "DRK", "02", "2023-03-11", None, 0, -5, 0),
    ("02210", "FreshCart Atlanta Midtown", "Atlanta", "GA", "NM", "01", "2014-08-09", None, 36000, -5, 7),
    ("02211", "FreshCart Atlanta Buckhead", "Atlanta", "GA", "EXP", "01", "2017-03-18", "2026-05-30", 8800, -5, 4),
    ("02305", "FreshCart Chicago Lincoln Park", "Chicago", "IL", "SC", "03", "2012-04-21", None, 61000, -6, 10),
    ("03050", "FreshCart Denver Highlands", "Denver", "CO", "NM", "04", "2024-09-07", None, 34000, -7, 7),
    ("00317", "FreshCart Toronto Liberty Village", "Toronto", "ON", "SC", "05", "2018-06-02", None, 55000, -5, 10),
    ("00322", "FreshCart Mississauga Square One", "Mississauga", "ON", "EXP", "05", "2025-06-14", None, 9200, -5, 4),
]
STORE = {s[0]: s for s in STORES}
BOSTON_REMODEL = d("2026-02-01")                 # Neighborhood Market -> Supercenter
CANADA = {"00317", "00322"}

# Product catalog: (matkl, department, category, subcategory, uom, [(item_nbr, name, brand, usd_price)])
PL, PLS = "FreshCart", "FreshCart Select"
CATALOG = [
    ("P0101", "Produce", "Fresh Fruit", "Bananas and Tropical", "KG", [
        ("93321", "Bananas", "Sunny Acres", 1.49), ("93322", "Organic Bananas", PLS, 1.99), ("93325", "Mangoes", "Sunny Acres", 3.99)]),
    ("P0102", "Produce", "Fresh Fruit", "Apples and Pears", "KG", [
        ("93401", "Honeycrisp Apples", "Maple Ridge Farms", 5.49), ("93402", "Gala Apples", PL, 3.29), ("93410", "Bartlett Pears", "Maple Ridge Farms", 3.99)]),
    ("P0103", "Produce", "Fresh Fruit", "Berries", "EA", [
        ("93501", "Strawberries 450g", "Sunny Acres", 4.49), ("93502", "Blueberries 170g", "Sunny Acres", 3.99), ("93503", "Raspberries 170g", PLS, 4.29)]),
    ("P0201", "Produce", "Fresh Vegetables", "Salad Greens", "EA", [
        ("94101", "Spring Mix 142g", PL, 3.99), ("94102", "Romaine Hearts 3pk", "Green Valley", 3.49), ("94103", "Baby Spinach 142g", "Green Valley", 3.79)]),
    ("P0202", "Produce", "Fresh Vegetables", "Root Vegetables", "KG", [
        ("94201", "Carrots", PL, 1.69), ("94202", "Yellow Onions", "Green Valley", 1.99), ("94203", "Russet Potatoes", "Green Valley", 1.49)]),
    ("P0203", "Produce", "Fresh Vegetables", "Tomatoes and Peppers", "KG", [
        ("94301", "Vine Tomatoes", "Green Valley", 4.39), ("94302", "Red Bell Peppers", "Green Valley", 5.49)]),
    ("B0101", "Bakery", "Bread", "Sliced Bread", "EA", [
        ("20101", "White Sandwich Bread 675g", PL, 2.49), ("20102", "Whole Wheat Bread 675g", "Baker's Row", 3.49), ("20103", "Sourdough Loaf", PLS, 4.99)]),
    ("B0201", "Bakery", "Sweet Bakery", "Pastries and Muffins", "EA", [
        ("20201", "Butter Croissants 4pk", PLS, 5.49), ("20202", "Blueberry Muffins 4pk", "Baker's Row", 5.99), ("20203", "Cinnamon Rolls 6pk", "Baker's Row", 6.49)]),
    ("M0101", "Meat and Seafood", "Fresh Meat", "Beef", "EA", [
        ("30101", "Ground Beef 85% 500g", PL, 6.99), ("30102", "Ribeye Steak 340g", "Prairie Gold", 14.99), ("30103", "Stewing Beef 450g", "Prairie Gold", 8.99)]),
    ("M0102", "Meat and Seafood", "Fresh Meat", "Poultry", "EA", [
        ("30201", "Chicken Breasts 700g", PL, 9.99), ("30202", "Whole Chicken", "Prairie Gold", 11.49), ("30203", "Chicken Thighs 900g", PL, 8.49)]),
    ("M0201", "Meat and Seafood", "Seafood", "Fresh Fish", "KG", [
        ("31101", "Atlantic Salmon Fillet", "Coastline", 24.99), ("31102", "Cod Fillet", "Coastline", 19.99)]),
    ("D0101", "Deli", "Deli Counter", "Sliced Meats and Cheese", "KG", [
        ("40101", "Smoked Turkey Breast", PLS, 18.99), ("40102", "Black Forest Ham", "Old Mill Deli", 16.99)]),
    ("D0201", "Deli", "Ready Meals", "Prepared Meals", "EA", [
        ("40201", "Chicken Caesar Salad Bowl", PL, 8.99), ("40202", "Lasagna Family Tray", PLS, 14.99), ("40203", "Sushi Combo 12pc", "Harbor Sushi", 11.99)]),
    ("E0101", "Dairy and Eggs", "Milk and Cream", "Milk", "EA", [
        ("5012", "Whole Milk 4L", PL, 3.99), ("5013", "2% Milk 4L", PL, 3.89), ("5020", "Oat Beverage 1.75L", "Northfield", 4.49)]),
    ("E0201", "Dairy and Eggs", "Cheese", "Cheese", "EA", [
        ("5101", "Aged Cheddar 400g", "Northfield", 7.99), ("5102", "Mozzarella 320g", PL, 5.49), ("5103", "Brie 200g", PLS, 6.99)]),
    ("E0301", "Dairy and Eggs", "Eggs", "Eggs", "EA", [
        ("5201", "Large Eggs 12pk", PL, 3.99), ("5202", "Free Range Eggs 12pk", "Sunny Acres", 5.99)]),
    ("E0401", "Dairy and Eggs", "Yogurt", "Yogurt", "EA", [
        ("5301", "Greek Yogurt Plain 750g", "Northfield", 5.49), ("5302", "Vanilla Yogurt 4pk", PL, 3.49)]),
    ("C0412", "Grocery", "Snacks", "Chips and Crisps", "EA", [
        ("4471023", "Crunchy Sea Salt Chips 200g", "Crunchwell", 3.49), ("4471024", "BBQ Kettle Chips 200g", "Crunchwell", 3.49),
        ("4471030", "Tortilla Chips 300g", "Golden Hollow", 3.99), ("4471040", "Lightly Salted Chips 200g", PL, 2.49)]),
    ("C0413", "Grocery", "Snacks", "Cookies and Biscuits", "EA", [
        ("4472001", "Chocolate Chip Cookies 300g", "Golden Hollow", 3.99), ("4472002", "Butter Shortbread 250g", PLS, 4.49),
        ("4472003", "Oat Crunch Biscuits 400g", "Golden Hollow", 3.29)]),
    ("C0414", "Grocery", "Snacks", "Chocolate and Candy", "EA", [
        ("4473001", "Dark Chocolate Bar 100g", "Velvet Peak", 2.99), ("4473002", "Milk Chocolate Bar 100g", "Velvet Peak", 2.79),
        ("4473003", "Gummy Bears 350g", "Candy Lane", 3.49)]),
    ("C0421", "Grocery", "Beverages", "Soft Drinks", "EA", [
        ("4481001", "Cola 12x355ml", "Brightfizz", 7.99), ("4481002", "Lemon Lime Soda 12x355ml", "Brightfizz", 7.99),
        ("4481003", "Sparkling Water Lime 8pk", PL, 4.99)]),
    ("C0422", "Grocery", "Beverages", "Coffee and Tea", "EA", [
        ("4482001", "Medium Roast Ground Coffee 925g", "Morning Harbor", 13.99), ("4482002", "Espresso Beans 1kg", PLS, 17.99),
        ("4482003", "Orange Pekoe Tea 72pk", "Morning Harbor", 5.49)]),
    ("C0423", "Grocery", "Beverages", "Bottled Water", "EA", [
        ("4483001", "Spring Water 24x500ml", "Summit Spring", 4.99), ("4483002", "Spring Water 24x500ml", PL, 3.49)]),
    ("C0431", "Grocery", "Pantry", "Pasta and Rice", "EA", [
        ("4491001", "Spaghetti 900g", "Pantry Lane", 2.49), ("4491002", "Basmati Rice 2kg", PL, 6.99), ("4491003", "Penne 900g", PL, 1.99)]),
    ("C0432", "Grocery", "Pantry", "Canned Goods", "EA", [
        ("4492001", "Diced Tomatoes 796ml", PL, 1.79), ("4492002", "Chickpeas 540ml", "Pantry Lane", 1.49), ("4492003", "Tuna Chunks 170g", "Coastline", 2.29)]),
    ("C0433", "Grocery", "Pantry", "Breakfast Cereal", "EA", [
        ("4493001", "Honey Oat Clusters 500g", "Harvest Moon", 5.49), ("4493002", "Corn Flakes 750g", PL, 3.99)]),
    ("H0101", "Household", "Cleaning", "Laundry", "EA", [
        ("551020", "Laundry Detergent 2.95L", "Clearbright", 13.99), ("551021", "Laundry Pods 42ct", PL, 11.99)]),
    ("H0102", "Household", "Cleaning", "Surface Cleaners", "EA", [
        ("552001", "Multi-Surface Spray 950ml", "Clearbright", 4.49), ("552002", "Disinfecting Wipes 80ct", PL, 3.99)]),
    ("H0201", "Household", "Paper Goods", "Paper Towels", "EA", [
        ("561001", "Paper Towels 6 Rolls", "SoftLeaf", 9.99), ("561002", "Paper Towels 6 Rolls", PL, 7.49)]),
    ("H0202", "Household", "Paper Goods", "Toilet Tissue", "EA", [
        ("562001", "Toilet Tissue 12 Rolls", "SoftLeaf", 10.99), ("562002", "Toilet Tissue 12 Rolls", PL, 8.49)]),
]
CANADA_ONLY = [("C0432", "4492050", "Maple Baked Beans 398ml", "Pantry Lane", 2.19),
               ("C0413", "4472050", "Maple Leaf Cream Cookies 350g", "Golden Hollow", 3.99)]
DISCONTINUED = {"4471024", "20203", "4481002"}
COST_RATIO = {"Produce": 0.62, "Bakery": 0.52, "Meat and Seafood": 0.71, "Deli": 0.58,
              "Dairy and Eggs": 0.73, "Grocery": 0.64, "Household": 0.66}

PRODUCTS = {}   # item_nbr -> dict
for matkl, dept, cat, sub, uom, items in CATALOG:
    for nbr, name, brand, price in items:
        PRODUCTS[nbr] = dict(item=nbr, name=name, brand=brand, price=price, matkl=matkl, dept=dept,
                             cat=cat, sub=sub, uom=uom, canada_only=False)
for matkl, nbr, name, brand, price in CANADA_ONLY:
    base = next(c for c in CATALOG if c[0] == matkl)
    PRODUCTS[nbr] = dict(item=nbr, name=name, brand=brand, price=price, matkl=matkl, dept=base[1],
                         cat=base[2], sub=base[3], uom="EA", canada_only=True)
for p in PRODUCTS.values():
    ratio = COST_RATIO[p["dept"]] - (0.08 if p["brand"] in (PL, PLS) else 0.0)
    p["cost"] = round(p["price"] * ratio, 2)

DEPT_MIX = {
    "SC": {"Produce": 18, "Bakery": 8, "Meat and Seafood": 9, "Deli": 6, "Dairy and Eggs": 16, "Grocery": 33, "Household": 10},
    "NM": {"Produce": 19, "Bakery": 8, "Meat and Seafood": 8, "Deli": 7, "Dairy and Eggs": 17, "Grocery": 32, "Household": 9},
    "EXP": {"Produce": 8, "Bakery": 11, "Meat and Seafood": 2, "Deli": 13, "Dairy and Eggs": 18, "Grocery": 43, "Household": 5},
    "ONLINE": {"Produce": 17, "Bakery": 6, "Meat and Seafood": 9, "Deli": 4, "Dairy and Eggs": 16, "Grocery": 33, "Household": 15},
}


def price_in(item: str, store_nbr: str) -> float:
    usd = PRODUCTS[item]["price"]
    if store_nbr in CANADA:
        return round(round(usd * 1.36, 1) - 0.01, 2)
    return usd


def store_format_on(store_nbr: str, day: date) -> str:
    if store_nbr == "01042" and day >= BOSTON_REMODEL:
        return "SC"
    return STORE[store_nbr][4]


def store_open_on(store_nbr: str, day: date) -> bool:
    s = STORE[store_nbr]
    return d(s[6]) <= day and (s[7] is None or day <= d(s[7]))


def utc_offset(store_nbr: str, day: date) -> int:
    base = STORE[store_nbr][9]
    # North American DST: second Sunday in March to first Sunday in November
    y = day.year
    mar = date(y, 3, 8) + timedelta(days=(6 - date(y, 3, 8).weekday()) % 7)
    nov = date(y, 11, 1) + timedelta(days=(6 - date(y, 11, 1).weekday()) % 7)
    return base + 1 if mar <= day < nov else base


# --------------------------------------------------------------------------------------
# Promotions
# --------------------------------------------------------------------------------------
MECH_PREFIX = {"BOGO": "BG", "PCT": "PC", "MULTI": "MB", "LOY": "LY"}
MECH_DESC = {"BOGO": "Buy one get one free", "PCT": "{pct}% off", "MULTI": "3 for the price of 2",
             "LOY": "Members save 15%"}
THEMES = ["Weekend Savings", "Snack Attack", "Fresh Deals", "Pantry Stock-Up", "Summer BBQ", "Back to School",
          "Holiday Hosting", "Healthy Start", "Game Day", "Clean Home", "Family Favourites", "Big Brands"]


def build_promotions():
    promos, items = [], []
    start = C.SALES_START
    item_ids = [i for i in PRODUCTS if i not in DISCONTINUED]
    week = 0
    while start <= C.SALES_END:
        for _ in range(2 if rng.random() < 0.45 else 1):
            mech = rng.choices(["BOGO", "PCT", "MULTI", "LOY"], [3, 4, 2, 2])[0]
            length = rng.choice([7, 7, 14])
            f = FISCAL_BY_DATE[start]
            code = f"{MECH_PREFIX[mech]}{str(f['fy'])[2:]}W{f['week']:02d}"
            if code == "BG26W33" or any(p["code"] == code for p in promos):   # BG26W33 is reserved for the golden receipt
                code += "B"
            pct = rng.choice([20, 25, 30]) if mech == "PCT" else (15 if mech == "LOY" else None)
            promos.append(dict(code=code, mech=mech, start=start, end=start + timedelta(days=length - 1),
                               pct=pct, desc=f"{rng.choice(THEMES)} - {MECH_DESC[mech].format(pct=pct)}"))
            for it in rng.sample(item_ids, rng.randint(3, 6)):
                items.append((code, it))
        start += timedelta(days=7)
        week += 1
    # the promotion used by the "golden receipt" traced through the docs
    promos.append(dict(code="BG26W33", mech="BOGO", start=d("2026-09-13"), end=d("2026-09-19"), pct=None,
                       desc="Snack Attack - Buy one get one free"))
    items.append(("BG26W33", "4471023"))
    return promos, items


PROMOS, PROMO_ITEMS = build_promotions()
PROMO_BY_CODE = {p["code"]: p for p in PROMOS}
_promo_index = defaultdict(list)            # item -> promos
for code, it in PROMO_ITEMS:
    _promo_index[it].append(PROMO_BY_CODE[code])


def active_promo(item: str, day: date):
    for p in _promo_index.get(item, []):
        if p["start"] <= day <= p["end"]:
            return p
    return None


# --------------------------------------------------------------------------------------
# Loyalty members
# --------------------------------------------------------------------------------------
CRM_EXTRACT_DATE = d("2026-09-06")


def build_members(n=1400):
    members = []
    used = set()
    home_stores = [s[0] for s in STORES if s[4] != "DRK"]
    for i in range(n):
        while True:
            card = "60" + "".join(rng.choice("0123456789") for _ in range(8))
            if card not in used:
                used.add(card)
                break
        enroll = date(2015, 1, 1) + timedelta(days=rng.randint(0, (d("2026-09-30") - date(2015, 1, 1)).days))
        dob = date(1948, 1, 1) + timedelta(days=rng.randint(0, 365 * 57))
        tier = rng.choices(["G", "S", "B"], [15, 35, 50])[0]
        members.append(dict(card=card, email=f"member{i + 1:05d}@example.com", dob=dob, tier=tier,
                            enroll=enroll, home=rng.choice(home_stores)))
    # the golden receipt's customer
    members[0].update(card="6034118822", tier="G", enroll=d("2019-04-02"), home="01042")
    # members who joined after the CRM extract: they shop, but are not in the extract yet
    for i, m in enumerate(members[1:25], start=1):
        m["enroll"] = d("2026-09-07") + timedelta(days=i % 12)
    return members


MEMBERS = build_members()
MEMBERS_BY_HOME = defaultdict(list)
for m in MEMBERS:
    MEMBERS_BY_HOME[m["home"]].append(m)


def pick_card(store_nbr: str, day: date):
    """45% of baskets scan a loyalty card, mostly members whose home store this is."""
    if rng.random() > 0.45:
        return None
    pool = MEMBERS_BY_HOME.get(store_nbr) if rng.random() < 0.8 else MEMBERS
    pool = pool or MEMBERS
    m = rng.choice(pool)
    return m["card"] if m["enroll"] <= day else None


# --------------------------------------------------------------------------------------
# Baskets
# --------------------------------------------------------------------------------------
MONTH_FACTOR = {1: .92, 2: .95, 3: .98, 4: 1.0, 5: 1.02, 6: 1.04, 7: 1.06, 8: 1.04, 9: .99, 10: 1.0, 11: 1.08, 12: 1.22}
DOW_FACTOR = {0: .88, 1: .86, 2: .90, 3: .97, 4: 1.10, 5: 1.30, 6: 1.15}
_items_by_dept = defaultdict(list)
for _it, _p in PRODUCTS.items():
    _items_by_dept[_p["dept"]].append(_it)


def expected_baskets(store_nbr: str, day: date) -> float:
    base = STORE[store_nbr][10]
    if store_nbr == "01042" and day >= BOSTON_REMODEL:
        base = 10
    growth = 1.045 if day >= d("2026-02-01") else 1.0
    return base * MONTH_FACTOR[day.month] * DOW_FACTOR[day.weekday()] * growth


def poissonish(mu: float) -> int:
    return max(0, int(round(rng.gauss(mu, math.sqrt(mu)))))


def choose_item(mix_key: str, store_nbr: str, day: date) -> str:
    mix = DEPT_MIX[mix_key]
    dept = rng.choices(list(mix), list(mix.values()))[0]
    cands = [i for i in _items_by_dept[dept]
             if (store_nbr in CANADA or not PRODUCTS[i]["canada_only"])
             and not (i in DISCONTINUED and day >= d("2026-03-01"))]
    weights = [2.2 if active_promo(i, day) else 1.0 for i in cands]
    return rng.choices(cands, weights)[0]


def make_line(item: str, store_nbr: str, day: date, card):
    """Return (qty, unit_price, extended, discount, promo_code)."""
    p = PRODUCTS[item]
    unit = price_in(item, store_nbr)
    promo = active_promo(item, day)
    if p["uom"] == "KG":
        qty = round(rng.uniform(0.25, 2.2), 3)
    else:
        qty = rng.choices([1, 2, 3], [78, 17, 5])[0]
        if promo and promo["mech"] == "BOGO" and rng.random() < 0.7:
            qty = 2
        if promo and promo["mech"] == "MULTI" and rng.random() < 0.5:
            qty = 3
    ext = round(qty * unit, 2)
    disc = 0.0
    if promo:
        m = promo["mech"]
        if m == "BOGO" and p["uom"] == "EA":
            disc = unit * (qty // 2)
        elif m == "PCT":
            disc = ext * promo["pct"] / 100
        elif m == "MULTI" and p["uom"] == "EA":
            disc = unit * (qty // 3)
        elif m == "LOY" and card:
            disc = ext * 0.15
    disc = round(disc, 2)
    return qty, unit, ext, disc, (promo["code"] if disc > 0 else "")


TAXABLE = {"Household", "Grocery"}


def pos_rows_for_day(store_nbr: str, day: date, counter):
    rows = []
    fmt = store_format_on(store_nbr, day)
    n = poissonish(expected_baskets(store_nbr, day))
    for _ in range(n):
        trx = str(next(counter))
        reg = f"{rng.randint(1, 6):02d}"
        is_training = rng.random() < 0.003
        if is_training:
            reg = "99"
        secs = rng.randint(7 * 3600, 22 * 3600)
        tm = f"{secs // 3600}{(secs % 3600) // 60:02d}{secs % 60:02d}"          # HMMSS, no leading zero
        card = pick_card(store_nbr, day)
        tender = rng.choice(["CC", "DB", "CS", "GC", "CC", "DB"])
        is_return = rng.random() < 0.012
        lines = []
        if is_return:
            item = choose_item(fmt, store_nbr, day)
            qty = 1 if PRODUCTS[item]["uom"] == "EA" else round(rng.uniform(0.3, 1.5), 3)
            unit = price_in(item, store_nbr)
            lines.append((item, qty, unit, round(qty * unit, 2), 0.0, "", "R"))
        else:
            for _ in range(rng.choices([1, 2, 3, 4, 5, 6], [22, 24, 20, 15, 11, 8])[0]):
                item = choose_item(fmt, store_nbr, day)
                qty, unit, ext, disc, promo = make_line(item, store_nbr, day, card)
                lines.append((item, qty, unit, ext, disc, promo, "S"))
            if rng.random() < 0.01:                                            # scanned twice, then voided
                it, qty, unit, ext, disc, promo, _t = rng.choice(lines)
                lines.append((it, qty, unit, ext, 0.0, "", "V"))
        for ln, (item, qty, unit, ext, disc, promo, typ) in enumerate(lines, start=1):
            tax = round((ext - disc) * 0.0625, 2) if PRODUCTS[item]["dept"] in TAXABLE else 0.0
            rows.append([trx, store_nbr, reg, str(ln), day.strftime("%Y%m%d"), tm, item.zfill(12),
                         fmt_qty(qty), f"{unit:.2f}", f"{ext:.2f}", f"{disc:.2f}", f"{tax:.2f}", typ,
                         card or "", promo, "CAD" if store_nbr in CANADA else "USD", tender])
    return rows


def fmt_qty(q) -> str:
    return str(q) if isinstance(q, int) else f"{q:.3f}"


POS_HEADER = ["TRX_ID", "STR_NBR", "REG_NBR", "LN_NBR", "TRX_DT", "TRX_TM", "ITM_ID", "QTY", "UNIT_PRC",
              "EXT_AMT", "DISC_AMT", "TAX_AMT", "TRX_TYP", "LYL_CARD_NBR", "PROMO_CD", "CRNCY_CD", "TNDR_CD"]


def golden_rows():
    """Two hand-written receipts that the documentation follows through every layer."""
    return {
        (d("2026-09-14"), "01042"): [
            ["88120431", "01042", "03", "1", "20260914", "93015", "000004471023", "2", "3.49", "6.98", "3.49", "0.22", "S", "6034118822", "BG26W33", "USD", "CC"],
            ["88120431", "01042", "03", "2", "20260914", "93015", "000000093321", "1.254", "1.49", "1.87", "0.00", "0.00", "S", "6034118822", "", "USD", "CC"],
            ["88120431", "01042", "03", "3", "20260914", "93015", "000004471023", "1", "3.49", "3.49", "0.00", "0.00", "V", "6034118822", "", "USD", "CC"],
            ["88120431", "01042", "03", "4", "20260914", "93015", "000000005012", "1", "3.99", "3.99", "0.00", "0.00", "S", "6034118822", "", "USD", "CC"],
        ],
        (d("2026-09-14"), "00317"): [
            ["88120519", "00317", "01", "1", "20260914", "94210", "000000551020", "1", "18.99", "18.99", "0.00", "0.00", "R", "", "", "CAD", "CC"],
        ],
    }


def corrupt(rows, n_bad_store=6, n_bad_date=4, n_bad_qty=3, n_unknown_item=5):
    """Plant a handful of broken rows, the kind a POS feed really produces."""
    for r in rng.sample(rows, n_bad_store):
        r[1] = ""
    for r in rng.sample(rows, n_bad_date):
        r[4] = r[4][:4] + "0230"                                               # 30 February
    for r in rng.sample(rows, n_bad_qty):
        r[7] = "9999"
    for r in rng.sample(rows, n_unknown_item):
        r[6] = "000009999999"                                                  # not in the product master


# --------------------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------------------
def write_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def generate_pos():
    counter = iter(range(70_000_001, 99_999_999))
    golden = golden_rows()
    by_month = defaultdict(list)
    for day in daterange(C.SALES_START, C.SALES_END):
        for s in STORES:
            nbr = s[0]
            if s[4] == "DRK" or not store_open_on(nbr, day):
                continue
            rows = pos_rows_for_day(nbr, day, counter)
            rows += golden.get((day, nbr), [])
            by_month[day.strftime("%Y%m")].extend(rows)
    months = sorted(by_month)
    for m in months:
        rows = by_month[m]
        if m in ("202504", "202511", "202607"):
            corrupt(rows)
        if m == "202506":                                                      # the store system re-sent 10 June
            rows += [list(r) for r in rows if r[4] == "20250610"]
        write_csv(C.RAW_DIR / "pos" / f"pos_tlog_{m}.csv", POS_HEADER, rows)
    return sum(len(v) for v in by_month.values())


def generate_ecommerce():
    fulfil = {"09001": 9, "00317": 2, "02305": 2}
    by_month = defaultdict(list)
    n = 1_000_000
    for day in daterange(C.SALES_START, C.SALES_END):
        for nbr, base in fulfil.items():
            mu = base * MONTH_FACTOR[day.month] * (1.045 if day >= d("2026-02-01") else 1.0) * (1.1 if day.weekday() in (5, 6) else 1.0)
            for _ in range(poissonish(mu)):
                n += 1
                secs = rng.randint(6 * 3600, 23 * 3600 + 3599)
                off = utc_offset(nbr, day)
                ts = f"{day.isoformat()}T{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}{'-' if off < 0 else '+'}{abs(off):02d}:00"
                card = pick_card(nbr, day)
                lines = []
                for ln in range(1, rng.randint(3, 11)):
                    item = choose_item("ONLINE", nbr, day)
                    qty, unit, ext, disc, promo = make_line(item, nbr, day, card)
                    lines.append({"lineNo": ln, "sku": item, "qty": qty, "unitPrice": unit,
                                  "lineTotal": ext, "discount": disc, "promoCode": promo or None})
                by_month[day.strftime("%Y%m")].append({
                    "orderId": f"W{n}", "orderTs": ts, "fulfilmentStoreId": nbr.lstrip("0"),
                    "loyaltyId": int(card) if card else None, "currency": "CAD" if nbr in CANADA else "USD",
                    "status": "CANCELLED" if rng.random() < 0.03 else "DELIVERED", "lines": lines})
    for m, orders in sorted(by_month.items()):
        p = C.RAW_DIR / "ecommerce" / f"ecom_orders_{m}.ndjson"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            for o in orders:
                f.write(json.dumps(o) + "\n")
    return sum(len(v) for v in by_month.values())


def generate_products():
    rows = []
    for it, p in PRODUCTS.items():
        status = "D" if it in DISCONTINUED else "A"
        if not p["canada_only"]:
            rows.append([it, "US01", p["name"], p["matkl"], p["brand"], p["uom"], status])
        # the Canadian sales org keeps its own copy, upper-cased, and never flags discontinuations
        rows.append([it, "CA01", p["name"].upper(), p["matkl"], p["brand"], p["uom"], "A"])
    rng.shuffle(rows)
    write_csv(C.RAW_DIR / "erp" / "product_master.csv",
              ["MATNR", "VKORG", "MAKTX", "MATKL", "BRAND_NM", "MEINS", "STATUS"], rows)
    write_csv(C.RAW_DIR / "erp" / "merch_hierarchy.csv", ["MATKL", "DEPT_NM", "CAT_NM", "SUBCAT_NM"],
              sorted({(c[0], c[1], c[2], c[3]) for c in CATALOG}))
    cost_rows = []
    for it, p in PRODUCTS.items():
        cost_rows.append([it, "20240101", f"{p['cost']:.2f}", "USD"])
        if rng.random() < 0.4:
            eff = C.SALES_START + timedelta(days=rng.randint(30, 560))
            cost_rows.append([it, eff.strftime("%Y%m%d"), f"{p['cost'] * rng.uniform(1.03, 1.08):.2f}", "USD"])
    write_csv(C.RAW_DIR / "erp" / "cost_history.csv", ["MATNR", "COST_EFF_DT", "STD_COST", "CRNCY_CD"], cost_rows)


def generate_stores():
    snaps = [d("2025-02-01"), d("2025-06-01"), d("2026-02-01"), d("2026-06-01")]
    for snap in snaps:
        rows = []
        for s in STORES:
            nbr, name, city, st, fmt, rgn, opened, closed, sqft = s[:9]
            if nbr == "00322" and snap < d("2025-06-01"):
                continue                                                        # not yet in the master
            if nbr == "01042" and snap >= BOSTON_REMODEL:
                fmt, sqft = "SC", 52000
            close = closed if (closed and snap >= d(closed)) else ""
            to_us = lambda x: datetime.strptime(x, "%Y-%m-%d").strftime("%m/%d/%Y") if x else ""
            rows.append([nbr, name, city, st, "US" if st != "ON" else "CA", fmt, rgn, to_us(opened), to_us(close), sqft])
        write_csv(C.RAW_DIR / "stores" / f"store_master_{snap.strftime('%Y%m%d')}.csv",
                  ["STR_NBR", "STR_NM", "CITY", "ST_CD", "CNTRY", "FMT_CD", "RGN_CD", "OPEN_DT", "CLOSE_DT", "SQFT"], rows)
    write_csv(C.RAW_DIR / "stores" / "ref_region.csv", ["RGN_CD", "RGN_NM"], sorted(REGIONS.items()))
    write_csv(C.RAW_DIR / "stores" / "ref_store_format.csv", ["FMT_CD", "FMT_NM"], sorted(FORMATS.items()))
    write_csv(C.RAW_DIR / "stores" / "ref_state_province.csv", ["ST_CD", "ST_NM", "CNTRY"],
              [(k, v[0], v[1]) for k, v in sorted(STATES.items())])


def generate_crm():
    rows = [[m["card"], m["email"], m["dob"].isoformat(), m["tier"], m["enroll"].isoformat(), m["home"]]
            for m in MEMBERS if m["enroll"] <= CRM_EXTRACT_DATE]
    write_csv(C.RAW_DIR / "crm" / f"crm_members_{CRM_EXTRACT_DATE.strftime('%Y%m%d')}.csv",
              ["CARD_NBR", "EMAIL", "DOB", "TIER", "ENROLL_DT", "HOME_STR"], rows)


def generate_promotions():
    write_csv(C.RAW_DIR / "promotions" / "promo_calendar.csv",
              ["PROMO_CD", "PROMO_DESC", "MECH_CD", "START_DT", "END_DT", "DISC_PCT"],
              [[p["code"], p["desc"], p["mech"], p["start"].isoformat(), p["end"].isoformat(), p["pct"] or ""] for p in PROMOS])
    write_csv(C.RAW_DIR / "promotions" / "promo_items.csv", ["PROMO_CD", "MATNR"], PROMO_ITEMS)


def generate_finance():
    rows = [[r["date"].strftime("%d/%m/%Y"), f"FY{r['fy']}", f"Q{r['quarter']}", f"P{r['period']:02d}",
             PERIOD_NAMES[r["period"] - 1], f"W{r['week']:02d}", r["week_start"].strftime("%d/%m/%Y")] for r in FISCAL]
    write_csv(C.RAW_DIR / "finance" / "fiscal_calendar.csv",
              ["CAL_DT", "FISC_YR", "FISC_QTR", "FISC_PRD", "FISC_PRD_NM", "FISC_WK", "FISC_WK_START"], rows)
    holidays = {d("2025-04-18"), d("2025-07-01"), d("2025-12-25"), d("2026-01-01"), d("2026-04-03"), d("2026-07-01")}
    rate, fx = 0.7300, []
    for day in daterange(d("2025-01-02"), C.SALES_END - timedelta(days=1)):
        rate = min(0.765, max(0.695, rate + rng.gauss(0, 0.0025)))
        if day.weekday() < 5 and day not in holidays:
            fx.append([day.isoformat(), "CAD", "USD", f"{rate:.4f}"])
    write_csv(C.RAW_DIR / "finance" / "fx_rates.csv", ["RATE_DT", "FROM_CCY", "TO_CCY", "RATE"], fx)


def generate_inventory():
    by_month = defaultdict(list)
    for s in STORES:
        nbr, fmt = s[0], s[4]
        # Express stores range 70% of products; the rest range everything they can sell
        ranged = [i for i in PRODUCTS if i not in DISCONTINUED and (nbr in CANADA or not PRODUCTS[i]["canada_only"])]
        if fmt == "EXP":
            ranged = sorted(rng.sample(ranged, int(len(ranged) * 0.7)))
        for item in ranged:
            fresh = PRODUCTS[item]["dept"] in ("Produce", "Bakery", "Meat and Seafood", "Deli", "Dairy and Eggs")
            level = rng.uniform(8, 40)
            for day in daterange(C.INVENTORY_START, C.SALES_END):
                if not store_open_on(nbr, day):
                    continue
                demand = rng.uniform(0.5, 6.0) * (1.4 if fresh else 1.0)
                level -= demand
                on_order = 0
                if level < 6:
                    on_order = rng.choice([12, 24, 36])
                    if rng.random() < (0.55 if fresh else 0.8):                 # delivery arrives on time
                        level += on_order
                oh = max(0.0, level)
                if rng.random() < 0.002:
                    oh = -rng.randint(1, 3)                                    # phantom negative stock
                qty = f"{oh:.3f}" if PRODUCTS[item]["uom"] == "KG" else str(int(round(oh)))
                by_month[day.strftime("%Y%m")].append([nbr, item.zfill(12), day.strftime("%Y%m%d"), qty, str(on_order)])
                if level < 0:
                    level = 0
    for m, rows in sorted(by_month.items()):
        write_csv(C.RAW_DIR / "inventory" / f"inv_snapshot_{m}.csv",
                  ["STR_NBR", "ITM_ID", "SNAP_DT", "OH_QTY", "ON_ORD_QTY"], rows)
    return sum(len(v) for v in by_month.values())


def main():
    for sub in ["pos", "ecommerce", "erp", "stores", "crm", "promotions", "finance", "inventory"]:
        if (C.RAW_DIR / sub).exists():                  # keep data/raw/README.md
            shutil.rmtree(C.RAW_DIR / sub)
    generate_finance()
    generate_stores()
    generate_products()
    generate_crm()
    generate_promotions()
    n_pos = generate_pos()
    n_ecom = generate_ecommerce()
    n_inv = generate_inventory()
    print(f"POS lines: {n_pos:,}   online orders: {n_ecom:,}   inventory rows: {n_inv:,}")
    print(f"Raw files written to {C.RAW_DIR}")


if __name__ == "__main__":
    main()
