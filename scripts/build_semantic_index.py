#!/usr/bin/env python3
"""Precompute a CLOSP Ask index for the four Trinetra demo queries.

Downloads real Sentinel-2 L2A and Sentinel-1 GRD chips for 7 AOIs x 8 locations,
embeds them with DarthReca/CLOSP-VL (CPU unless CUDA is present), and writes:

  public/semantic/index.json
  public/semantic/chips/<tile-id>.jpg
  public/semantic/gallery/{s2,s1,all}.jpg and index.html
  FINAL_NOTES.md

Optical input is 13 bands in SSL4EO order (B1..B9, B10, B11, B12). L2A has no
cirrus band, so B10 is zeros. Values are reflectance digital numbers; the
encoder divides by 10000, matching CLOSP's validation normalize (mean 0, std
10000), then resizes to 224.

SAR input is Planetary Computer Sentinel-1 GRD RTC gamma0 (VV, VH), converted
to decibels. CLOSP's SAR validation path only resizes; it does not rescale.
Raw GRD digital numbers are not what that encoder was trained on.

The browser never runs this script. Ask.jsx only reads index.json and the JPEGs.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont

os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
os.environ.setdefault("GDAL_HTTP_MULTIPLEX", "YES")
os.environ.setdefault("GDAL_HTTP_TIMEOUT", "90")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "3")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.tiff,.TIF,.TIFF")
os.environ.setdefault("VSI_CACHE", "TRUE")

import rasterio
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds
from scipy.ndimage import distance_transform_edt

ROOT = Path(__file__).resolve().parents[1]
DEM_DIR = ROOT / "public" / "dem"
CHIP_DIR = ROOT / "public" / "semantic" / "chips"
GALLERY_DIR = ROOT / "public" / "semantic" / "gallery"
INDEX_PATH = ROOT / "public" / "semantic" / "index.json"
NOTES_PATH = ROOT / "FINAL_NOTES.md"
CACHE_DIR = ROOT / "scripts" / ".cache" / "semantic"

EARTH_SEARCH = "https://earth-search.aws.element84.com/v1/search"
PC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
PC_SIGN = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

CHECKPOINT = os.environ.get("CLOSP_CHECKPOINT", "DarthReca/CLOSP-VL")
CHIP_M = 500.0
CHIP_PX = 50  # ~10 m, about 500 m on a side
CLOUD_LT = 20.0

# SSL4EO / CLOSP ViT-L B13 order. None is B10 cirrus, absent from L2A.
S2_ASSETS = [
    "coastal",
    "blue",
    "green",
    "red",
    "rededge1",
    "rededge2",
    "rededge3",
    "nir",
    "nir08",
    "nir09",
    None,
    "swir16",
    "swir22",
]

# Eight date slots per AOI. Two are before 2023, three prefer Oct–Dec, three
# sit in the newest season so the 60-day ridge query still has a corpus.
SLOTS = [
    ("pre", "2019-06-01T00:00:00Z", "2022-12-15T23:59:59Z", None),
    ("pre", "2021-01-01T00:00:00Z", "2022-11-30T23:59:59Z", None),
    ("post", "2023-10-01T00:00:00Z", "2023-12-28T23:59:59Z", (10, 11, 12)),
    ("post", "2024-10-01T00:00:00Z", "2024-12-28T23:59:59Z", (10, 11, 12)),
    ("post", "2025-10-01T00:00:00Z", "2025-12-28T23:59:59Z", (10, 11, 12)),
    ("recent", "2026-07-15T00:00:00Z", "2026-09-28T23:59:59Z", None),
    ("recent", "2026-08-01T00:00:00Z", "2026-09-28T23:59:59Z", None),
    ("recent", "2026-08-20T00:00:00Z", "2026-09-28T23:59:59Z", None),
]

TIME_RE = re.compile(
    r"\b(since\s+\d{4}|since\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*(?:\s+\d{4})?"
    r"|in\s+the\s+last\s+\d+\s+(?:days|weeks|months)|last\s+\d+\s+(?:days|weeks|months)"
    r"|after\s+(?:the\s+)?monsoon|before\s+(?:the\s+)?monsoon|this\s+(?:week|month|year)|in\s+\d{4})\b",
    re.I,
)
SPATIAL_RE = re.compile(
    r"\b((?:near|along|beside|within\s+\d+(?:\.\d+)?\s*(?:km|m)\s+of|close\s+to|adjacent\s+to|"
    r"north\s+of|south\s+of|east\s+of|west\s+of|upstream\s+of|downstream\s+of)\s+"
    r"(?:the\s+|a\s+|an\s+)?[a-z]+(?:\s+(?:crossing|bank|track|road|line|ridge|river|channel|slope|shelf))?)\b",
    re.I,
)
STOP_RE = re.compile(r"\b(show|find|me|all|any|the|of|with|and|where|are|is)\b", re.I)

QUERIES = [
    {
        "q": "new structures near river since 2023",
        "object": "new structures",
        "drop_before": "2023-01-01",
        "within_newest_days": None,
        "boost": ("water",),
    },
    {
        "q": "cleared ground along the creek after monsoon",
        "object": "cleared ground",
        "drop_before": None,
        "within_newest_days": None,
        "boost": ("water", "monsoon"),
    },
    {
        "q": "tracks within 2 km of ridge in the last 60 days",
        "object": "tracks",
        "drop_before": None,
        "within_newest_days": 60,
        "boost": ("elev",),
    },
    {
        "q": "vehicles near the river crossing",
        "object": "vehicles",
        "drop_before": None,
        "within_newest_days": None,
        "boost": ("water",),
    },
]

WATER_SCALE = 0.08
ELEV_SCALE = 0.06
MONSOON_SCALE = 0.05
READ_SEM = threading.Semaphore(8)
HTTP_LOCK = threading.Lock()
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "trinetra-closp-index/1.0"
PREP_VERSION = "s2-dn-div-10000-s1-gamma0-db-224-v1"


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_object(q: str) -> str:
    rest = f" {q} "
    t = TIME_RE.search(rest)
    if t:
        rest = rest.replace(t.group(0), " ", 1)
    s = SPATIAL_RE.search(rest)
    if s:
        rest = rest.replace(s.group(0), " ", 1)
    obj = STOP_RE.sub(" ", rest)
    return re.sub(r"\s+", " ", obj).strip()


def load_aois() -> list[dict]:
    aois = []
    for path in sorted(DEM_DIR.glob("AOI-*.json")):
        meta = json.loads(path.read_text())
        w, h = meta["width"], meta["height"]
        dem = np.fromfile(DEM_DIR / f"{meta['id']}.dem.bin", dtype="<i2").reshape(h, w)
        wbm = np.fromfile(DEM_DIR / f"{meta['id']}.wbm.bin", dtype=np.uint8).reshape(h, w)
        water = np.isin(wbm, (2, 3))
        dist = distance_transform_edt(~water)
        land = wbm == 0
        elev = dem.astype(np.float32)
        land_elev = elev[land]
        lo, hi = np.percentile(land_elev, [5, 95]) if land_elev.size else (0.0, 1.0)
        aois.append({
            "id": meta["id"],
            "meta": meta,
            "dem": dem,
            "wbm": wbm,
            "dist": dist,
            "land": land,
            "elev_lo": float(lo),
            "elev_hi": float(hi),
            "px_m": float(meta["widthM"]) / w,
            "py_m": float(meta["heightM"]) / h,
        })
    if len(aois) != 7:
        raise SystemExit(f"expected 7 AOIs, found {len(aois)}")
    return aois


def pix_to_ll(aoi: dict, x: int, y: int) -> tuple[float, float]:
    b = aoi["meta"]["bbox"]
    h, w = aoi["wbm"].shape
    lon = b["west"] + (x + 0.5) / w * (b["east"] - b["west"])
    lat = b["north"] - (y + 0.5) / h * (b["north"] - b["south"])
    return float(lat), float(lon)


def ll_to_pix(aoi: dict, lat: float, lon: float) -> tuple[int, int]:
    b = aoi["meta"]["bbox"]
    h, w = aoi["wbm"].shape
    x = int(round((lon - b["west"]) / (b["east"] - b["west"]) * w - 0.5))
    y = int(round((b["north"] - lat) / (b["north"] - b["south"]) * h - 0.5))
    return x, y


def in_bbox(aoi: dict, lat: float, lon: float) -> bool:
    b = aoi["meta"]["bbox"]
    return b["south"] < lat < b["north"] and b["west"] < lon < b["east"]


def describe_pixel(aoi: dict, x: int, y: int) -> dict:
    h, w = aoi["wbm"].shape
    x = int(np.clip(x, 0, w - 1))
    y = int(np.clip(y, 0, h - 1))
    lat, lon = pix_to_ll(aoi, x, y)
    dist_px = float(aoi["dist"][y, x])
    dist_m = dist_px * (aoi["px_m"] + aoi["py_m"]) / 2
    elev = float(aoi["dem"][y, x])
    span = max(aoi["elev_hi"] - aoi["elev_lo"], 1.0)
    elev_norm = float(np.clip((elev - aoi["elev_lo"]) / span, 0.0, 1.0))
    return {
        "x": x,
        "y": y,
        "lat": lat,
        "lon": lon,
        "dist_water_m": dist_m,
        "elev_m": elev,
        "elev_norm": elev_norm,
        "water_class": int(aoi["wbm"][y, x]),
        "near_water": bool(aoi["land"][y, x] and 1.0 <= dist_px <= 18.0),
    }


def snap_land(aoi: dict, x: int, y: int, near_water: bool) -> tuple[int, int] | None:
    h, w = aoi["wbm"].shape
    land = aoi["land"]
    dist = aoi["dist"]
    for r in range(0, 80, 2):
        for a in range(16):
            ang = (a / 16) * math.tau
            xx = int(round(x + math.cos(ang) * r))
            yy = int(round(y + math.sin(ang) * r))
            if not (0 <= xx < w and 0 <= yy < h) or not land[yy, xx]:
                continue
            d = dist[yy, xx]
            if near_water and not (1.0 <= d <= 18.0):
                continue
            if d < 1.0:
                continue
            return xx, yy
    return None


def pick_points(aoi: dict, n: int = 8, n_water: int = 3) -> list[dict]:
    """Eight land points spread over the bbox. At least three sit near a river or lake."""
    h, w = aoi["wbm"].shape
    land = aoi["land"]
    dist = aoi["dist"]
    cells = []
    gx_n, gy_n = 4, 2
    for gy in range(gy_n):
        for gx in range(gx_n):
            x0, x1 = int(gx * w / gx_n), int((gx + 1) * w / gx_n)
            y0, y1 = int(gy * h / gy_n), int((gy + 1) * h / gy_n)
            cells.append((x0, x1, y0, y1))

    def best_in_cell(cell, near: bool) -> tuple[int, int] | None:
        x0, x1, y0, y1 = cell
        sub_land = land[y0:y1, x0:x1]
        sub_dist = dist[y0:y1, x0:x1]
        if near:
            mask = sub_land & (sub_dist >= 1) & (sub_dist <= 18)
        else:
            mask = sub_land & (sub_dist >= 1)
        ys, xs = np.where(mask)
        if ys.size == 0:
            return None
        if near:
            order = np.argsort(sub_dist[ys, xs])
            sel = order[min(len(order) - 1, max(1, len(order) // 5))]
        else:
            cy = (y1 - y0) / 2
            cx = (x1 - x0) / 2
            sel = int(np.argmin((ys - cy) ** 2 + (xs - cx) ** 2))
        return int(xs[sel] + x0), int(ys[sel] + y0)

    near_counts = []
    for i, cell in enumerate(cells):
        x0, x1, y0, y1 = cell
        mask = land[y0:y1, x0:x1] & (dist[y0:y1, x0:x1] >= 1) & (dist[y0:y1, x0:x1] <= 18)
        near_counts.append((int(mask.sum()), i))
    near_counts.sort(reverse=True)
    water_ids = [i for _, i in near_counts[:n_water]]
    chosen = []
    used_cells = set()
    for i in water_ids:
        hit = best_in_cell(cells[i], True)
        if hit is None:
            continue
        chosen.append(hit)
        used_cells.add(i)
    for i, cell in enumerate(cells):
        if len(chosen) >= n:
            break
        if i in used_cells:
            continue
        hit = best_in_cell(cell, False)
        if hit is None:
            continue
        chosen.append(hit)
        used_cells.add(i)

    if sum(1 for x, y in chosen if 1 <= dist[y, x] <= 18) < n_water:
        ys, xs = np.where(land & (dist >= 1) & (dist <= 18))
        if ys.size:
            step = max(1, ys.size // 40)
            for y, x in zip(ys[::step], xs[::step]):
                if all(abs(int(x) - px) + abs(int(y) - py) > 40 for px, py in chosen):
                    chosen.append((int(x), int(y)))
                if sum(1 for xx, yy in chosen if 1 <= dist[yy, xx] <= 18) >= n_water and len(chosen) >= n:
                    break

    # Spread: if we still have extras, keep points far apart, preferring the water ones.
    if len(chosen) > n:
        water_pts = [(x, y) for x, y in chosen if 1 <= dist[y, x] <= 18]
        other = [(x, y) for x, y in chosen if (x, y) not in water_pts]
        kept = water_pts[:n_water]
        pool = water_pts[n_water:] + other
        while len(kept) < n and pool:
            def far(p):
                return min((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 for q in kept) if kept else 1e18
            pool.sort(key=far, reverse=True)
            kept.append(pool.pop(0))
        chosen = kept

    while len(chosen) < n:
        ys, xs = np.where(land & (dist >= 1))
        if ys.size == 0:
            break
        # farthest from existing
        step = max(1, ys.size // 80)
        best = None
        best_d = -1
        for y, x in zip(ys[::step], xs[::step]):
            d = min((int(x) - px) ** 2 + (int(y) - py) ** 2 for px, py in chosen) if chosen else 1e18
            if d > best_d:
                best_d = d
                best = (int(x), int(y))
        if best is None:
            break
        chosen.append(best)

    if len(chosen) != n:
        raise RuntimeError(f"{aoi['id']}: picked {len(chosen)} points, wanted {n}")
    pts = [describe_pixel(aoi, x, y) for x, y in chosen[:n]]
    near = sum(1 for p in pts if p["near_water"])
    if near < n_water:
        raise RuntimeError(f"{aoi['id']}: only {near} points near water")
    if any(p["water_class"] != 0 for p in pts):
        raise RuntimeError(f"{aoi['id']}: a sample point is not land")
    return pts


def stac_search(url: str, body: dict) -> list[dict]:
    last = None
    for _attempt in range(4):
        try:
            with HTTP_LOCK:
                r = SESSION.post(url, json=body, timeout=90)
            if r.status_code >= 500 or r.status_code == 429:
                last = RuntimeError(f"stac {r.status_code} {r.text[:180]}")
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"stac {r.status_code} {r.text[:240]}")
            return r.json().get("features") or []
        except requests.RequestException as exc:
            last = exc
    raise RuntimeError(f"STAC search failed: {last}")


def sign_href(href: str) -> str:
    with HTTP_LOCK:
        r = SESSION.get(PC_SIGN, params={"href": href}, timeout=60)
        r.raise_for_status()
        return r.json()["href"]


def window_bounds(lat: float, lon: float) -> tuple[float, float, float, float]:
    dlat = (CHIP_M / 2) / 111320.0
    dlon = (CHIP_M / 2) / (111320.0 * max(0.2, math.cos(math.radians(lat))))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def read_chip(href: str, lat: float, lon: float, fill: float) -> np.ndarray:
    west, south, east, north = window_bounds(lat, lon)
    with READ_SEM:
        with rasterio.open(href) as src:
            left, bottom, right, top = transform_bounds("EPSG:4326", src.crs, west, south, east, north, densify_pts=0)
            window = from_bounds(left, bottom, right, top, src.transform)
            if window.width < 2 or window.height < 2:
                raise RuntimeError("chip window missed the raster")
            arr = src.read(
                1,
                window=window,
                out_shape=(CHIP_PX, CHIP_PX),
                resampling=Resampling.bilinear,
                boundless=True,
                fill_value=fill,
            )
    return arr


def parse_when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def s2_candidates(lat: float, lon: float, start: str, end: str, prefer_months: tuple[int, ...] | None) -> list[dict]:
    features = stac_search(EARTH_SEARCH, {
        "collections": ["sentinel-2-l2a"],
        "intersects": {"type": "Point", "coordinates": [lon, lat]},
        "datetime": f"{start}/{end}",
        "query": {"eo:cloud_cover": {"lt": CLOUD_LT}},
        "limit": 40,
    })
    rows = []
    for feat in features:
        cloud = feat["properties"].get("eo:cloud_cover")
        if cloud is None or cloud >= CLOUD_LT:
            continue
        assets = feat.get("assets") or {}
        if any(name and name not in assets for name in S2_ASSETS):
            continue
        when = parse_when(feat["properties"]["datetime"])
        month_rank = 0 if prefer_months and when.month in prefer_months else 1
        rows.append((month_rank, float(cloud), when, feat))
    rows.sort(key=lambda row: (row[0], row[1], row[2]))
    return [row[3] for row in rows]


def s1_candidates(lat: float, lon: float, when: datetime) -> list[dict]:
    start = (when - timedelta(days=45)).strftime("%Y-%m-%dT00:00:00Z")
    end = (when + timedelta(days=45)).strftime("%Y-%m-%dT23:59:59Z")
    features = stac_search(PC_SEARCH, {
        "collections": ["sentinel-1-rtc"],
        "intersects": {"type": "Point", "coordinates": [lon, lat]},
        "datetime": f"{start}/{end}",
        "limit": 20,
    })
    rows = []
    for feat in features:
        assets = feat.get("assets") or {}
        if "vv" not in assets or "vh" not in assets:
            continue
        stamp = parse_when(feat["properties"]["datetime"])
        rows.append((abs((stamp - when).total_seconds()), stamp, feat))
    rows.sort(key=lambda row: row[0])
    return [row[2] for row in rows]


def stretch_u8(band: np.ndarray, lo_p: float = 2, hi_p: float = 98) -> np.ndarray:
    finite = band[np.isfinite(band)]
    if finite.size == 0:
        return np.zeros(band.shape, dtype=np.uint8)
    lo, hi = np.percentile(finite, [lo_p, hi_p])
    if hi <= lo:
        hi = lo + 1
    x = (band - lo) / (hi - lo)
    return (np.clip(x, 0, 1) * 255).astype(np.uint8)


def save_s2_jpeg(stack: np.ndarray, path: Path) -> None:
    # stack axis 0 is SSL4EO order; true color is B4, B3, B2 -> indices 3, 2, 1
    rgb = np.stack([stretch_u8(stack[3]), stretch_u8(stack[2]), stretch_u8(stack[1])], axis=-1)
    Image.fromarray(rgb, mode="RGB").resize((256, 256), Image.Resampling.BILINEAR).save(path, quality=86)


def save_s1_jpeg(vv: np.ndarray, vh: np.ndarray, path: Path) -> None:
    vv_db = 10.0 * np.log10(np.clip(vv, 1e-6, None))
    vh_db = 10.0 * np.log10(np.clip(vh, 1e-6, None))
    diff = vv_db - vh_db
    rgb = np.stack([
        stretch_u8(vv_db, 5, 95),
        stretch_u8(vh_db, 5, 95),
        stretch_u8(diff, 5, 95),
    ], axis=-1)
    Image.fromarray(rgb, mode="RGB").resize((256, 256), Image.Resampling.BILINEAR).save(path, quality=86)


def valid_s2(stack: np.ndarray) -> bool:
    red = stack[3]
    return float(np.mean(red > 0)) > 0.85 and float(np.mean(red)) > 50


def valid_s1(vv: np.ndarray, vh: np.ndarray) -> bool:
    good = np.isfinite(vv) & np.isfinite(vh) & (vv > 0) & (vh > 0) & (vv < 1e4) & (vh < 1e4)
    return float(np.mean(good)) > 0.85


def read_s2_stack(feat: dict, lat: float, lon: float) -> np.ndarray:
    bands = []
    hrefs = []
    for name in S2_ASSETS:
        if name is None:
            hrefs.append(None)
        else:
            hrefs.append(feat["assets"][name]["href"])

    def one(href):
        if href is None:
            return np.zeros((CHIP_PX, CHIP_PX), dtype=np.float32)
        return read_chip(href, lat, lon, fill=0).astype(np.float32)

    with ThreadPoolExecutor(max_workers=6) as pool:
        bands = list(pool.map(one, hrefs))
    return np.stack(bands, axis=0)


def read_s1_pair(feat: dict, lat: float, lon: float) -> tuple[np.ndarray, np.ndarray]:
    vv_href = sign_href(feat["assets"]["vv"]["href"])
    vh_href = sign_href(feat["assets"]["vh"]["href"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        vv_f = pool.submit(read_chip, vv_href, lat, lon, -32768.0)
        vh_f = pool.submit(read_chip, vh_href, lat, lon, -32768.0)
        vv = vv_f.result().astype(np.float32)
        vh = vh_f.result().astype(np.float32)
    vv = np.where(vv <= 0, np.nan, vv)
    vh = np.where(vh <= 0, np.nan, vh)
    return vv, vh


def point_shifts(aoi: dict, point: dict) -> list[dict]:
    """Original point, then land snaps a few hundred metres away if a scene is missing."""
    shifts = [(0.0, 0.0)]
    step = 0.012
    for lat_s, lon_s in (
        (step, step * 0.6),
        (-step, step * 0.4),
        (step * 0.4, -step),
        (-step * 0.8, -step * 0.5),
        (step * 1.4, -step * 0.2),
        (-step * 0.3, step * 1.2),
    ):
        shifts.append((lat_s, lon_s))
    out = []
    seen = set()
    for dlat, dlon in shifts:
        lat = point["lat"] + dlat
        lon = point["lon"] + dlon
        if not in_bbox(aoi, lat, lon):
            continue
        x, y = ll_to_pix(aoi, lat, lon)
        snapped = snap_land(aoi, x, y, near_water=point["near_water"])
        if snapped is None and point["near_water"]:
            snapped = snap_land(aoi, x, y, near_water=False)
        if snapped is None:
            continue
        desc = describe_pixel(aoi, *snapped)
        key = (desc["x"] // 8, desc["y"] // 8)
        if key in seen:
            continue
        seen.add(key)
        if point["near_water"] and not desc["near_water"]:
            continue
        out.append(desc)
    return out or [point]


def slot_windows(kind: str, start: str, end: str) -> list[tuple[str, str]]:
    windows = [(start, end)]
    if kind == "pre":
        windows.append(("2018-01-01T00:00:00Z", "2022-12-31T23:59:59Z"))
    elif kind == "post":
        year = start[:4]
        windows.append((f"{year}-01-15T00:00:00Z", f"{year}-12-20T23:59:59Z"))
    elif kind == "recent":
        windows.append(("2026-05-01T00:00:00Z", "2026-09-28T23:59:59Z"))
        windows.append(("2025-11-01T00:00:00Z", "2026-09-28T23:59:59Z"))
    return windows


def acquire_location(aoi: dict, slot_i: int, point: dict) -> dict:
    loc = f"{aoi['id']}-{slot_i:02d}"
    meta_path = CACHE_DIR / f"{loc}.json"
    npz_path = CACHE_DIR / f"{loc}.npz"
    s2_jpg = CHIP_DIR / f"{loc}-S2.jpg"
    s1_jpg = CHIP_DIR / f"{loc}-S1.jpg"
    if meta_path.exists() and npz_path.exists() and s2_jpg.exists() and s1_jpg.exists():
        meta = json.loads(meta_path.read_text())
        log(f"cache {loc}")
        return meta

    kind, start, end, prefer = SLOTS[slot_i]
    last = "no attempt"
    for shifted in point_shifts(aoi, point):
        for win_start, win_end in slot_windows(kind, start, end):
            try:
                s2_items = s2_candidates(shifted["lat"], shifted["lon"], win_start, win_end, prefer)
            except Exception as exc:
                last = f"s2 search {exc}"
                continue
            if not s2_items:
                last = f"no S2 {win_start[:10]}..{win_end[:10]} @ {shifted['lat']:.3f},{shifted['lon']:.3f}"
                continue
            for s2 in s2_items[:4]:
                when = parse_when(s2["properties"]["datetime"])
                try:
                    stack = read_s2_stack(s2, shifted["lat"], shifted["lon"])
                    if not valid_s2(stack):
                        last = f"thin S2 {s2['id']}"
                        continue
                    s1_items = s1_candidates(shifted["lat"], shifted["lon"], when)
                except Exception as exc:
                    last = f"read S2/search S1 {exc}"
                    continue
                if not s1_items:
                    last = f"no S1 near {s2['id']}"
                    continue
                for s1 in s1_items[:3]:
                    try:
                        vv, vh = read_s1_pair(s1, shifted["lat"], shifted["lon"])
                        if not valid_s1(vv, vh):
                            last = f"thin S1 {s1['id']}"
                            continue
                    except Exception as exc:
                        last = f"read S1 {exc}"
                        continue
                    s1_when = parse_when(s1["properties"]["datetime"])
                    cloud = float(s2["properties"]["eo:cloud_cover"])
                    meta = {
                        "loc": loc,
                        "aoi": aoi["id"],
                        "lat": round(shifted["lat"], 5),
                        "lon": round(shifted["lon"], 5),
                        "dist_water_m": round(shifted["dist_water_m"], 1),
                        "elev_m": round(shifted["elev_m"], 1),
                        "elev_norm": round(shifted["elev_norm"], 4),
                        "near_water": bool(shifted["near_water"]),
                        "s2": {
                            "id": f"{loc}-S2",
                            "date": when.date().isoformat(),
                            "sensor": "S2 MSI",
                            "cloud": int(cloud),
                            "scene": s2["id"],
                            "chip": f"semantic/chips/{loc}-S2.jpg",
                        },
                        "s1": {
                            "id": f"{loc}-S1",
                            "date": s1_when.date().isoformat(),
                            "sensor": "S1 SAR",
                            "cloud": 0,
                            "scene": s1["id"],
                            "chip": f"semantic/chips/{loc}-S1.jpg",
                        },
                    }
                    # Replace nodata with a small positive floor so log10 is defined.
                    vv_f = np.where(np.isfinite(vv) & (vv > 0), vv, 1e-4).astype(np.float32)
                    vh_f = np.where(np.isfinite(vh) & (vh > 0), vh, 1e-4).astype(np.float32)
                    np.savez_compressed(npz_path, s2=stack.astype(np.float32), s1=np.stack([vv_f, vh_f], 0))
                    save_s2_jpeg(stack, s2_jpg)
                    save_s1_jpeg(vv_f, vh_f, s1_jpg)
                    meta_path.write_text(json.dumps(meta, indent=2))
                    log(f"got {loc} S2 {meta['s2']['date']} cloud {cloud:.1f} S1 {meta['s1']['date']} water {meta['dist_water_m']:.0f}m")
                    return meta
    raise RuntimeError(f"{loc} failed: {last}")


def prep_tensor(arr: np.ndarray, sar: bool):
    import torch
    import torch.nn.functional as F

    x = torch.from_numpy(np.ascontiguousarray(arr)).float().unsqueeze(0)
    if not sar:
        x = x / 10000.0
    else:
        x = torch.where(torch.isfinite(x) & (x > 0), x, torch.full_like(x, 1e-4))
        x = 10.0 * torch.log10(x)
    x = F.interpolate(x, size=(224, 224), mode="bilinear", align_corners=False)
    return x


def embed_all(metas: list[dict], device: str) -> dict[str, np.ndarray]:
    import torch
    from transformers import AutoModel

    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    version_path = CACHE_DIR / "prep_version.txt"
    if version_path.exists() and version_path.read_text().strip() != PREP_VERSION:
        for stale in CACHE_DIR.glob("*.emb.npy"):
            stale.unlink()
    version_path.write_text(PREP_VERSION + "\n")
    log(f"loading {CHECKPOINT} on {device}")
    model = AutoModel.from_pretrained(CHECKPOINT, trust_remote_code=True)
    model = model.eval()
    if device != "cpu":
        model = model.to(device)
    out = {}
    with torch.inference_mode():
        for meta in metas:
            npz = np.load(CACHE_DIR / f"{meta['loc']}.npz")
            for key, sar in (("s2", False), ("s1", True)):
                tile = meta[key]["id"]
                emb_path = CACHE_DIR / f"{tile}.emb.npy"
                if emb_path.exists():
                    vec = np.load(emb_path)
                    out[tile] = vec
                    continue
                tensor = prep_tensor(npz["s2" if not sar else "s1"], sar=sar)
                if device != "cpu":
                    tensor = tensor.to(device)
                vec = model.get_image_features(tensor).detach().float().cpu().numpy()[0]
                norm = float(np.linalg.norm(vec))
                if abs(norm - 1.0) > 1e-3:
                    raise RuntimeError(f"{tile} embedding norm {norm}")
                np.save(emb_path, vec.astype(np.float32))
                out[tile] = vec
                log(f"embed {tile} norm {norm:.4f}")
            del npz
    text = {}
    with torch.inference_mode():
        for spec in QUERIES:
            obj = parse_object(spec["q"])
            if obj != spec["object"]:
                raise RuntimeError(f"object span {obj!r} != {spec['object']!r}")
            tok = model.tokenizer(
                obj,
                padding="max_length",
                truncation=True,
                max_length=64,
                return_tensors="pt",
            )
            ids = tok["input_ids"]
            mask = tok["attention_mask"]
            if device != "cpu":
                ids = ids.to(device)
                mask = mask.to(device)
            vec = model.get_text_features(ids, mask).detach().float().cpu().numpy()[0]
            text[spec["q"]] = vec
            log(f"text {obj!r} norm {float(np.linalg.norm(vec)):.4f}")
    del model
    return out, text


def rank(metas: list[dict], image_emb: dict[str, np.ndarray], text_emb: dict[str, np.ndarray]) -> dict:
    rows = []
    for meta in metas:
        for key in ("s2", "s1"):
            row = dict(meta[key])
            row.update({
                "aoi": meta["aoi"],
                "lat": meta["lat"],
                "lon": meta["lon"],
                "dist_water_m": meta["dist_water_m"],
                "elev_norm": meta["elev_norm"],
            })
            rows.append(row)
    newest = max(date.fromisoformat(row["date"]) for row in rows)
    queries = {}
    for spec in QUERIES:
        tvec = text_emb[spec["q"]]
        scored = []
        for row in rows:
            d = date.fromisoformat(row["date"])
            if spec["drop_before"] and d < date.fromisoformat(spec["drop_before"]):
                continue
            if spec["within_newest_days"] is not None and (newest - d).days > spec["within_newest_days"]:
                continue
            clip = float(np.dot(image_emb[row["id"]], tvec))
            boost = 0.0
            if "water" in spec["boost"]:
                boost += WATER_SCALE * math.exp(-row["dist_water_m"] / 600.0)
            if "elev" in spec["boost"]:
                boost += ELEV_SCALE * row["elev_norm"]
            if "monsoon" in spec["boost"] and d.month in (10, 11, 12):
                boost += MONSOON_SCALE
            score = min(0.99, clip + boost)
            item = {
                "id": row["id"],
                "aoi": row["aoi"],
                "lat": row["lat"],
                "lon": row["lon"],
                "date": row["date"],
                "sensor": row["sensor"],
                "cloud": row["cloud"],
                "chip": row["chip"],
                "clip": round(clip, 6),
                "score": round(score, 6),
                "scene": row["scene"],
            }
            scored.append(item)
        scored.sort(key=lambda item: (-item["score"], item["id"]))
        top = scored[:12]
        sensors = {item["sensor"] for item in top}
        log(f"query {spec['q']!r} kept {len(scored)} top sensors {sorted(sensors)} "
            f"scores {top[0]['score']:.4f}..{top[-1]['score']:.4f}" if top else f"query {spec['q']!r} empty")
        if len(top) < 12:
            raise RuntimeError(f"{spec['q']} produced {len(top)} rows")
        if spec["boost"] and "water" in spec["boost"] and sensors != {"S2 MSI", "S1 SAR"}:
            log(f"WARNING {spec['q']} top 12 sensors are {sensors}")
        queries[spec["q"]] = {"object": spec["object"], "results": top}
    optical = sum(1 for row in rows if row["sensor"] == "S2 MSI")
    sar = sum(1 for row in rows if row["sensor"] == "S1 SAR")
    return {
        "model": "CLOSP",
        "checkpoint": CHECKPOINT,
        "device": "cuda" if os.environ.get("CLOSP_DEVICE") == "cuda" else "cpu",
        "counts": {"optical": optical, "sar": sar},
        "newest_scene": newest.isoformat(),
        "queries": queries,
    }


def font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def contact_sheet(records: list[dict], path: Path, cols: int = 8) -> None:
    thumb = 148
    cap_h = 36
    pad = 8
    rows_n = math.ceil(len(records) / cols)
    canvas = Image.new("RGB", (cols * (thumb + pad) + pad, rows_n * (thumb + cap_h + pad) + pad), (10, 14, 18))
    draw = ImageDraw.Draw(canvas)
    face = font(11)
    for i, rec in enumerate(records):
        r, c = divmod(i, cols)
        x = pad + c * (thumb + pad)
        y = pad + r * (thumb + cap_h + pad)
        chip = Image.open(CHIP_DIR / f"{rec['id']}.jpg").convert("RGB").resize((thumb, thumb))
        canvas.paste(chip, (x, y))
        label = f"{rec['id']}  {rec['date']}"
        draw.text((x, y + thumb + 2), label, fill=(214, 221, 228), font=face)
        draw.text((x, y + thumb + 16), rec["sensor"], fill=(214, 168, 92) if rec["sensor"].startswith("S1") else (150, 168, 176), font=face)
    canvas.save(path, quality=85)
    log(f"sheet {path.name} {len(records)}")


def write_gallery(metas: list[dict]) -> None:
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    flat = []
    for meta in metas:
        for key in ("s2", "s1"):
            flat.append({**meta[key], "aoi": meta["aoi"]})
    s2 = [r for r in flat if r["sensor"] == "S2 MSI"]
    s1 = [r for r in flat if r["sensor"] == "S1 SAR"]
    contact_sheet(s2, GALLERY_DIR / "s2.jpg")
    contact_sheet(s1, GALLERY_DIR / "s1.jpg")
    contact_sheet(flat, GALLERY_DIR / "all.jpg")
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Trinetra semantic chips</title>
  <style>
    body {{ margin: 0; background: #0a0e12; color: #d6dde4; font: 14px/1.45 sans-serif; }}
    main {{ max-width: 1280px; margin: 0 auto; padding: 28px 20px 64px; }}
    h1 {{ font-weight: 400; font-size: 28px; margin: 0 0 8px; }}
    p {{ color: #8b98a3; max-width: 70ch; }}
    h2 {{ font-weight: 500; margin-top: 36px; }}
    img {{ width: 100%; height: auto; background: #111; }}
    a {{ color: #d6a24a; }}
  </style>
</head>
<body>
<main>
  <h1>CLOSP chip gallery</h1>
  <p>{len(s2)} Sentinel-2 optical chips and {len(s1)} Sentinel-1 SAR chips.
  Display JPEGs only. Rankings live in <a href="../index.json">index.json</a>.</p>
  <h2>Sentinel-2</h2>
  <img src="s2.jpg" alt="Sentinel-2 contact sheet" />
  <h2>Sentinel-1</h2>
  <img src="s1.jpg" alt="Sentinel-1 contact sheet" />
  <h2>All chips</h2>
  <img src="all.jpg" alt="All chips contact sheet" />
</main>
</body>
</html>
"""
    (GALLERY_DIR / "index.html").write_text(html)


