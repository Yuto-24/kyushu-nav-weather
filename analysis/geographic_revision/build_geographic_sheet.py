"""Render one common Kyushu A4 sheet per forecast using fixed geographic sectors.

The geometry and weather inputs are independent of the locations of the printed
tables. No route or leg assignment is used. Original prototypes are preserved.
Run with PYTHONPATH=/tmp/sector_geometry_deps when Shapely is not in the runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fitz
import numpy as np
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas
from shapely.geometry import LineString, Point, box, shape
from shapely.ops import polylabel, transform, unary_union

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
JP = "HeiseiKakuGo-W5"
pdfmetrics.registerFont(UnicodeCIDFont(JP))
W, H = landscape(A4)
CARD_W, CARD_H, BODY_PT = 56 * mm, 34.5 * mm, 8.5
ALTITUDES = [9500, 8500, 7500, 6500, 5500, 4500, 3500, 3000, 2500]
SECTOR_IDS_9 = list("ABCDEFGHJ")
SECTOR_IDS_8 = list("ABCDEGHJ")
SLOT_IDS = ((8, list("CEFHJ")), (233, list("ABDG")))
MAP_BOUNDS = (129.4, 30.45, 132.4, 33.95)
MAP_HEIGHT = 166 * mm
MAP_BOTTOM = 18 * mm
BADGE_RADIUS = 2.45 * mm


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def text(c, x, y, value, size=8, font=JP, align="left"):
    c.setFillGray(0)
    c.setFont(font, size)
    method = c.drawCentredString if align == "center" else c.drawRightString if align == "right" else c.drawString
    method(x, y, str(value))


def polygon(c, xy, fill=False, stroke=True):
    p = c.beginPath()
    p.moveTo(*xy[0])
    for q in xy[1:]:
        p.lineTo(*q)
    p.close()
    c.drawPath(p, fill=int(fill), stroke=int(stroke))


def number(value, decimals=0, sign=False):
    if value is None:
        return "?"
    value = round(float(value), decimals)
    if value == 0:
        return "0" if decimals == 0 else f"{0:.{decimals}f}"
    return f"{value:+.{decimals}f}" if sign else f"{value:.{decimals}f}"


def wind_direction_10deg(value):
    """Round the true wind-from direction half up to 10 degrees; north is 360."""
    direction = float(value)
    if not math.isfinite(direction):
        raise ValueError("Wind direction must be finite.")
    rounded = int(math.floor(((direction % 360) + 5) / 10) * 10)
    return rounded or 360


def timestamp(value, date=True):
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone(timedelta(hours=9)))
        return parsed.strftime("%Y-%m-%d %H:%M JST" if date else "%H:%M JST")
    except ValueError:
        return str(value) if "JST" in str(value) else str(value) + " JST"


def display_row(row, altitude):
    marker = {"terrain": "T", "missing": "?", "layout_only": "—"}.get(row.get("status", "valid"))
    if marker:
        return [str(altitude), marker, marker, marker, marker]
    direction, speed = row.get("wind_from_true_deg"), row.get("wind_speed_kt")
    wind = "?" if direction is None or speed is None else f"{wind_direction_10deg(direction):03d}/{int(round(speed)):02d}"
    if row.get("spread_flag"):
        wind += "*"
    if row.get("partial_flag"):
        wind += "†"
    return [str(altitude), number(row.get("temperature_c"), sign=True),
            number(row.get("cas_kt")), number(row.get("tas_kt")), wind]


def table_slots(sector_ids):
    return {gid: (side * mm, (187 - i * 35.75) * mm - CARD_H)
            for side, ids in SLOT_IDS for i, gid in enumerate(ids) if gid in sector_ids}


def draw_card(c, gid, name, data, slot):
    x, y = slot
    top = y + CARD_H
    c.setFillGray(1)
    c.setStrokeGray(0)
    c.setLineWidth(.8)
    c.rect(x, y, CARD_W, CARD_H, fill=1, stroke=1)
    c.setFillGray(0)
    c.rect(x, top - 4.6 * mm, 7 * mm, 4.6 * mm, fill=1, stroke=0)
    c.setFillGray(1)
    c.setFont("Helvetica-Bold", 9.4)
    c.drawCentredString(x + 3.5 * mm, top - 3.35 * mm, gid)
    title_size = 8.4
    if pdfmetrics.stringWidth(name, JP, title_size) > 46.3 * mm:
        title_size = 7.5
    if pdfmetrics.stringWidth(name, JP, title_size) > 46.3 * mm:
        raise ValueError(f"Sector {gid} name is too long for the table header: {name}")
    text(c, x + 8.2 * mm, top - 3.35 * mm, name, title_size)
    c.setStrokeGray(0)
    c.setLineWidth(.55)
    c.line(x, top - 4.6 * mm, x + CARD_W, top - 4.6 * mm)
    cols = np.asarray([0, 9.5, 17.5, 25.5, 33.5, 56]) * mm
    for i, label in enumerate(["ft MSL", "OAT ℃", "CAS", "TAS", "WIND °T/kt"]):
        text(c, x + (cols[i] + cols[i + 1]) / 2, top - 7.45 * mm, label, 7.1, align="center")
    c.setLineWidth(.45)
    c.line(x, top - 8.55 * mm, x + CARD_W, top - 8.55 * mm)
    rows = {int(r["altitude_ft_msl"]): r for r in data["rows"]}
    if len(data["rows"]) != 9 or set(rows) != set(ALTITUDES):
        raise ValueError(f"Sector {gid} must contain all nine distinct altitudes.")
    body_top, row_height = top - 8.55 * mm, (CARD_H - 8.55 * mm) / 9
    for i, altitude in enumerate(ALTITUDES):
        values = display_row(rows[altitude], altitude)
        baseline = body_top - (i + .5) * row_height - BODY_PT * .718 / 2
        for j, value in enumerate(values):
            font = "Helvetica-Bold" if j == 0 else "Helvetica"
            if pdfmetrics.stringWidth(value, font, BODY_PT) > cols[j + 1] - cols[j] - .8 * mm:
                raise ValueError(f"Sector {gid} {altitude} value is too wide: {value}")
            text(c, x + (cols[j] + cols[j + 1]) / 2, baseline, value, BODY_PT, font=font, align="center")
        if i < 8:
            c.setLineWidth(.4)
            c.line(x, body_top - (i + 1) * row_height, x + CARD_W, body_top - (i + 1) * row_height)


def projection():
    lon0, lat0, lon1, lat1 = MAP_BOUNDS
    sy = MAP_HEIGHT / (lat1 - lat0)
    sx = sy * math.cos(math.radians(32))
    x0 = (W - (lon1 - lon0) * sx) / 2
    def project(x, y, z=None):
        return x0 + (np.asarray(x) - lon0) * sx, MAP_BOTTOM + (np.asarray(y) - lat0) * sy
    def unproject(x, y):
        return lon0 + (x - x0) / sx, lat0 + (y - MAP_BOTTOM) / sy
    return project, unproject, (x0, MAP_BOTTOM, (lon1 - lon0) * sx, MAP_HEIGHT)


def load_geometry(path):
    payload = read_json(path)
    if payload.get("type") != "FeatureCollection":
        raise ValueError("Sector geometry must be a GeoJSON FeatureCollection.")
    features = {f["properties"]["id"]: f for f in payload["features"]}
    if len(payload["features"]) != len(features) or set(features) not in (set(SECTOR_IDS_8), set(SECTOR_IDS_9)):
        raise ValueError("Fixed geographic template requires A,B,C,D,E,G,H,J, with optional internal-comparison F.")
    sector_ids = sorted(features)
    shapes = {gid: shape(features[gid]["geometry"]) for gid in sector_ids}
    for gid, geometry in shapes.items():
        if geometry.geom_type not in ("Polygon", "MultiPolygon") or not geometry.is_valid or geometry.is_empty:
            raise ValueError(f"Invalid sector geometry {gid}.")
        if not box(*MAP_BOUNDS).covers(geometry):
            raise ValueError(f"Sector {gid} falls outside the map extent.")
    union = unary_union(list(shapes.values()))
    overlap = sum(g.area for g in shapes.values()) - union.area
    if overlap > 1e-9:
        raise ValueError(f"Sector overlaps exceed numeric tolerance: {overlap} deg².")
    return features, shapes, {
        "source": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "sector_ids": sector_ids, "overlap_area_deg2": overlap,
        "components_per_sector": {gid: 1 if g.geom_type == "Polygon" else len(g.geoms) for gid, g in shapes.items()},
        "bounds": list(union.bounds),
    }


def stroke_geometry(c, geometry):
    if geometry.is_empty:
        return
    if geometry.geom_type in ("LineString", "LinearRing"):
        coords = list(geometry.coords)
        p = c.beginPath()
        p.moveTo(*coords[0][:2])
        for xy in coords[1:]:
            p.lineTo(*xy[:2])
        c.drawPath(p, fill=0, stroke=1)
    elif geometry.geom_type == "Polygon":
        stroke_geometry(c, geometry.boundary)
    else:
        for part in geometry.geoms:
            stroke_geometry(c, part)


def reference_labels(refs, project):
    # Geographic references only. The routes array is intentionally not read.
    config = {
        "RJFM": ("RJFM", 2, -3), "RJFO": ("RJFO", 2, 1), "RJFG": ("RJFG", 2, -3),
        "RJFT": ("RJFT", 2, -3), "RJFK": ("RJFK", 2, 1), "RJFU": ("RJFU", -12, 2),
        "RJFS": ("RJFS", -7, 3), "IWAISHIMA": ("祝島", 2, 1),
        "TSURUMISAKI_OUTBOUND": ("鶴御崎", 2, 1), "OMARU": ("小丸", 2, 1),
        "OSHIMA": ("南郷大島", 2, 1), "TOIMISAKI": ("都井岬", 2, -3),
        "NOMAMISAKI": ("野間岬", -12, 2), "MAKURAZAKI": ("枕崎", 1, -3),
        "KOMENOTSU_STATION": ("米ノ津駅", 2, -3), "SHISHIJIMA": ("獅子島", 2, 2),
        "UBUSHIMA": ("産島", -9, -2), "YAME_IC": ("八女IC", -4, 2),
        "TAMANA": ("玉名", -7, 1),
    }
    records = []
    for point in refs["points"]:
        pid = point["id"]
        if pid not in config:
            continue
        label, dx, dy = config[pid]
        x, y = project(point["longitude_deg"], point["latitude_deg"])
        airport = point["kind"] == "airport"
        size, font = (7.1, "Helvetica-Bold") if airport else (6.4, JP)
        tx, ty = x + dx * mm, y + dy * mm
        bounds = (tx - .3 * mm, ty - .35 * mm,
                  tx + pdfmetrics.stringWidth(label, font, size) + .3 * mm, ty + size * .85)
        records.append({"id": pid, "label": label, "airport": airport, "x": float(x), "y": float(y),
                        "tx": float(tx), "ty": float(ty), "size": size, "font": font, "bounds": bounds})
    return records


def leader_layout(shapes, slots, references, project, unproject):
    projected = {gid: transform(project, geometry) for gid, geometry in shapes.items()}
    obstacles = [box(*r["bounds"]).buffer(.45 * mm) for r in references]
    obstacles += [Point(r["x"], r["y"]).buffer(1.1 * mm) for r in references]
    obstacle_union = unary_union(obstacles)
    leaders, badge_points, checks = [], [], []
    for _, ids in SLOT_IDS:
        for gid in ids:
            if gid not in shapes:
                continue
            region = projected[gid]
            largest = region if region.geom_type == "Polygon" else max(region.geoms, key=lambda p: p.area)
            center = polylabel(largest, tolerance=.2 * mm)
            candidates = [center, largest.representative_point()]
            bx0, by0, bx1, by1 = region.bounds
            for x in np.arange(bx0 + 2 * mm, bx1 - 2 * mm, 2.3 * mm):
                for y in np.arange(by0 + 2 * mm, by1 - 2 * mm, 2.3 * mm):
                    p = Point(x, y)
                    if region.contains(p) and p.distance(region.boundary) > BADGE_RADIUS + .5 * mm:
                        candidates.append(p)
            x, y = slots[gid]
            left = x < W / 2
            start_x = x + CARD_W if left else x
            gutter_x = (75 if left else 222) * mm
            best = None
            for p in candidates:
                if p.distance(region.boundary) < BADGE_RADIUS + .3 * mm or p.distance(obstacle_union) < BADGE_RADIUS + .3 * mm:
                    continue
                if any(p.distance(other) < 2 * BADGE_RADIUS + 1.5 * mm for other in badge_points):
                    continue
                if any(p.distance(old) < BADGE_RADIUS + .5 * mm for old in leaders):
                    continue
                for dy in (0, -5, 5, -10, 10):
                    start_y = y + CARD_H / 2 + dy * mm
                    coords = [(start_x, start_y), (gutter_x, start_y), (p.x, p.y)]
                    leader = LineString(coords)
                    if leader.intersects(obstacle_union):
                        continue
                    if any(leader.crosses(old) or leader.intersection(old).length > .1 * mm for old in leaders):
                        continue
                    if any(leader.distance(other) < BADGE_RADIUS + .5 * mm for other in badge_points):
                        continue
                    score = leader.length + .65 * p.distance(center) + .4 * abs(dy) * mm
                    if best is None or score < best[0]:
                        best = score, p, leader
            if best is None:
                raise ValueError(f"No visible non-crossing table connector found for sector {gid}.")
            _, point, leader = best
            leaders.append(leader)
            badge_points.append(point)
            checks.append({"sector_id": gid, "anchor_lon_lat": list(unproject(point.x, point.y)),
                           "anchor_page_mm": [point.x / mm, point.y / mm],
                           "leader_page_mm": [[x / mm, y / mm] for x, y in leader.coords],
                           "inside_own_sector": bool(region.contains(point)),
                           "boundary_clearance_mm": point.distance(region.boundary) / mm,
                           "anchor_role": "Visible table connection location inside its fixed geographic sector; not a point weather sample.",
                           "visible_outside_tables": True})
    return projected, leaders, badge_points, checks


def draw_map(c, shapes, slots, refs):
    project, unproject, rectangle = projection()
    references = reference_labels(refs, project)
    projected, leaders, badge_points, checks = leader_layout(shapes, slots, references, project, unproject)
    c.saveState()
    clip = c.beginPath()
    clip.rect(*rectangle)
    c.clipPath(clip, stroke=0)
    c.setFillGray(.965)
    c.setStrokeGray(.48)
    c.setLineWidth(.35)
    for feature in read_json(ROOT / "kyushu_coastline.geojson")["features"]:
        polygons = feature["geometry"]["coordinates"]
        if feature["geometry"]["type"] == "Polygon":
            polygons = [polygons]
        for rings in polygons:
            for ring in rings:
                polygon(c, [project(*q[:2]) for q in ring], fill=True)
    boundaries = unary_union([g.boundary for g in projected.values()])
    c.setStrokeGray(0)
    c.setLineWidth(.75)
    c.setDash(2.3, 1.7)
    stroke_geometry(c, boundaries)
    c.setDash()
    c.restoreState()
    for leader in leaders:
        c.setStrokeGray(1)
        c.setLineWidth(2.2)
        stroke_geometry(c, leader)
        c.setStrokeGray(0)
        c.setLineWidth(.7)
        stroke_geometry(c, leader)
    for r in references:
        c.setFillGray(1)
        x0, y0, x1, y1 = r["bounds"]
        c.rect(x0, y0, x1 - x0, y1 - y0, fill=1, stroke=0)
        text(c, r["tx"], r["ty"], r["label"], r["size"], font=r["font"])
        c.setFillGray(0)
        if r["airport"]:
            c.rect(r["x"] - .5 * mm, r["y"] - .5 * mm, mm, mm, fill=1, stroke=0)
        else:
            c.circle(r["x"], r["y"], .35 * mm, fill=1, stroke=0)
    for record, point in zip(checks, badge_points):
        c.setFillGray(1)
        c.setStrokeGray(0)
        c.setLineWidth(.85)
        c.circle(point.x, point.y, BADGE_RADIUS, fill=1, stroke=1)
        text(c, point.x, point.y - 9.3 * .718 / 2, record["sector_id"], 9.3, font="Helvetica-Bold", align="center")
    # North orientation is explicit, without adding a navigation route or point.
    nx, ny = 92 * mm, 179 * mm
    c.setStrokeGray(0)
    c.setLineWidth(.7)
    c.line(nx, ny - 5 * mm, nx, ny)
    c.line(nx, ny, nx - 1 * mm, ny - 1.8 * mm)
    c.line(nx, ny, nx + 1 * mm, ny - 1.8 * mm)
    text(c, nx, ny + 1.5 * mm, "N", 7.5, font="Helvetica-Bold", align="center")
    return checks, references, rectangle


def draw_legend(c, metadata, layout_only, sector_ids):
    x, y, width, height = 233 * mm, 9.5 * mm, CARD_W, CARD_H
    c.setStrokeGray(0)
    c.setFillGray(1)
    c.setLineWidth(.65)
    c.rect(x, y, width, height, fill=1, stroke=1)
    text(c, x + 2 * mm, y + height - 4 * mm, "記号・表示", 8.2)
    threshold = metadata.get("spread_flag_threshold_kt", 5)
    spread_lines = metadata.get("spread_flag_legend_ja", [f"* 域内の風の差 RMS > {threshold:g} kt", "  試作基準・未確定"])
    if isinstance(spread_lines, str):
        spread_lines = [spread_lines]
    lines = list(spread_lines) + ["† 一部点を地形・補間条件で除外", "T：地形以下　?：取得不可", "印は当該表・当該高度に対応", "風向：真方位・10°刻み（北360）"]
    if len(lines) > 7:
        raise ValueError("Legend must fit at most seven lines.")
    for i, line in enumerate(lines):
        if pdfmetrics.stringWidth(line, JP, 6.8) > width - 4 * mm:
            raise ValueError(f"Legend line too long: {line}")
        text(c, x + 2 * mm, y + height - (9 + i * 3.7) * mm, line, 6.8)
    # Short map legend sits in the broad central margin left of the map.
    text(c, 69 * mm, 181 * mm, f"固定{len(sector_ids)}セクター", 8.0)
    text(c, 69 * mm, 176 * mm, "全6方面共通", 7.0)
    text(c, 69 * mm, 171.5 * mm, "経路未定で印刷可", 6.6)
    text(c, 69 * mm, 167 * mm, f"モデル：{metadata.get('model', '入力待ち')}", 6.6)
    if "F" not in sector_ids:
        lx, ly = 8 * mm, 81 * mm
        top = ly + CARD_H
        c.setStrokeGray(0)
        c.setFillGray(1)
        c.setLineWidth(.65)
        c.rect(lx, ly, CARD_W, CARD_H, fill=1, stroke=1)
        text(c, lx + 2 * mm, top - 4 * mm, "地図・巡航条件", 8.2)
        c.setStrokeGray(0)
        c.setLineWidth(.75)
        c.setDash(2.3, 1.7)
        c.line(lx + 2 * mm, top - 8.5 * mm, lx + 9 * mm, top - 8.5 * mm)
        c.setDash()
        text(c, lx + 11 * mm, top - 9.4 * mm, "固定セクターの境界", 7)
        c.setLineWidth(.7)
        c.line(lx + 2 * mm, top - 13 * mm, lx + 9 * mm, top - 13 * mm)
        text(c, lx + 11 * mm, top - 13.9 * mm, "表への引出線", 7)
        c.setFillGray(1)
        c.circle(lx + 5.5 * mm, top - 18 * mm, 1.85 * mm, fill=1, stroke=1)
        text(c, lx + 5.5 * mm, top - 18 * mm - 7.4 * .718 / 2, "A", 7.4, font="Helvetica-Bold", align="center")
        text(c, lx + 11 * mm, top - 18.9 * mm, "区域と表の対応", 7)
        text(c, lx + 2 * mm, top - 23.4 * mm, "丸英字は気象の抽出点ではない", 6.8)
        text(c, lx + 2 * mm, top - 28 * mm, "SR22 65% BP / MSL=PA", 6.8)
        text(c, lx + 2 * mm, top - 32.1 * mm, "フェアリング有・A/C OFF", 6.8)
    else:
        text(c, W / 2, 15 * mm,
             "破線：固定区域境界　実線：引出線　丸英字：表の接続位置（抽出点ではない）",
             6.8, align="center")


def render(forecast, features, shapes, geometry_checks, refs, layout_only, stem, outdir):
    metadata = forecast.get("metadata", {})
    groups = {g["id"]: g for g in forecast["groups"]}
    sector_ids = sorted(shapes)
    if len(forecast["groups"]) != len(shapes) or set(groups) != set(shapes):
        raise ValueError("Weather input must contain exactly the geographic sector IDs in the supplied geometry.")
    pdf = outdir / f"{stem}.pdf"
    c = canvas.Canvas(str(pdf), pagesize=(W, H))
    c.setTitle("九州 NAV LOG 固定地理セクター気象資料")
    valid = timestamp(metadata.get("valid_time_jst", "HH:MM"), date=False)
    target = metadata.get("target_date", "対象日未入力")
    historical = "HISTORICAL" in metadata.get("status", "").upper()
    caption = f"{target}の{'過去' if historical else ''}予報・試作"
    text(c, 8 * mm, 199.1 * mm, "九州 NAV LOG 気象資料", 13)
    text(c, 110 * mm, 199.4 * mm, valid, 11, font="Helvetica-Bold")
    text(c, 289 * mm, 199.5 * mm, "全6方面共通 / 配置確認用" if layout_only else caption + " / 全6方面共通", 8.2, align="right")
    init, generated = timestamp(metadata.get("initial_time_jst")), timestamp(metadata.get("generated_time_jst"))
    text(c, 8 * mm, 193.1 * mm, f"対象日：{target}   予報対象：{valid}   初期：{init}   生成：{generated}", 7.6)
    c.setStrokeGray(0)
    c.setLineWidth(.8)
    c.line(8 * mm, 190.5 * mm, 289 * mm, 190.5 * mm)
    slots = table_slots(sector_ids)
    map_checks, reference_records, map_rectangle = draw_map(c, shapes, slots, refs)
    for gid in sector_ids:
        name = features[gid]["properties"].get("name_ja", f"セクター {gid}")
        draw_card(c, gid, name, groups[gid], slots[gid])
    draw_legend(c, metadata, layout_only, sector_ids)
    text(c, W / 2, 10.6 * mm, "配置確認用・気象値未入力" if layout_only else caption, 8.2, align="center")
    text(c, 8 * mm, 5.3 * mm,
         "気温：℃ / 高度：ft MSL / 風・CAS・TAS：kt" if "F" not in sector_ids else "SR22 65% BP / フェアリング有・A/C OFF / MSL=PA", 6.5)
    text(c, 289 * mm, 5.3 * mm, "風・CAS・TAS：kt / 地名・空港は概略参照 / 地図：Natural Earth", 6.5, align="right")
    c.showPage()
    c.save()
    with fitz.open(pdf) as doc:
        if len(doc) != 1:
            raise ValueError("Each forecast must produce exactly one A4 page.")
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72), colorspace=fitz.csGRAY, alpha=False)
        pix.set_dpi(300, 300)
        pix.save(str(outdir / f"{stem}.png"))
        page.get_pixmap(matrix=fitz.Matrix(110 / 72, 110 / 72), colorspace=fitz.csGRAY, alpha=False).save(str(outdir / f"{stem}_qa.png"))
        page_mm = [page.rect.width / mm, page.rect.height / mm]
    leader_lines = [LineString(item["leader_page_mm"]) for item in map_checks]
    crossing_count = sum(leader_lines[i].crosses(leader_lines[j]) for i in range(len(leader_lines)) for j in range(i))
    return {"pdf": str(pdf), "png": str(outdir / f"{stem}.png"), "a4_mm": page_mm,
            "tables": len(groups), "table_mm": [56, 34.5], "weather_number_font_pt": BODY_PT,
            "altitudes_ft_msl_descending": ALTITUDES, "wind_direction_rounding": "10-degree half-up; north 360",
            "map_rectangle_mm": [v / mm for v in map_rectangle], "map_connection_checks": map_checks,
            "map_connections": len(map_checks), "leader_crossing_count": int(crossing_count),
            "routes_drawn": 0, "leg_index_entries": 0, "geometry": geometry_checks,
            "airport_ids": [r["id"] for r in reference_records if r["airport"]],
            "spread_rows": sum(bool(r.get("spread_flag")) for g in groups.values() for r in g["rows"]),
            "partial_rows": sum(bool(r.get("partial_flag")) for g in groups.values() for r in g["rows"]),
            "table_reference_mode": "Fixed geographic polygon and visible lettered table connector; no fixed route lookup.",
            "layout_only": layout_only, "source_weather_metadata": metadata}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=HERE / "paper_forecasts.json")
    parser.add_argument("--geometry", type=Path, default=HERE / "geographic_sectors_8.geojson")
    parser.add_argument("--output-dir", type=Path, default=HERE)
    parser.add_argument("--layout-only", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    features, shapes, geometry_checks = load_geometry(args.geometry)
    sector_ids = sorted(shapes)
    layout_only = args.layout_only or not args.input.exists()
    if layout_only:
        payload = {"forecasts": [{"metadata": {}, "groups": [
            {"id": gid, "rows": [{"altitude_ft_msl": altitude, "status": "layout_only"} for altitude in ALTITUDES]}
            for gid in sector_ids]}]}
    else:
        payload = read_json(args.input)
    reports = []
    refs = read_json(ROOT / "route_reference_draft.json")
    for forecast in payload.get("forecasts", [payload]):
        if layout_only:
            stem = f"geographic_sheet_{len(sector_ids)}_layout"
        else:
            metadata = forecast["metadata"]
            date_token = datetime.strptime(metadata["target_date"], "%Y-%m-%d").strftime("%Y%m%d")
            time_token = timestamp(metadata["valid_time_jst"], date=False).split()[0].replace(":", "")
            if len(time_token) != 4 or not time_token.isdigit():
                raise ValueError("Forecast time must resolve to HH:MM JST.")
            stem = f"geographic_sheet_{len(sector_ids)}_{date_token}_{time_token}"
        reports.append(render(forecast, features, shapes, geometry_checks, refs, layout_only, stem, args.output_dir))
    (args.output_dir / f"geographic_layout_checks_{len(sector_ids)}.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([{key: report[key] for key in ("pdf", "png", "tables", "map_connections", "leader_crossing_count", "spread_rows", "partial_rows", "layout_only")} for report in reports], ensure_ascii=False))


if __name__ == "__main__":
    main()
