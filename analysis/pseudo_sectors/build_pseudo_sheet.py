"""A4 midpoint lookup sheet: fuel per minute, larger aligned numbers, no X marks."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fitz
import numpy as np
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from shapely.geometry import LineString, Point, box, shape
from shapely.ops import polylabel, transform, unary_union

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "analysis/geographic_revision"))
import build_geographic_sheet as base

# Embed metrically compatible numeric fonts so printing and PDF viewers keep
# the same glyph widths; relying on an unembedded core font changed the digits
# in the independent Poppler rendering in this environment.
pdfmetrics.registerFont(TTFont("Helvetica", str(HERE / "fonts/LiberationSans-Regular.ttf")))
pdfmetrics.registerFont(TTFont("Helvetica-Bold", str(HERE / "fonts/LiberationSans-Bold.ttf")))

IDS = list("ABCDEFGHIJ")
SIDES = ((8, list("ABCDE")), (223, list("FGHIJ")))
CARD_W, CARD_H = 66 * mm, 34.4 * mm
HEADER_H, BODY_PT, WIND_PT = 5.6 * mm, 9.8, 10.1
TAB_W, TAB_H, BADGE_RADIUS = 6.2 * mm, 6.5 * mm, 3.0 * mm
COLS = np.cumsum([0, 10, 7, 8, 8, 19, 14]) * mm
base.MAP_HEIGHT, base.MAP_BOTTOM = 176 * mm, 8 * mm
OUTPUT = ROOT / "output"
WEATHER = HERE / "weather/paper_forecasts.json"
GEOMETRY = HERE / "geometry/pseudo_sectors_10.geojson"
BOUNDARIES = HERE / "geometry/pseudo_boundaries_10.geojson"
SELECTION = HERE / "design/selection_10.json"
JST = timezone(timedelta(hours=9))


def read(path):
    return json.loads(Path(path).read_text())


def inputs(weather_path=WEATHER):
    geo, selection = read(GEOMETRY), read(SELECTION)
    features = {f["properties"]["id"]: f for f in geo["features"]}
    assert set(features) == set(IDS) and len(geo["features"]) == 10
    shapes = {k: shape(v["geometry"]) for k, v in features.items()}
    assert all(g.is_valid and not g.is_empty for g in shapes.values())
    union = unary_union(list(shapes.values()))
    assert abs(sum(g.area for g in shapes.values()) - union.area) < 1e-8
    reps = {r["sector_id"]: r for r in selection["representatives"]}
    for gid in IDS:
        assert shapes[gid].contains(Point(reps[gid]["midpoint_lon_lat"]))
    for name, expected in read(HERE / "presentation_v2/decision.json")["geometry_and_representatives_sha256"].items():
        assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == expected
    forecasts = read(weather_path)["forecasts"]
    assert 1 <= len(forecasts) <= 2
    for forecast in forecasts:
        assert {g["id"] for g in forecast["groups"]} == set(IDS)
        for group in forecast["groups"]:
            assert group["representative_leg_id"] == reps[group["id"]]["leg_id"]
            assert [r["altitude_ft_msl"] for r in group["rows"]] == base.ALTITUDES
            assert all(r.get("status", "valid") in {"valid", "terrain", "missing", "layout_only"}
                       for r in group["rows"])
    return shapes, forecasts


def table_slots():
    return {gid: (side * mm, (186 - i * 36) * mm - CARD_H)
            for side, ids in SIDES for i, gid in enumerate(ids)}


def display_row(row):
    altitude = int(row["altitude_ft_msl"])
    state = {"terrain": "T", "missing": "?", "layout_only": "—"}.get(row.get("status", "valid"))
    if state:
        return [str(altitude)] + [state] * 5, ""
    direction, speed = row.get("wind_from_true_deg"), row.get("wind_speed_kt")
    wind = "?" if direction is None or speed is None else f"{base.wind_direction_10deg(direction):03d}/{int(round(speed)):02d}"
    marks = ("*" if row.get("spread_flag") else "") + ("!" if row.get("substitution_flag") else "") + ("†" if row.get("partial_flag") else "")
    return [str(altitude), base.number(row.get("temperature_c"), sign=True),
            base.number(row.get("cas_kt")), base.number(row.get("tas_kt")),
            wind, row.get("fuel_per_minute_us_gal_display") or "?"], marks


def draw_card(c, gid, data, slot, connection):
    x, y = slot
    top, body_top = y + CARD_H, y + CARD_H - HEADER_H
    c.setFillGray(1); c.rect(x, y, CARD_W, CARD_H, fill=1, stroke=0)
    c.setFillGray(.945); c.rect(x, y, COLS[1], CARD_H, fill=1, stroke=0)
    c.setFillGray(.94); c.rect(x, body_top, CARD_W, HEADER_H, fill=1, stroke=0)
    labels = [("ALT", "ft MSL"), ("OAT", "°C"), ("CAS", "kt"),
              ("TAS", "kt"), ("WIND", "°T / kt"), ("FUEL", "US gal/min")]
    for i, (label, unit) in enumerate(labels):
        cx = x + (COLS[i] + COLS[i + 1]) / 2
        base.text(c, cx, top - 2.25 * mm, label, 7.8, font="Helvetica-Bold", align="center")
        base.text(c, cx, top - 4.65 * mm, unit, 6.8, font="Helvetica", align="center")
    c.setStrokeGray(0)
    for i in range(1, len(COLS) - 1):
        c.setLineWidth(.24); c.line(x + COLS[i], y, x + COLS[i], top)
    c.setLineWidth(.65); c.line(x, body_top, x + CARD_W, body_top)
    row_h = (CARD_H - HEADER_H) / 9
    rows = {int(r["altitude_ft_msl"]): r for r in data["rows"]}
    rendered = []
    for i, altitude in enumerate(base.ALTITUDES):
        values, marks = display_row(rows[altitude])
        baseline = body_top - (i + .5) * row_h - BODY_PT * .718 / 2
        for col, value in enumerate(values):
            font = "Helvetica-Bold" if col in (0, 4) else "Helvetica"
            size = WIND_PT if col == 4 else BODY_PT
            available = COLS[col + 1] - COLS[col] - (4.4 if col == 4 else 1.1) * mm
            assert pdfmetrics.stringWidth(value, font, size) <= available, (gid, altitude, col, value)
            if col == 4:
                # A fixed suffix area keeps all wind digits aligned despite flags.
                base.text(c, x + COLS[col] + 7.6 * mm, baseline, value, size, font=font, align="center")
                if marks:
                    base.text(c, x + COLS[col] + 14.4 * mm, baseline + 1.2, marks, 7.0, font="Helvetica-Bold")
            else:
                pad = {0: 1.0, 1: .65, 2: 1.0, 3: 1.0, 5: 1.8}[col] * mm
                base.text(c, x + COLS[col + 1] - pad, baseline, value, size, font=font, align="right")
        rendered.append({"altitude_ft_msl": altitude, "values": values, "marks": marks})
        if i < 8:
            c.setLineWidth(.48 if altitude in (7500, 5500, 3500) else .24)
            c.line(x, body_top - (i + 1) * row_h, x + CARD_W, body_top - (i + 1) * row_h)
    c.setLineWidth(.8); c.rect(x, y, CARD_W, CARD_H, fill=0, stroke=1)
    ty = connection["leader_page_mm"][0][1] * mm
    tx = x + CARD_W if x < base.W / 2 else x - TAB_W
    c.setFillGray(0); c.rect(tx, ty - TAB_H / 2, TAB_W, TAB_H, fill=1, stroke=0)
    c.setFillGray(1); c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(tx + TAB_W / 2, ty - 12 * .718 / 2, gid)
    return rendered


def leader_layout(shapes, slots, references, project, unproject):
    projected = {gid: transform(project, geom) for gid, geom in shapes.items()}
    obstacles = [box(*r["bounds"]).buffer(.45 * mm) for r in references]
    obstacles += [Point(r["x"], r["y"]).buffer(1.1 * mm) for r in references]
    obstacle_union = unary_union(obstacles)
    leaders, badges, checks = [], [], []
    for _, ids in SIDES:
        for gid in ids:
            region = projected[gid]
            center = polylabel(region, tolerance=.2 * mm)
            candidates = [center, region.representative_point()]
            bx0, by0, bx1, by1 = region.bounds
            for px in np.arange(bx0 + 3.5 * mm, bx1 - 3.5 * mm, 2.2 * mm):
                for py in np.arange(by0 + 3.5 * mm, by1 - 3.5 * mm, 2.2 * mm):
                    point = Point(px, py)
                    if region.contains(point) and point.distance(region.boundary) > BADGE_RADIUS + .6 * mm:
                        candidates.append(point)
            x, y = slots[gid]
            left = x < base.W / 2
            sx = x + CARD_W + TAB_W if left else x - TAB_W
            gx, sy = (82 if left else 215) * mm, y + CARD_H / 2
            best = None
            for point in candidates:
                if point.distance(region.boundary) < BADGE_RADIUS + .4 * mm or point.distance(obstacle_union) < BADGE_RADIUS + .5 * mm:
                    continue
                if any(point.distance(p) < 2 * BADGE_RADIUS + 1.5 * mm for p in badges):
                    continue
                if any(point.distance(old) < BADGE_RADIUS + .5 * mm for old in leaders):
                    continue
                leader = LineString([(sx, sy), (gx, sy), (point.x, point.y)])
                if leader.intersects(obstacle_union):
                    continue
                if any(leader.crosses(old) or leader.intersection(old).length > .1 * mm for old in leaders):
                    continue
                if any(leader.distance(p) < BADGE_RADIUS + .5 * mm for p in badges):
                    continue
                score = leader.length + 1.2 * point.distance(center)
                if best is None or score < best[0]:
                    best = score, point, leader
            if best is None:
                raise ValueError(f"No visible connector for {gid}")
            _, point, leader = best
            leaders.append(leader); badges.append(point)
            checks.append({"sector_id": gid, "anchor_lon_lat": list(unproject(point.x, point.y)),
                           "anchor_page_mm": [point.x / mm, point.y / mm],
                           "leader_page_mm": [[px / mm, py / mm] for px, py in leader.coords],
                           "leader_length_mm": leader.length / mm,
                           "inside_own_sector": bool(region.contains(point)),
                           "boundary_clearance_mm": point.distance(region.boundary) / mm,
                           "anchor_role": "Visible connector in lookup area; not a midpoint or weather extraction point.",
                           "visible_outside_tables": True})
    return projected, leaders, badges, checks


def draw_map(c, shapes, slots, refs):
    project, unproject, rectangle = base.projection()
    references = base.reference_labels(refs, project)
    _, leaders, badges, checks = leader_layout(shapes, slots, references, project, unproject)
    c.saveState()
    clip = c.beginPath(); clip.rect(*rectangle); c.clipPath(clip, stroke=0)
    c.setFillGray(.975); c.setStrokeGray(.43); c.setLineWidth(.35)
    for feature in read(ROOT / "kyushu_coastline.geojson")["features"]:
        coords = feature["geometry"]["coordinates"]
        polygons = [coords] if feature["geometry"]["type"] == "Polygon" else coords
        for rings in polygons:
            for ring in rings:
                base.polygon(c, [project(*p[:2]) for p in ring], fill=True)
    c.setStrokeGray(0); c.setLineWidth(.65); c.setDash(2.4, 1.8)
    for feature in read(BOUNDARIES)["features"]:
        base.stroke_geometry(c, transform(project, shape(feature["geometry"])))
    c.setDash(); c.restoreState()
    for leader in leaders:
        c.setStrokeGray(1); c.setLineWidth(2.1); base.stroke_geometry(c, leader)
        c.setStrokeGray(0); c.setLineWidth(.75); base.stroke_geometry(c, leader)
    for r in references:
        c.setFillGray(1)
        x0,y0,x1,y1 = r["bounds"]
        c.rect(x0,y0,x1-x0,y1-y0,fill=1,stroke=0)
        base.text(c,r["tx"],r["ty"],r["label"],r["size"],font=r["font"])
        c.setFillGray(0)
        if r["airport"]:
            c.rect(r["x"]-.5*mm,r["y"]-.5*mm,mm,mm,fill=1,stroke=0)
        else:
            c.circle(r["x"],r["y"],.35*mm,fill=1,stroke=0)
    for record, point in zip(checks, badges):
        c.setFillGray(1); c.setStrokeGray(0); c.setLineWidth(.9)
        c.circle(point.x,point.y,BADGE_RADIUS,fill=1,stroke=1)
        base.text(c,point.x,point.y-11.3*.718/2,record["sector_id"],11.3,font="Helvetica-Bold",align="center")
    nx,ny=89*mm,178*mm
    c.setStrokeGray(0);c.setLineWidth(.7)
    c.line(nx,ny-5*mm,nx,ny)
    c.line(nx,ny,nx-mm,ny-1.8*mm);c.line(nx,ny,nx+mm,ny-1.8*mm)
    base.text(c,nx,ny+1.5*mm,"N",7.5,font="Helvetica-Bold",align="center")
    return checks, references, rectangle


def is_historical(metadata, generated_at):
    generated = datetime.fromisoformat(generated_at)
    if generated.tzinfo is None:
        raise ValueError("Image generation time must include its UTC offset.")
    return metadata["target_date"] < generated.astimezone(JST).date().isoformat()


def draw_header(c, metadata, generated_at):
    date = metadata["target_date"].replace("-", ".")
    valid = f"{date}  {metadata['valid_time_jst']} JST"
    base.text(c,8*mm,198.5*mm,"九州 NAV LOG",14)
    if is_historical(metadata, generated_at):
        base.text(c,47.8*mm,198.8*mm,"過去予報",7.9)
    base.text(c,289*mm,198*mm,valid,18,font="Helvetica-Bold",align="right")
    base.text(c,8*mm,191.8*mm,"SR22 65% BP / フェアリング有 / A/C OFF / MSL=PA",7.3)
    model = metadata.get("model", "MSM")
    base.text(c,112*mm,191.8*mm,model+"  初期 "+base.timestamp(metadata.get("initial_time_jst")),7.3)
    base.text(c,289*mm,191.8*mm,"生成 "+base.timestamp(generated_at),7.3,align="right")
    c.setStrokeGray(0);c.setLineWidth(.8)
    c.line(8*mm,188.7*mm,289*mm,188.7*mm)
    return valid


def render(forecast, shapes, refs, *, output_dir=OUTPUT, weather_path=WEATHER,
           generated_at=None, qa_dir=None):
    generated_at = generated_at or datetime.now(JST).replace(microsecond=0).isoformat()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    m=forecast["metadata"]; date=m["target_date"]
    stem=f"kyushu_pseudo_{m['valid_time_jst'].replace(':','')}_{date.replace('-','')}"
    path=output_dir/f"{stem}.pdf"
    c=canvas.Canvas(str(path),pagesize=(base.W,base.H))
    c.setTitle(f"九州NAV LOG {date} {m['valid_time_jst']} JST")
    period = "Historical forecast" if is_historical(m, generated_at) else "Forecast"
    c.setSubject(f"{period}, {m.get('model', 'MSM')}, fixed representative-leg means, SR22 65% BP. Coastline: Natural Earth public domain.")
    header=draw_header(c,m,generated_at)
    slots=table_slots()
    checks,references,rectangle=draw_map(c,shapes,slots,refs)
    connections={r["sector_id"]:r for r in checks}
    groups={g["id"]:g for g in forecast["groups"]}
    rendered={gid:draw_card(c,gid,groups[gid],slots[gid],connections[gid]) for gid in IDS}
    c.showPage();c.save()
    png=output_dir/f"{stem}.png"
    with fitz.open(path) as doc:
        assert len(doc)==1
        p=doc[0]
        pix=p.get_pixmap(matrix=fitz.Matrix(300/72,300/72),colorspace=fitz.csGRAY,alpha=False)
        pix.set_dpi(300,300);pix.save(png)
        if qa_dir is not None:
            Path(qa_dir).mkdir(parents=True, exist_ok=True)
            p.get_pixmap(matrix=fitz.Matrix(150/72,150/72),colorspace=fitz.csGRAY,alpha=False).save(Path(qa_dir)/f"{stem}_qa.png")
        page_mm=[p.rect.width/mm,p.rect.height/mm]
        extracted=p.get_text()
        assert all(str(h) in extracted for h in base.ALTITUDES)
        assert "×" not in extracted and "代表レグ平均" not in extracted and "中点" not in extracted
        assert extracted.count("US gal/min")==10
        assert header in extracted
    lines=[LineString(r["leader_page_mm"]) for r in checks]
    crossings=sum(lines[i].crosses(lines[j]) for i in range(len(lines)) for j in range(i))
    assert crossings==0
    rects={gid:box(x/mm,y/mm,(x+CARD_W)/mm,(y+CARD_H)/mm) for gid,(x,y) in slots.items()}
    intrusions=[(r["sector_id"],gid) for r,line in zip(checks,lines) for gid,rect in rects.items()
                if gid!=r["sector_id"] and line.intersection(rect).length>1e-6]
    assert not intrusions
    return {"pdf":str(path),"png":str(png),"a4_mm":page_mm,"image_pixels":[pix.width,pix.height],
      "image_dpi":300,"image_color":"grayscale","tables":10,"weather_rows":90,
      "table_mm":[CARD_W/mm,CARD_H/mm],"table_row_height_mm":(CARD_H-HEADER_H)/9/mm,
      "weather_number_font_pt":BODY_PT,"wind_font_pt":WIND_PT,"altitudes_descending":base.ALTITUDES,
      "columns":["ALT ft MSL","OAT °C","CAS kt","TAS kt","WIND °T/kt","FUEL US gal/min"],
      "fuel_display_decimals":3,"rendered_rows":rendered,"main_header":header,
      "numeric_fonts_embedded":True,"numeric_font_family":"Liberation Sans",
      "generated_time_jst":generated_at,"target_date":date,"forecast_time_jst":m["valid_time_jst"],
      "historical_label":is_historical(m,generated_at),"source_model_display":m.get("model","MSM"),
      "source_weather_metadata":m,"map_connections":checks,"leader_crossing_count":int(crossings),
      "other_table_intrusions":intrusions,"representative_midpoint_marks":[],"representative_midpoint_mark_count":0,
      "footer_explanatory_lines":0,"repeated_table_titles":0,"routes_drawn":0,"leg_lookup_entries":0,
      "hierarchical_regions":False,"physical_print_tested":False,
      "spatial_lookup":"Same fixed midpoint lookup and representative values; presentation revision only.",
      "spread_rows":sum(r.get("spread_flag",False) for g in groups.values() for r in g["rows"]),
      "substitution_rows":sum(r.get("substitution_flag",False) for g in groups.values() for r in g["rows"]),
      "partial_rows":sum(r.get("partial_flag",False) for g in groups.values() for r in g["rows"]),
      "inputs_sha256":{str(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p):hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in [Path(weather_path).resolve(),GEOMETRY,BOUNDARIES,SELECTION]}}


def main():
    OUTPUT.mkdir(exist_ok=True)
    shapes,forecasts=inputs()
    refs=read(ROOT/"route_reference_draft.json")
    generated_at=datetime.now(JST).replace(microsecond=0).isoformat()
    reports=[render(f,shapes,refs,generated_at=generated_at,qa_dir=HERE) for f in forecasts]
    dates=sorted({f["metadata"]["target_date"].replace("-", "") for f in forecasts})
    combined=OUTPUT/f"kyushu_pseudo_weather_{'_'.join(dates)}.pdf"
    with fitz.open() as doc:
        for r in reports:
            with fitz.open(r["pdf"]) as single:doc.insert_pdf(single)
        doc.save(combined)
    (HERE/"layout_checks.json").write_text(json.dumps({"schema":"pseudo-print-layout-2","forecasts":reports,"combined_pdf":str(combined)},ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"combined_pdf":str(combined),"pages":[{k:r[k] for k in ['png','tables','weather_number_font_pt','leader_crossing_count','representative_midpoint_mark_count','spread_rows','substitution_rows']} for r in reports]},ensure_ascii=False))


if __name__=="__main__":main()