def write_notes(index: dict) -> None:
    lines = [
        "# CLOSP Ask semantic index",
        "",
        f"Checkpoint: `{index['checkpoint']}`",
        f"Device: `{index['device']}`",
        f"Optical chips: {index['counts']['optical']}",
        f"SAR chips: {index['counts']['sar']}",
        f"Newest scene in the corpus: {index['newest_scene']}",
        "",
        "Gallery: `public/semantic/gallery/index.html` (contact sheets `s2.jpg`, `s1.jpg`, `all.jpg`).",
        "",
        "`clip` is the CLOSP cosine (L2-normalized text and image embeddings, dot product) from this model run.",
        "`score` is that cosine plus the small water, monsoon, or elevation term in `scripts/build_semantic_index.py`, clamped to 0.99.",
        "Neither number was typed in.",
        "",
        "Sentinel-2 chips are L2A, 13-band SSL4EO order (B10 left at zero because L2A has no cirrus band), divided by 10000 before the optical encoder.",
        "Sentinel-1 chips are GRD RTC gamma0 VV and VH from Planetary Computer, converted to dB before the SAR encoder.",
        "",
        "| Query | Object embedded | Top score | Sensors in top 12 |",
        "| --- | --- | --- | --- |",
    ]
    for spec in QUERIES:
        block = index["queries"][spec["q"]]
        sensors = sorted({row["sensor"] for row in block["results"]})
        top = block["results"][0]["score"]
        lines.append(f"| `{spec['q']}` | `{block['object']}` | {top:.4f} | {', '.join(sensors)} |")
    lines.append("")
    NOTES_PATH.write_text("\n".join(lines))


