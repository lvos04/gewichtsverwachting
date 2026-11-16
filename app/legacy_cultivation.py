"""Evidence-only reader for the supplied historical weight/planning workbook.

The source contains repeated 40-bay blocks on `teeltschema`, not explicit
round numbers or calendar years. `data` is a separate lighting source and is
never read by this parser.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

FIELDS = {"bay_number": 1, "plant_week": 2, "plant_weekday": 3,
          "light_out_week": 7, "light_out_weekday": 8,
          "harvest_week": 16, "harvest_weekday": 17,
          "weight_g": 19, "loss_raw": 39, "note": 40,
          "alar_raw": 41, "reaction_raw": 42}
MAX_GROUPS = 100
MAX_ROWS = 2000


def _json(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        return value[:16384]
    return value


def _source(cell, cached, row_number, column_number):
    formula = cell.data_type == "f"
    return {"cell": f"{get_column_letter(column_number)}{row_number}",
            "value": None if formula else _json(cell.value),
            "formula": _json(cell.value) if formula else None,
            "cached": _json(cached.value) if formula else None,
            "confidence": "formula_cache_unverified" if formula else
                          "literal" if cell.value is not None else "missing"}


def _integer(source, minimum, maximum):
    value = source["value"]
    if source["formula"] is not None or isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    if abs(float(value) - round(float(value))) > 1e-8:
        return None
    integer = round(float(value))
    return integer if minimum <= integer <= maximum else None


def parse_weight_workbook(path):
    """Return compact evidence and block boundaries; do not create dates."""
    path = Path(path)
    wb = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    cached = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    try:
        if "teeltschema" not in wb.sheetnames:
            raise ValueError("werkblad teeltschema ontbreekt")
        ws = wb["teeltschema"]
        cs = cached["teeltschema"]
        header = next(ws.iter_rows(min_row=1, max_row=1, max_col=44))
        expected = {1: "kap", 2: "planten", 7: "licht uit", 16: "leeg", 19: "gewicht"}
        if any(str(header[col - 1].value or "").strip().lower() != label for col, label in expected.items()):
            raise ValueError("teeltschema heeft geen herkende kolomkoppen")
        if ws.max_row and ws.max_row > MAX_ROWS:
            raise ValueError("teeltschema heeft te veel rijen")
        groups = []
        current = None
        raw_rows = ws.iter_rows(min_row=2, max_row=min(ws.max_row or MAX_ROWS, MAX_ROWS), max_col=44)
        cached_rows = cs.iter_rows(min_row=2, max_row=min(ws.max_row or MAX_ROWS, MAX_ROWS), max_col=44)
        previous_bay = None
        for row_number, (raw, values) in enumerate(zip(raw_rows, cached_rows), 2):
            bay = raw[0].value
            if isinstance(bay, bool) or not isinstance(bay, (int, float)) or int(bay) != bay or not 1 <= bay <= 40:
                previous_bay = None
                continue
            bay = int(bay)
            if current is None or previous_bay is None or bay <= previous_bay:
                current = {"group_index": len(groups) + 1, "start_row": row_number,
                           "end_row": row_number, "rows": []}
                groups.append(current)
                if len(groups) > MAX_GROUPS:
                    raise ValueError("te veel rondeblokken")
            evidence = {name: _source(raw[col - 1], values[col - 1], row_number, col)
                        for name, col in FIELDS.items()}
            record = {"row_number": row_number, "group_index": current["group_index"],
                      "bay_number": bay,
                      "plant_week": _integer(evidence["plant_week"], 1, 53),
                      "plant_weekday": _integer(evidence["plant_weekday"], 1, 7),
                      "light_out_week": _integer(evidence["light_out_week"], 1, 53),
                      "light_out_weekday": _integer(evidence["light_out_weekday"], 1, 7),
                      "harvest_week": _integer(evidence["harvest_week"], 1, 53),
                      "harvest_weekday": _integer(evidence["harvest_weekday"], 1, 7),
                      "weight_raw": evidence["weight_g"]["value"],
                      "weight_formula": evidence["weight_g"]["formula"],
                      "loss_raw": evidence["loss_raw"]["value"],
                      "alar_raw": evidence["alar_raw"]["value"],
                      "note": evidence["note"]["value"],
                      "source_cells": evidence}
            current["rows"].append(record)
            current["end_row"] = row_number
            previous_bay = bay
        counts = Counter("formula_weight" if row["weight_formula"] else
                         "weight" if row["weight_raw"] not in (None, "") else "no_weight"
                         for group in groups for row in group["rows"])
        return {"groups": groups, "summary": {"groups": len(groups),
                "bay_rows": sum(len(g["rows"]) for g in groups),
                "weight_rows": counts["weight"], "unweighted_rows": counts["no_weight"],
                "formula_weight_rows": counts["formula_weight"],
                "lighting_data_sheet_ignored": "data" in wb.sheetnames,
                "authority": "historical planning/weight evidence only; Priva precedes for actual light and climate"}}
    finally:
        wb.close()
        cached.close()


def match_rounds(groups: list[dict], program_records: list[dict]) -> list[dict]:
    """Match by multiple literal bay/week/day anchors, never a sheet offset."""
    by_round: dict[str, dict[int, list[dict]]] = {}
    for record in program_records:
        by_round.setdefault(record["round_name"], {}).setdefault(record["bay_number"], []).append(record)
    matches = []
    for group in groups:
        scores = []
        for round_name, bays in by_round.items():
            exact = contradictions = anchors = 0
            duplicate_bays = []
            label_conflict = any("round_label_conflict" in record.get("review_reasons", [])
                                 for records in bays.values() for record in records)
            for row in group["rows"]:
                records = bays.get(row["bay_number"], [])
                if len(records) > 1:
                    duplicate_bays.append(row["bay_number"])
                    continue
                if not records or row["plant_week"] is None or row["plant_weekday"] is None:
                    continue
                program = records[0]
                if program["plant_week"] is None or program["plant_weekday"] is None:
                    continue
                anchors += 1
                if (row["plant_week"], row["plant_weekday"]) == (program["plant_week"], program["plant_weekday"]):
                    exact += 1
                else:
                    contradictions += 1
            if anchors:
                scores.append({"round_name": round_name, "exact": exact,
                               "contradictions": contradictions, "anchors": anchors,
                               "duplicate_bays": sorted(set(duplicate_bays)),
                               "round_label_conflict": label_conflict})
        scores.sort(key=lambda x: (x["exact"] - x["contradictions"], x["exact"]), reverse=True)
        winner = scores[0] if scores else None
        runner = scores[1] if len(scores) > 1 else None
        confident = bool(winner and not winner["round_label_conflict"] and winner["exact"] >= 3 and
                         winner["exact"] >= 2 * max(1, winner["contradictions"]) and
                         (not runner or winner["exact"] - winner["contradictions"] >=
                          runner["exact"] - runner["contradictions"] + 3))
        matches.append({"group_index": group["group_index"], "start_row": group["start_row"],
                        "end_row": group["end_row"],
                        "matched_round": winner["round_name"] if confident else None,
                        "confidence": "matched_literal_anchors" if confident else
                                      "round_label_conflict" if winner and winner["round_label_conflict"] else "ambiguous",
                        "candidates": scores[:3]})
    return matches


def group_plant_years(group: dict, first_iso_year: int) -> dict[int, int]:
    """Apply an explicitly supplied first-week year across visible week wrap."""
    years = {}
    year = first_iso_year
    previous_week = None
    for row in group["rows"]:
        week = row["plant_week"]
        if week is not None and previous_week is not None and previous_week - week > 20:
            year += 1
        years[row["row_number"]] = year
        if week is not None:
            previous_week = week
    return years


def iso_date(year: int, week: int | None, weekday: int | None) -> date | None:
    if week is None or weekday is None:
        return None
    try:
        return date.fromisocalendar(year, week, weekday)
    except ValueError:
        return None


def next_week_date(start: date | None, week: int | None, weekday: int | None,
                   max_days: int) -> date | None:
    if start is None or week is None or weekday is None:
        return None
    choices = [iso_date(year, week, weekday) for year in
               (start.isocalendar().year, start.isocalendar().year + 1,
                start.isocalendar().year + 2)]
    choices = [day for day in choices if day is not None and start <= day <= start + timedelta(days=max_days)]
    return min(choices) if choices else None
