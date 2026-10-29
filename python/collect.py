# -*- coding: utf-8 -*-
"""
Събирач на търговски обекти. Без изкуствен интелект: само детерминирани правила.

Източници:
  1. OpenStreetMap (Overpass API)  – магазини, студиа, сгради, хотели, молове
  2. Сайтовете на самите обекти    – адрес, телефон, работно време (schema.org JSON-LD)
  3. CSV файлове в папка imports/  – всякакви други списъци (вериги, справочници, теренни обходи)

Примери:
  python collect.py --area slanchev-bryag
  python collect.py --place "Варна" --id varna
  python collect.py --area slanchev-bryag --enrich      (+ проверка на сайтовете)
  python collect.py --list                              (показва зоните)

Нужен е само Python 3.8+ (без допълнителни пакети).
"""
import argparse
import csv
import glob
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import config

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
IMPORTS_DIR = os.path.join(HERE, "imports")
TOURISM_CSV = os.path.join(HERE, "tourism.csv")


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def http_get(url, data=None, timeout=60, max_bytes=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(max_bytes) if max_bytes else r.read()
        charset = r.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace"), r.geturl()


def geocode(place):
    q = urllib.parse.urlencode({"q": place, "format": "json", "limit": 1, "countrycodes": "bg"})
    text, _ = http_get("https://nominatim.openstreetmap.org/search?" + q, timeout=30)
    res = json.loads(text)
    if not res:
        sys.exit(f"Не намирам „{place}“ в OpenStreetMap.")
    s, n, w, e = map(float, res[0]["boundingbox"])
    log(f"Намерено: {res[0].get('display_name')}")
    return (s, w, n, e)


# ---------------------------------------------------------------------------
# OVERPASS
# ---------------------------------------------------------------------------
STAY_RE = "|".join(config.STAY_TYPES)


def overpass_query(bbox):
    b = "%f,%f,%f,%f" % bbox
    return f"""
[out:json][timeout:180];
nwr["shop"]({b})->.shops;
nwr["tourism"~"^({STAY_RE})$"]({b})->.stay;
.shops out geom;
.stay out geom;
(way["building"](around.shops:15); way["building"](around.stay:15);)->.blds;
.blds out geom;
nwr["shop"="mall"]({b});
out geom;
"""


def fetch_overpass(bbox):
    q = overpass_query(bbox).encode()
    body = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for attempt in range(2):
        for url in config.OVERPASS_MIRRORS:
            try:
                log(f"Overpass: {url}")
                text, _ = http_get(url, data=body, timeout=200)
                return json.loads(text)
            except Exception as ex:  # 429, 504, мрежа
                last = ex
                log(f"  неуспех ({ex}); опитвам следващ сървър")
                time.sleep(3)
        time.sleep(20)
    sys.exit(f"Overpass не отговаря: {last}")


# ---------------------------------------------------------------------------
# ГЕОМЕТРИЯ (локална равнинна проекция в метри — точна за зони до ~50 км)
# ---------------------------------------------------------------------------
def to_xy(lat, lon, lat0):
    return (lon * 111320.0 * math.cos(math.radians(lat0)), lat * 110540.0)


def poly_area_m2(coords, lat0):
    pts = [to_xy(c[0], c[1], lat0) for c in coords]
    if len(pts) < 4:
        return 0.0
    s = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def point_in_poly(lat, lon, coords):
    inside = False
    j = len(coords) - 1
    for i in range(len(coords)):
        yi, xi = coords[i]
        yj, xj = coords[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def dist_m(a, b):
    lat0 = (a[0] + b[0]) / 2
    ax, ay = to_xy(a[0], a[1], lat0)
    bx, by = to_xy(b[0], b[1], lat0)
    return math.hypot(ax - bx, ay - by)


def element_shape(el):
    """Връща (lat, lon, polygon|None)."""
    if el["type"] == "node":
        return el["lat"], el["lon"], None
    if el["type"] == "way" and el.get("geometry"):
        g = [(p["lat"], p["lon"]) for p in el["geometry"] if p]
        closed = len(g) >= 4 and g[0] == g[-1]
        lat = sum(p[0] for p in g) / len(g)
        lon = sum(p[1] for p in g) / len(g)
        return lat, lon, (g if closed else None)
    if el.get("bounds"):
        bd = el["bounds"]
        outer = []
        for m in el.get("members", []):
            if m.get("role") == "outer" and m.get("geometry"):
                outer = [(p["lat"], p["lon"]) for p in m["geometry"] if p]
                break
        closed = len(outer) >= 4 and outer[0] == outer[-1]
        return (bd["minlat"] + bd["maxlat"]) / 2, (bd["minlon"] + bd["maxlon"]) / 2, (outer if closed else None)
    return None


class Polys:
    """Бързо търсене в кой полигон попада точка (решетка от клетки)."""

    def __init__(self, items, cell=0.002):
        self.cell, self.grid = cell, {}
        for it in items:
            lats = [p[0] for p in it["poly"]]
            lons = [p[1] for p in it["poly"]]
            it["bb"] = (min(lats), min(lons), max(lats), max(lons))
            for gx in range(int(min(lats) / cell), int(max(lats) / cell) + 1):
                for gy in range(int(min(lons) / cell), int(max(lons) / cell) + 1):
                    self.grid.setdefault((gx, gy), []).append(it)

    def find(self, lat, lon):
        out = []
        for it in self.grid.get((int(lat / self.cell), int(lon / self.cell)), []):
            s, w, n, e = it["bb"]
            if s <= lat <= n and w <= lon <= e and point_in_poly(lat, lon, it["poly"]):
                out.append(it)
        return out


# ---------------------------------------------------------------------------
# КЛАСИФИКАЦИЯ
# ---------------------------------------------------------------------------
def category_of(tags):
    for cid, (_, _, rules, _) in config.CATEGORIES.items():
        for key, values in rules:
            if tags.get(key) in values:
                return cid
    return None


def address_of(tags):
    street = tags.get("addr:street", "")
    num = tags.get("addr:housenumber", "")
    city = tags.get("addr:city") or tags.get("addr:place", "")
    parts = [" ".join(x for x in (street, num) if x), city]
    return ", ".join(p for p in parts if p)


def levels(tags, default):
    try:
        return max(1, int(float(str(tags.get("building:levels", "")).split(";")[0])))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# ОБРАБОТКА НА OSM
# ---------------------------------------------------------------------------
def process_osm(osm, lat0):
    shops, stays, buildings, malls, seen = [], [], [], [], set()
    for el in osm.get("elements", []):
        key = (el["type"], el["id"])
        tags = el.get("tags", {})
        shape = element_shape(el)
        if not shape:
            continue
        lat, lon, poly = shape
        if tags.get("shop") == "mall":
            if key not in seen:
                malls.append({"name": tags.get("name", "Търговски център"), "lat": lat, "lon": lon, "poly": poly,
                              "area": round(poly_area_m2(poly, lat0)) if poly else None})
            seen.add(key)
            continue
        if "shop" in tags:
            if key in seen:
                continue
            seen.add(key)
            shops.append({"el": el, "tags": tags, "lat": lat, "lon": lon, "poly": poly, "cat": category_of(tags)})
        elif tags.get("tourism") in config.STAY_TYPES:
            if key in seen:
                continue
            seen.add(key)
            stays.append({"el": el, "tags": tags, "lat": lat, "lon": lon, "poly": poly})
        if "building" in tags and poly and ("b" + str(el["id"])) not in seen:
            seen.add("b" + str(el["id"]))
            buildings.append({"tags": tags, "poly": poly, "area": poly_area_m2(poly, lat0), "shops": 0})
    return shops, stays, buildings, malls


RETAIL_BUILDINGS = {"retail", "commercial", "kiosk", "supermarket", "shop", "mall"}


def estimate_shop_area(s, bld_index, mall_index, lat0):
    lo, typ, hi = config.CATEGORIES[s["cat"]][3]
    if s["poly"]:
        a = poly_area_m2(s["poly"], lat0)
        if 3 < a < 30000:
            return round(a), "измерена", 0.10, "контурът на обекта в OSM"
    hits = bld_index.find(s["lat"], s["lon"])
    if hits:
        b = min(hits, key=lambda x: x["area"])
        n = max(1, b["shops"])
        btype = b["tags"].get("building", "yes")
        share = b["area"] / n
        if btype in RETAIL_BUILDINGS or mall_index.find(s["lat"], s["lon"]):
            share = b["area"] * levels(b["tags"], 1) / n
            note = f"търговска сграда {round(b['area'])} м², {n} обекта в нея"
        elif b["area"] <= 250 and n == 1:
            note = f"самостоятелна малка сграда {round(b['area'])} м²"
        elif share < typ:
            note = f"сграда {round(b['area'])} м², поделена на {n} обекта"
        else:
            return typ, "типична", 0.5, f"партер на по-голяма сграда ({btype}); типична площ за бранша"
        est = min(max(share, lo), hi)
        return round(est), "по сграда", 0.35, note
    return typ, "типична", 0.5, "няма данни за сградата; типична площ за бранша"


def estimate_beds(st, bld_index, lat0):
    t = st["tags"]
    typ = t.get("tourism")
    default_beds, default_lv = config.STAY_TYPES[typ]
    for k in ("beds", "capacity"):
        try:
            v = int(re.sub(r"\D", "", str(t.get(k, ""))) or 0)
            if v > 0:
                return v, "от OSM"
        except ValueError:
            pass
    try:
        rooms = int(re.sub(r"\D", "", str(t.get("rooms", ""))) or 0)
        if rooms > 0:
            return rooms * 2, "стаи × 2"
    except ValueError:
        pass
    area, lv = None, default_lv
    if st["poly"]:
        area, lv = poly_area_m2(st["poly"], lat0), levels(t, default_lv)
    else:
        hits = bld_index.find(st["lat"], st["lon"])
        if hits:
            b = min(hits, key=lambda x: x["area"])
            area, lv = b["area"], levels(b["tags"], default_lv)
    if area and typ in ("hotel", "hostel", "motel", "guest_house"):
        beds = round(area * lv * 0.65 / 28 * 2)  # 65% жилищна площ, 28 м² на стая, 2 легла
        return max(4, min(beds, 2500)), "по сграда"
    return default_beds, "типично"


def build_records(shops, stays, buildings, malls, lat0):
    bld_index = Polys([b for b in buildings])
    mall_index = Polys([m for m in malls if m["poly"]])
    for s in shops:  # брой обекти (от всякакъв вид) във всяка сграда
        for b in bld_index.find(s["lat"], s["lon"]):
            b["shops"] += 1

    records = []
    for s in shops:
        if not s["cat"]:
            continue
        t = s["tags"]
        area, conf, err, note = estimate_shop_area(s, bld_index, mall_index, lat0)
        mall = mall_index.find(s["lat"], s["lon"])
        records.append({
            "id": f"osm:{s['el']['type'][0]}{s['el']['id']}",
            "name": t.get("name") or t.get("brand") or "(без име)",
            "brand": t.get("brand") or "",
            "cat": s["cat"],
            "osm_type": t.get("shop"),
            "lat": round(s["lat"], 6), "lon": round(s["lon"], 6),
            "address": address_of(t),
            "phone": t.get("phone") or t.get("contact:phone") or "",
            "website": t.get("website") or t.get("contact:website") or t.get("brand:website") or "",
            "hours": t.get("opening_hours", ""),
            "mall": mall[0]["name"] if mall else "",
            "area": area, "area_conf": conf, "area_err": err, "area_note": note,
            "sources": ["OpenStreetMap"],
            "osm_url": f"https://www.openstreetmap.org/{s['el']['type']}/{s['el']['id']}",
        })
    other = sum(1 for s in shops if not s["cat"])

    stay_out = []
    for st in stays:
        beds, how = estimate_beds(st, bld_index, lat0)
        t = st["tags"]
        stay_out.append({"name": t.get("name", ""), "type": t.get("tourism"), "stars": t.get("stars", ""),
                         "lat": round(st["lat"], 6), "lon": round(st["lon"], 6), "beds": beds, "beds_how": how})
    mall_out = [{"name": m["name"], "lat": round(m["lat"], 6), "lon": round(m["lon"], 6), "area": m["area"],
                 "poly": [[round(a, 6), round(b, 6)] for a, b in (m["poly"] or [])]} for m in malls]
    return records, stay_out, mall_out, other


# ---------------------------------------------------------------------------
# САЙТОВЕ НА ОБЕКТИТЕ (schema.org JSON-LD + meta)
# ---------------------------------------------------------------------------
LD_RE = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_robots = {}


def allowed(url):
    p = urllib.parse.urlparse(url)
    root = f"{p.scheme}://{p.netloc}"
    if root not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            text, _ = http_get(root + "/robots.txt", timeout=config.WEB_TIMEOUT, max_bytes=200_000)
            rp.parse(text.splitlines())
        except Exception:
            rp.parse([])
        _robots[root] = rp
    return _robots[root].can_fetch(config.USER_AGENT, url)


def walk_ld(obj, found):
    if isinstance(obj, list):
        for o in obj:
            walk_ld(o, found)
    elif isinstance(obj, dict):
        t = obj.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(isinstance(x, str) and (x.endswith("Store") or x in ("LocalBusiness", "BeautySalon", "HairSalon",
               "DaySpa", "NailSalon", "Organization", "ShoppingCenter")) for x in types):
            found.append(obj)
        for v in obj.values():
            if isinstance(v, (list, dict)):
                walk_ld(v, found)


def fmt_hours(ld):
    oh = ld.get("openingHours")
    if isinstance(oh, list):
        return "; ".join(map(str, oh))
    if isinstance(oh, str):
        return oh
    spec = ld.get("openingHoursSpecification")
    if isinstance(spec, list):
        parts = []
        for s in spec:
            d = s.get("dayOfWeek", "")
            d = ",".join(x.split("/")[-1][:2] for x in d) if isinstance(d, list) else str(d).split("/")[-1][:2]
            parts.append(f"{d} {s.get('opens', '')}-{s.get('closes', '')}")
        return "; ".join(parts)
    return ""


def scan_site(url):
    if not url.startswith("http"):
        url = "http://" + url
    try:
        if not allowed(url):
            return {"status": "забранено от robots.txt"}
        html, final = http_get(url, timeout=config.WEB_TIMEOUT, max_bytes=config.WEB_MAX_BYTES)
    except Exception as ex:
        return {"status": f"недостъпен ({type(ex).__name__})"}
    out = {"status": "ok", "final_url": final}
    m = TITLE_RE.search(html)
    if m:
        out["title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:150]
    found = []
    for block in LD_RE.findall(html):
        try:
            walk_ld(json.loads(block.strip()), found)
        except ValueError:
            continue
    if found:
        ld = found[0]
        addr = ld.get("address")
        if isinstance(addr, dict):
            out["address"] = ", ".join(str(addr.get(k, "")) for k in ("streetAddress", "addressLocality") if addr.get(k))
        if ld.get("telephone"):
            out["phone"] = str(ld["telephone"])
        h = fmt_hours(ld)
        if h:
            out["hours"] = h
        fs = ld.get("floorSize")
        if isinstance(fs, dict) and fs.get("value"):
            out["floor_size"] = fs.get("value")
    return out


def enrich(records):
    cache_path = os.path.join(DATA_DIR, "web_cache.json")
    cache = json.load(open(cache_path, encoding="utf-8")) if os.path.exists(cache_path) else {}
    urls = sorted({r["website"] for r in records if r["website"]} - set(cache))
    log(f"Сайтове за проверка: {len(urls)} нови (+{len(cache)} от кеша)")
    with ThreadPoolExecutor(config.WEB_WORKERS) as ex:
        for url, res in zip(urls, ex.map(scan_site, urls)):
            cache[url] = res
    json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    n = 0
    for r in records:
        info = cache.get(r["website"])
        if not info or info.get("status") != "ok":
            continue
        n += 1
        if "Сайт" not in r["sources"]:
            r["sources"].append("Сайт")
        for k in ("address", "phone", "hours"):
            if info.get(k) and not r[k]:
                r[k] = info[k]
        if info.get("floor_size"):
            try:
                r["area"], r["area_conf"], r["area_err"], r["area_note"] = round(float(info["floor_size"])), "от сайта", 0.1, "обявена площ в сайта"
            except ValueError:
                pass
        r["site_title"] = info.get("title", "")
    log(f"Допълнени от сайт: {n}")


# ---------------------------------------------------------------------------
# CSV ИМПОРТ + ОБЕДИНЯВАНЕ
# ---------------------------------------------------------------------------
TRANSLIT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
            "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
            "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sht", "ъ": "a", "ь": "y", "ю": "yu", "я": "ya"}
LABEL_TO_CAT = {v[0].lower(): k for k, v in config.CATEGORIES.items()}


def norm(name):
    s = "".join(TRANSLIT.get(c, c) for c in (name or "").lower())
    return re.sub(r"[^a-z0-9]", "", s)


def same(a, b):
    if a["cat"] != b["cat"] or dist_m((a["lat"], a["lon"]), (b["lat"], b["lon"])) > config.DEDUP_METERS:
        return False
    na, nb = norm(a["name"]), norm(b["name"])
    return bool(na and nb and (na in nb or nb in na))


def load_imports(bbox):
    s, w, n, e = bbox
    out = []
    for path in sorted(glob.glob(os.path.join(IMPORTS_DIR, "*.csv"))):
        src = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8-sig") as f:
            for i, row in enumerate(csv.DictReader(f)):
                try:
                    lat, lon = float(row["lat"]), float(row["lon"])
                except (KeyError, ValueError):
                    continue
                if not (s <= lat <= n and w <= lon <= e):
                    continue
                cat = (row.get("category") or "").strip()
                cat = cat if cat in config.CATEGORIES else LABEL_TO_CAT.get(cat.lower())
                if not cat:
                    continue
                lo, typ, hi = config.CATEGORIES[cat][3]
                area = row.get("area_m2", "").strip()
                out.append({
                    "id": f"csv:{src}:{i}", "name": row.get("name", "").strip() or "(без име)",
                    "brand": row.get("brand", "").strip(), "cat": cat, "osm_type": "",
                    "lat": lat, "lon": lon, "address": row.get("address", "").strip(),
                    "phone": row.get("phone", "").strip(), "website": row.get("website", "").strip(),
                    "hours": row.get("hours", "").strip(), "mall": row.get("mall", "").strip(),
                    "area": round(float(area)) if area else typ,
                    "area_conf": "от източника" if area else "типична",
                    "area_err": 0.1 if area else 0.5,
                    "area_note": f"площ от {src}" if area else "типична площ за бранша",
                    "sources": [row.get("source", "").strip() or src], "osm_url": "",
                })
    return out


def merge(records, extra):
    added = 0
    for x in extra:
        hit = next((r for r in records if same(r, x)), None)
        if hit:
            for src in x["sources"]:
                if src not in hit["sources"]:
                    hit["sources"].append(src)
            for k in ("brand", "address", "phone", "website", "hours", "mall"):
                if x[k] and not hit[k]:
                    hit[k] = x[k]
            if x["area_conf"] == "от източника":
                hit.update({k: x[k] for k in ("area", "area_conf", "area_err", "area_note")})
        else:
            records.append(x)
            added += 1
    log(f"От CSV: {len(extra)} реда, {added} нови обекта, {len(extra) - added} съвпадения")


def load_tourism():
    rows = []
    if os.path.exists(TOURISM_CSV):
        with open(TOURISM_CSV, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("zona") and row.get("godina") and (row.get("noshtuvki") or row.get("pristignali")):
                    rows.append({k: row.get(k, "") for k in ("zona", "godina", "mesec", "noshtuvki", "pristignali", "iztochnik")})
    return rows


# ---------------------------------------------------------------------------
# ЗАПИС
# ---------------------------------------------------------------------------
def excel_cell(v):
    """Текст, който започва с = + - @, Excel изпълнява като формула; апострофът отпред го прави обикновен текст."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def save(area_id, area_name, bbox, records, stays, malls, other):
    os.makedirs(DATA_DIR, exist_ok=True)
    store_path = os.path.join(DATA_DIR, "areas.json")
    store = json.load(open(store_path, encoding="utf-8")) if os.path.exists(store_path) else {"areas": {}}
    store["areas"][area_id] = {
        "name": area_name, "bbox": bbox, "collected": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "shops": records, "stays": stays, "malls": malls, "other_shops": other,
    }
    store["categories"] = {k: {"label": v[0], "color": v[1]} for k, v in config.CATEGORIES.items()}
    store["tourism"] = load_tourism()
    json.dump(store, open(store_path, "w", encoding="utf-8"), ensure_ascii=False)
    with open(os.path.join(DATA_DIR, "data.js"), "w", encoding="utf-8") as f:
        f.write("window.SHOP_DATA = ")
        json.dump(store, f, ensure_ascii=False)
        f.write(";\n")
    with open(os.path.join(DATA_DIR, f"{area_id}.csv"), "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["име", "марка", "бранш", "lat", "lon", "адрес", "телефон", "сайт", "работно време", "мол",
                     "площ м²", "точност на площта", "източници"])
        for r in records:
            wr.writerow([excel_cell(v) for v in (
                r["name"], r["brand"], config.CATEGORIES[r["cat"]][0], r["lat"], r["lon"], r["address"],
                r["phone"], r["website"], r["hours"], r["mall"], r["area"], r["area_conf"], " + ".join(r["sources"]))])


def main():
    ap = argparse.ArgumentParser(description="Събирач на търговски обекти (без AI)")
    ap.add_argument("--area", help="готова зона от config.py, напр. slanchev-bryag")
    ap.add_argument("--place", help="произволно място в България, напр. \"Варна\"")
    ap.add_argument("--bbox", help="юг,запад,север,изток")
    ap.add_argument("--id", help="идентификатор на зоната при --place/--bbox")
    ap.add_argument("--enrich", action="store_true", help="проверява и сайтовете на обектите")
    ap.add_argument("--overpass-file", help="готов JSON от overpass-turbo вместо заявка")
    ap.add_argument("--list", action="store_true", help="показва готовите зони")
    a = ap.parse_args()

    if a.list:
        for k, (name, bb) in config.AREAS.items():
            log(f"{k:18} {name}  {bb}")
        return
    if a.area:
        if a.area not in config.AREAS:
            sys.exit(f"Няма зона „{a.area}“. Виж: python collect.py --list")
        area_id, (area_name, bbox) = a.area, config.AREAS[a.area]
    elif a.bbox:
        bbox = tuple(float(x) for x in a.bbox.split(","))
        area_id, area_name = a.id or "zona", a.id or "Зона"
    elif a.place:
        bbox = geocode(a.place)
        area_id, area_name = a.id or norm(a.place) or "zona", a.place
    else:
        ap.print_help()
        return

    lat0 = (bbox[0] + bbox[2]) / 2
    log(f"Зона: {area_name} {bbox}")
    osm = json.load(open(a.overpass_file, encoding="utf-8")) if a.overpass_file else fetch_overpass(bbox)
    shops, stays, buildings, malls = process_osm(osm, lat0)
    log(f"OSM: {len(shops)} магазина/студиа общо, {len(buildings)} сгради, {len(stays)} места за настаняване, {len(malls)} мола")
    records, stay_out, mall_out, other = build_records(shops, stays, buildings, malls, lat0)
    log(f"В избраните браншове: {len(records)}")
    merge(records, load_imports(bbox))
    if a.enrich:
        enrich(records)
    save(area_id, area_name, bbox, records, stay_out, mall_out, other)
    log(f"Готово. Отвори viewer.html (данните са в data/). Таблица: data/{area_id}.csv")


if __name__ == "__main__":
    main()