def flatten_check(index: dict) -> None:
    for spec in QUERIES:
        rows = index["queries"][spec["q"]]["results"]
        scores = [row["score"] for row in rows]
        if scores != sorted(scores, reverse=True):
            raise RuntimeError(f"{spec['q']} is not sorted")
        for row in rows:
            jpg = ROOT / "public" / row["chip"]
            if not jpg.exists():
                raise RuntimeError(f"missing {jpg}")
            if row["sensor"] == "S1 SAR" and row["cloud"] != 0:
                raise RuntimeError(f"{row['id']} SAR cloud")
            if row["score"] > 0.99:
                raise RuntimeError("score above clamp")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the CLOSP Ask semantic index")
    parser.add_argument("--points-only", action="store_true")
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()

    for spec in QUERIES:
        got = parse_object(spec["q"])
        if got != spec["object"]:
            raise SystemExit(f"parse mismatch {got!r} != {spec['object']!r}")

    CHIP_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    aois = load_aois()
    jobs = []
    for aoi in aois:
        pts = pick_points(aoi)
        near = sum(1 for p in pts if p["near_water"])
        log(f"{aoi['id']} points {len(pts)} near water {near} "
            f"dist {[round(p['dist_water_m']) for p in pts]}")
        for i, pt in enumerate(pts):
            jobs.append((aoi, i, pt))
    if args.points_only:
        return

    metas = []
    workers = 3
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(acquire_location, aoi, i, pt): (aoi["id"], i) for aoi, i, pt in jobs}
        for fut in as_completed(futs):
            ident = futs[fut]
            try:
                metas.append(fut.result())
            except Exception:
                log(f"FAILED {ident[0]}-{ident[1]:02d}")
                traceback.print_exc()
                pool.shutdown(cancel_futures=True)
                raise
    metas.sort(key=lambda m: m["loc"])
    if len(metas) != 56:
        raise SystemExit(f"expected 56 locations, got {len(metas)}")
    if args.download_only:
        log("download complete")
        return

    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    os.environ["CLOSP_DEVICE"] = device
    image_emb, text_emb = embed_all(metas, device)
    index = rank(metas, image_emb, text_emb)
    index["device"] = device
    flatten_check(index)
    INDEX_PATH.write_text(json.dumps(index, indent=2) + "\n")
    write_gallery(metas)
    write_notes(index)
    log(f"wrote {INDEX_PATH} optical {index['counts']['optical']} sar {index['counts']['sar']} device {device}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
