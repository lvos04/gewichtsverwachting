import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import UploadFile

from app import history_import as hi
from app.legacy_cultivation import group_plant_years, match_rounds, parse_weight_workbook
from app.auth import COOKIE
from app.db import Base, get_db
from app.models import AuthSession, Bay, Cultivation, PlanningVersion, User, Variety


COLS = {"round_name": "A", "bay_number": "B", "variety": "C", "plant_date": "D",
        "light_out_date": "E", "harvest_start_date": "F", "harvest_end_date": "G",
        "weight_g": "H", "density": "I", "beds": "J"}


def workbook(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Historie"
    ws.append(list(COLS))
    for row in rows:
        ws.append(row)
    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def sample_row(round_name, bay, variety, weight="105,5", plant="01-01-2020", beds=6):
    return [round_name, bay, variety, plant, "20-01-2020", "01-03-2020", "02-03-2020",
            weight, "55,5", beds]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(hi, "DATA_DIR", tmp_path)
    monkeypatch.setattr(hi, "ROOT", tmp_path / "history-imports")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all([User(id=1, username="writer", password_hash="x", role="medewerker"),
                    User(id=2, username="reader", password_hash="x", role="lezer")])
        db.add_all([Bay(number=n, department=1, beds=6) for n in range(1, 41)])
        db.add_all([Variety(name=name, target_weight_g=105) for name in ("antonov", "pjotr", "tutu")])
        db.commit()
    app = FastAPI()
    app.include_router(hi.router)
    def sessions():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = sessions
    client = TestClient(app)
    return client, factory, tmp_path


def auth_cookie(factory, role="writer"):
    token = f"token-{role}"
    user_id = 1 if role == "writer" else 2
    with factory() as db:
        db.add(AuthSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user_id,
                           expires_at=hi.utcnow() + timedelta(days=1)))
        db.commit()
    return {COOKIE: token}


def upload(client, cookies, content):
    response = client.post("/api/history-imports", files={"file": ("history.xlsx", content)}, cookies=cookies)
    assert response.status_code == 200, response.text
    return response.json()


def map_file(client, cookies, import_id, *, overrides=None, defaults=None):
    return client.post(f"/api/history-imports/{import_id}/mapping", cookies=cookies,
        json={"sheet": "Historie", "header_row": 1, "column_map": COLS,
              "defaults": defaults or {}, "overrides": overrides or [],
              "date_year_policy": "explicit_only"})


def test_preview_review_confirm_mixed_classes_formula_and_idempotence(setup):
    client, factory, root = setup
    cookies = auth_cookie(factory)
    rows = [sample_row("R1", 1, "a"), sample_row("R2", 2, "3a3p", 100),
            sample_row("R3", 3, "3a3t", 100), sample_row("R4", 4, "a", plant="01-01-20"),
            sample_row("R5", 5, "a", weight="95/65"), sample_row("R6", 6, "a", weight="=90+10"),
            sample_row("R7", 7, "g", 100), sample_row("R8", 8, "", 100)]
    content = workbook(rows)
    created = upload(client, cookies, content)
    assert created["status"] == "uploaded"
    assert created["sheets"][0]["name"] == "Historie"
    sheet_preview = client.get(f"/api/history-imports/{created['id']}/sheet-preview",
                               params={"sheet": "Historie", "header_row": 1}, cookies=cookies)
    assert sheet_preview.status_code == 200
    assert sheet_preview.json()["headers"][0] == {"column": "A", "value": "round_name"}
    with factory() as db:
        assert db.scalars(select(Cultivation)).all() == []
    preview_response = map_file(client, cookies, created["id"])
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()["preview"]
    assert preview["summary"] == {"total_rows": 8, "valid": 3, "ambiguous": 5, "excluded": 0, "duplicate": 0}
    assert any(r["row_number"] == 7 and "formulecel" in r["reasons"][0]
               for r in preview_response.json()["reviews"][0]["reasons"])
    assert client.post(f"/api/history-imports/{created['id']}/confirm", json={"confirmed": True}, cookies=cookies).status_code == 409
    overrides = [{"row_number": n, "exclude": True, "reason": "historische bron niet eenduidig"}
                 for n in (4, 5, 7, 8, 9)]
    reviewed = map_file(client, cookies, created["id"], overrides=overrides)
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["preview"]["summary"]["ambiguous"] == 0
    confirmed = client.post(f"/api/history-imports/{created['id']}/confirm",
                            json={"confirmed": True}, cookies=cookies)
    assert confirmed.status_code == 200, confirmed.text
    assert len(confirmed.json()["created_ids"]) == 4
    assert client.post(f"/api/history-imports/{created['id']}/confirm",
                       json={"confirmed": True}, cookies=cookies).json() == confirmed.json()
    with factory() as db:
        cultivations = db.scalars(select(Cultivation)).all()
        assert len(cultivations) == 4
        assert sorted(c.beds for c in cultivations if c.round_name == "R2") == [3, 3]
        assert {c.label_quality for c in cultivations if c.round_name == "R2"} == {"historical_assigned"}
        from app.models import Harvest
        classes = db.scalar(select(Harvest).join(Cultivation).where(Cultivation.round_name == "R5"))
        assert classes.weight_g is None
        assert [x["weight_g"] for x in classes.quality_classes] == [95, 65]
    assert (root / "history-imports" / f"{hashlib.sha256(content).hexdigest()}.xlsx").read_bytes() == content
    second = upload(client, cookies, content)
    duplicate_preview = map_file(client, cookies, second["id"])
    assert duplicate_preview.json()["preview"]["summary"]["duplicate"] == 3
    assert duplicate_preview.json()["preview"]["summary"]["ambiguous"] == 5


def test_explicit_formula_correction_and_duplicate_guard(setup):
    client, factory, _ = setup
    cookies = auth_cookie(factory)
    content = workbook([sample_row("R1", 1, "a", weight="=90+10")])
    item = upload(client, cookies, content)
    assert map_file(client, cookies, item["id"]).json()["preview"]["summary"]["ambiguous"] == 1
    correction = [{"row_number": 2, "fields": {"weight_g": "100"}, "reason": "gecontroleerd met originele weeglijst"}]
    mapped = map_file(client, cookies, item["id"], overrides=correction)
    assert mapped.json()["preview"]["summary"]["valid"] == 1
    assert client.post(f"/api/history-imports/{item['id']}/confirm", json={"confirmed": True}, cookies=cookies).status_code == 200
    duplicate = upload(client, cookies, content)
    mapped_again = map_file(client, cookies, duplicate["id"], overrides=correction)
    assert mapped_again.json()["preview"]["summary"]["duplicate"] == 1
    result = client.post(f"/api/history-imports/{duplicate['id']}/confirm", json={"confirmed": True}, cookies=cookies)
    assert result.json()["created_ids"] == []
    assert result.json()["skipped_duplicate_rows"] == [2]
    proposed_correction = upload(client, cookies, content)
    conflicting_preview = map_file(client, cookies, proposed_correction["id"], overrides=[
        {"row_number": 2, "fields": {"weight_g": "110"}, "reason": "andere koppeling voorgesteld"}])
    assert conflicting_preview.json()["preview"]["summary"]["ambiguous"] == 1
    assert client.post(f"/api/history-imports/{proposed_correction['id']}/confirm",
                       json={"confirmed": True}, cookies=cookies).status_code == 409
    conflicting = map_file(client, cookies, duplicate["id"], overrides=[
        {"row_number": 2, "fields": {"weight_g": "110"}, "reason": "andere koppeling voorgesteld"}])
    assert conflicting.status_code == 409  # confirmed mapping is immutable


def test_years_roles_and_atomic_confirm_conflict(setup):
    client, factory, _ = setup
    writer = auth_cookie(factory)
    reader = auth_cookie(factory, "reader")
    content = workbook([sample_row("R1", 1, "a"), sample_row("R2", 2, "a")])
    assert client.post("/api/history-imports", files={"file": ("history.xlsx", content)}, cookies=reader).status_code == 403
    item = upload(client, writer, content)
    assert client.get(f"/api/history-imports/{item['id']}", cookies=reader).status_code == 200
    assert map_file(client, reader, item["id"]).status_code == 403
    assert map_file(client, writer, item["id"]).status_code == 200
    with factory() as db:
        antonov = db.scalar(select(Variety).where(Variety.name == "antonov"))
        c = Cultivation(round_name="R2", bay_number=2, variety_id=antonov.id, beds=6,
                        category="standard", status="historical", label_quality="verified")
        db.add(c); db.flush()
        db.add(PlanningVersion(cultivation_id=c.id, plant_date=date(2020, 1, 1),
                               light_out_date=date(2020, 1, 20), author_id=1))
        db.commit()
    response = client.post(f"/api/history-imports/{item['id']}/confirm", json={"confirmed": True}, cookies=writer)
    assert response.status_code == 409
    with factory() as db:
        assert db.scalars(select(Cultivation).where(Cultivation.round_name == "R1")).all() == []
        assert db.scalar(select(hi.HistoryImport).where(hi.HistoryImport.id == item["id"])).status == "preview_ready"


def test_reject_zip_bomb_and_macro_member(tmp_path):
    import zipfile
    path = tmp_path / "bad.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "x")
        archive.writestr("xl/workbook.xml", "x")
        archive.writestr("xl/vbaProject.bin", "macro")
    with pytest.raises(ValueError, match="onveilig"):
        hi._validate_xlsx(path)


def test_whole_cap_stems_and_alar_not_allocated_to_mixed_varieties(setup):
    client, factory, _ = setup
    cookies = auth_cookie(factory)
    wb = Workbook()
    ws = wb.active
    ws.title = "Historie"
    ws.append(list(COLS) + ["planned_stems", "historical_alar_total", "historical_alar_unit"])
    ws.append(sample_row("R1", 1, "3a3p", 100) + [10000, "20,5", "g/1000m2"])
    content = BytesIO(); wb.save(content)
    item = upload(client, cookies, content.getvalue())
    mapping = {**COLS, "planned_stems": "K", "historical_alar_total": "L",
               "historical_alar_unit": "M"}
    preview = client.post(f"/api/history-imports/{item['id']}/mapping", cookies=cookies,
        json={"sheet": "Historie", "header_row": 1, "column_map": mapping,
              "date_year_policy": "explicit_only"})
    assert preview.status_code == 200, preview.text
    assert preview.json()["preview"]["summary"]["valid"] == 1
    confirmed = client.post(f"/api/history-imports/{item['id']}/confirm",
                            json={"confirmed": True}, cookies=cookies)
    assert confirmed.status_code == 200, confirmed.text
    with factory() as db:
        source = db.scalar(select(hi.HistorySourceRow))
        assert source.whole_cap_values["planned_stems"] == 10000
        assert source.whole_cap_values["historical_alar_total"] == 20.5
        assert source.whole_cap_values["historical_alar_unit"] == "g/1000m2"
        assert all(p.planned_stems is None for p in db.scalars(select(PlanningVersion)).all())


def test_review_versions_preserve_previous_corrections(setup):
    client, factory, _ = setup
    cookies = auth_cookie(factory)
    item = upload(client, cookies, workbook([sample_row("R1", 1, "a")]))
    first = map_file(client, cookies, item["id"], overrides=[
        {"row_number": 2, "fields": {"weight_g": "110"}, "reason": "weegbon eerste controle"}])
    assert first.status_code == 200
    second = map_file(client, cookies, item["id"], overrides=[
        {"row_number": 2, "fields": {"weight_g": "120"}, "reason": "weegbon na hercontrole"}])
    assert second.status_code == 200
    detail = client.get(f"/api/history-imports/{item['id']}", cookies=cookies).json()
    assert len(detail["reviews"]) == 2
    assert detail["reviews"][0]["mapping"]["overrides"][0]["reason"] == "weegbon eerste controle"
    assert detail["reviews"][0]["mapping"]["overrides"][0]["fields"]["weight_g"] == "110"
    assert detail["reviews"][1]["mapping"]["overrides"][0]["fields"]["weight_g"] == "120"
    assert detail["preview"]["rows"][0]["effective"]["weight_g"] == "120"


def test_parallel_confirm_never_creates_duplicate_cultivation(tmp_path, monkeypatch):
    monkeypatch.setattr(hi, "DATA_DIR", tmp_path)
    monkeypatch.setattr(hi, "ROOT", tmp_path / "history-imports")
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(User(id=1, username="writer", password_hash="x", role="medewerker"))
        db.add(Bay(number=1, department=1, beds=6))
        db.add(Variety(name="antonov", target_weight_g=105))
        db.commit()
    app = FastAPI()
    app.include_router(hi.router)
    def sessions():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = sessions
    token = "parallel-token"
    with factory() as db:
        db.add(AuthSession(token_hash=hashlib.sha256(token.encode()).hexdigest(),
                           user_id=1, expires_at=hi.utcnow() + timedelta(days=1)))
        db.commit()
    cookies = {COOKIE: token}
    with TestClient(app) as client:
        item = upload(client, cookies, workbook([sample_row("R1", 1, "a")]))
        assert map_file(client, cookies, item["id"]).status_code == 200
    def confirm():
        with TestClient(app) as client:
            return client.post(f"/api/history-imports/{item['id']}/confirm",
                json={"confirmed": True}, cookies=cookies).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: confirm(), range(2)))
    assert 200 in statuses
    assert set(statuses) <= {200, 409}
    with factory() as db:
        assert len(db.scalars(select(Cultivation)).all()) == 1
        assert len(db.scalars(select(hi.HistorySourceRow)).all()) == 1


def test_reject_duplicate_archive_paths_and_xml_entities(tmp_path):
    import zipfile
    duplicate = tmp_path / "duplicate.xlsx"
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("[Content_Types].xml", "x")
        archive.writestr("xl/workbook.xml", "x")
        archive.writestr("xl/workbook.xml", "y")
    with pytest.raises(ValueError, match="dubbele"):
        hi._validate_xlsx(duplicate)
    entity = tmp_path / "entity.xlsx"
    with zipfile.ZipFile(entity, "w") as archive:
        archive.writestr("[Content_Types].xml", "x")
        archive.writestr("xl/workbook.xml", '<!DOCTYPE x [<!ENTITY e "boom">]><x>&e;</x>')
    with pytest.raises(ValueError, match="XML-entiteiten"):
        hi._validate_xlsx(entity)


def test_override_size_is_bounded():
    with pytest.raises(ValueError):
        hi.RowOverride(row_number=2, fields={"weight_g": "1" * 2001}, reason="check")


def paired_legacy_workbooks(*, mismatch=False, extra=False):
    """Small structural analogues; never copy a real farm row into CI."""
    weight = Workbook()
    ws = weight.active
    ws.title = "teeltschema"
    for col, value in {"A": "kap", "B": "planten", "G": "licht uit", "P": "leeg",
                       "S": "gewicht"}.items():
        ws[f"{col}1"] = value
    for row, bay, week, weekday, species_weight in (
            [(2, 1, 31, 1, 101), (3, 2, 31, 3, "95/65"), (4, 3, 32, 1, None)] +
            ([(5, 4, 33, 2, 104)] if mismatch or extra else [])):
        for col, value in {"A": bay, "B": week, "C": weekday,
                           "G": week + 2, "H": weekday, "P": 42,
                           "Q": weekday, "S": species_weight}.items():
            ws[f"{col}{row}"] = value
        if bay == 3:
            ws[f"AO{row}"] = 2000
    weight.create_sheet("data")
    out = BytesIO(); weight.save(out)
    weight_bytes = out.getvalue()
    program = Workbook()
    program.remove(program.active)
    for number in range(3, 8):
        ws = program.create_sheet(f"Ronde {number}")
        for cell, value in {"C8": f"Ronde {number}", "D10": "schema", "E10": "pl datum",
                            "H10": "licht uit", "N10": "bloei", "C11": "vak", "E11": "w/d"}.items():
            ws[cell] = value
        for row, bay, week, weekday, schema in (
                [(12, 1, 31, 1, "a"), (13, 2, 31, 3, "3a3p"), (14, 3, 32, 1, "p")] +
                ([(15, 4, 40 if mismatch and number == 7 else 33, 2, "a")]
                 if mismatch or extra else [])):
            ws[f"C{row}"] = bay
            ws[f"D{row}"] = schema
            ws[f"E{row}"] = week if number == 7 else week - (7 - number) * 3
            ws[f"F{row}"] = weekday
            ws[f"G{row}"] = 14
            ws[f"M{row}"] = 55
    out = BytesIO(); program.save(out)
    return weight_bytes, out.getvalue()


def test_actual_profile_pair_requires_year_and_preserves_planning_without_harvest(setup):
    client, factory, _ = setup
    cookies = auth_cookie(factory)
    weights, program = paired_legacy_workbooks()
    w = upload(client, cookies, weights)
    p = upload(client, cookies, program)
    assert w["profile"] == "legacy_weight_workbook"
    assert p["profile"] == "legacy_program_workbook"
    url = f"/api/history-imports/{w['id']}/legacy-preview"
    body = {"program_import_id": p["id"], "round_years": {},
            "weekday_convention": "iso_monday_1", "single_species_beds": 6,
            "include_unweighted_planning": True}
    no_year = client.post(url, json=body, cookies=cookies)
    assert no_year.status_code == 200, no_year.text
    assert no_year.json()["legacy_profile"]["matches"][0]["matched_round"] == "Ronde 7"
    assert no_year.json()["preview"]["summary"]["ambiguous"] == 3
    assert client.post(f"/api/history-imports/{w['id']}/confirm",
                       json={"confirmed": True}, cookies=cookies).status_code == 409
    body["round_years"] = {"Ronde 7": 2024}
    reviewed = client.post(url, json=body, cookies=cookies)
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["preview"]["summary"]["valid"] == 3
    result = client.post(f"/api/history-imports/{w['id']}/confirm",
                         json={"confirmed": True}, cookies=cookies)
    assert result.status_code == 200, result.text
    assert len(result.json()["created_ids"]) == 4
    from app.models import Harvest
    with factory() as db:
        cs = db.scalars(select(Cultivation)).all()
        assert len(cs) == 4
        assert {c.label_quality for c in cs if c.bay_number == 2} == {"historical_assigned"}
        assert db.scalar(select(Harvest).join(Cultivation).where(Cultivation.bay_number == 3)) is None
        assert all(c.label_quality == "unknown" for c in cs if c.bay_number == 3)
        assert len(db.scalars(select(Harvest)).all()) == 3
        source = db.scalar(select(hi.HistorySourceRow).where(hi.HistorySourceRow.row_number == 4))
        assert source.whole_cap_values["source_alar_raw"] == 2000
        assert source.whole_cap_values.get("historical_alar_unit") is None
        assert db.scalar(select(hi.HistoryImport).where(hi.HistoryImport.id == w["id"])).mapping["profile"] == "legacy_paired"


def test_program_only_planning_can_be_explicitly_included_without_harvest(setup):
    client, factory, _ = setup
    cookies = auth_cookie(factory)
    weights, program = paired_legacy_workbooks()
    w = upload(client, cookies, weights)
    p = upload(client, cookies, program)
    response = client.post(f"/api/history-imports/{w['id']}/legacy-preview", cookies=cookies,
        json={"program_import_id": p["id"],
              "round_years": {f"Ronde {i}": 2024 for i in range(3, 8)},
              "weekday_convention": "iso_monday_1", "single_species_beds": 6,
              "include_unweighted_planning": True, "include_program_only_planning": True})
    assert response.status_code == 200, response.text
    assert response.json()["preview"]["summary"]["valid"] == 15
    assert response.json()["preview"]["summary"]["program_only_available"] == 12
    confirmed = client.post(f"/api/history-imports/{w['id']}/confirm",
                            json={"confirmed": True}, cookies=cookies)
    assert confirmed.status_code == 200, confirmed.text
    from app.models import Harvest
    with factory() as db:
        assert len(db.scalars(select(Cultivation)).all()) == 20
        assert len(db.scalars(select(Harvest)).all()) == 3
        assert all(c.label_quality == "unknown" for c in db.scalars(select(Cultivation)).all()
                   if c.round_name != "Ronde 7")


def test_confirmed_round_year_wrap_is_explicit_and_local():
    group = {"rows": [{"row_number": 2, "plant_week": 52},
                      {"row_number": 3, "plant_week": 1},
                      {"row_number": 4, "plant_week": 2}]}
    assert group_plant_years(group, 2024) == {2: 2024, 3: 2025, 4: 2025}


def test_conflicting_literal_anchor_needs_explicit_review(setup):
    client, factory, _ = setup
    cookies = auth_cookie(factory)
    weights, program = paired_legacy_workbooks(mismatch=True)
    w = upload(client, cookies, weights)
    p = upload(client, cookies, program)
    body = {"program_import_id": p["id"], "round_years": {"Ronde 7": 2024},
            "weekday_convention": "iso_monday_1", "single_species_beds": 6,
            "include_unweighted_planning": True}
    url = f"/api/history-imports/{w['id']}/legacy-preview"
    preview = client.post(url, json=body, cookies=cookies)
    assert preview.status_code == 200, preview.text
    row = next(x for x in preview.json()["preview"]["rows"] if x["row_number"] == 5)
    assert row["original"]["anchor_mismatch"] is True
    assert row["status"] == "ambiguous"
    body["overrides"] = [{"row_number": 5, "accept_anchor_mismatch": True,
                          "reason": "afwijkende registratie handmatig gecontroleerd"}]
    reviewed = client.post(url, json=body, cookies=cookies)
    row = next(x for x in reviewed.json()["preview"]["rows"] if x["row_number"] == 5)
    assert row["status"] == "valid"
    assert "handmatig geaccepteerd" in row["reasons"][0]


def test_paired_formula_weight_never_becomes_blank_planning_or_cached_fact(setup):
    from openpyxl import load_workbook
    client, factory, _ = setup
    cookies = auth_cookie(factory)
    weights, program = paired_legacy_workbooks()
    wb = load_workbook(BytesIO(weights))
    wb["teeltschema"]["S2"] = "=100+1"
    output = BytesIO(); wb.save(output)
    w = upload(client, cookies, output.getvalue())
    p = upload(client, cookies, program)
    url = f"/api/history-imports/{w['id']}/legacy-preview"
    body = {"program_import_id": p["id"], "round_years": {"Ronde 7": 2024},
            "weekday_convention": "iso_monday_1", "single_species_beds": 6}
    preview = client.post(url, json=body, cookies=cookies)
    assert preview.status_code == 200, preview.text
    assert preview.json()["legacy_profile"]["weight_summary"]["formula_weight_rows"] == 1
    row = next(x for x in preview.json()["preview"]["rows"] if x["row_number"] == 2)
    assert row["status"] == "ambiguous"
    assert "formule" in row["reasons"][0]
    body["overrides"] = [{"row_number": 2, "fields": {"weight_g": "101"},
                          "reason": "eindgewicht nagewogen op papieren lijst"}]
    corrected = client.post(url, json=body, cookies=cookies)
    row = next(x for x in corrected.json()["preview"]["rows"] if x["row_number"] == 2)
    assert row["status"] == "valid"
    assert "handmatig vervangen" in row["reasons"][0]


def test_duplicate_program_bay_preserves_candidates_and_requires_selected_source_row(setup):
    from openpyxl import load_workbook
    client, factory, _ = setup
    cookies = auth_cookie(factory)
    weights, program = paired_legacy_workbooks(extra=True)
    wb = load_workbook(BytesIO(program))
    ws = wb["Ronde 7"]
    for cell, value in {"C16": 1, "D16": "6t", "E16": 31, "F16": 1,
                        "G16": 14, "M16": 55}.items():
        ws[cell] = value
    output = BytesIO(); wb.save(output)
    w = upload(client, cookies, weights)
    p = upload(client, cookies, output.getvalue())
    url = f"/api/history-imports/{w['id']}/legacy-preview"
    body = {"program_import_id": p["id"], "round_years": {"Ronde 7": 2024},
            "weekday_convention": "iso_monday_1", "single_species_beds": 6,
            "include_unweighted_planning": True}
    preview = client.post(url, json=body, cookies=cookies)
    assert preview.status_code == 200, preview.text
    row = next(x for x in preview.json()["preview"]["rows"] if x["row_number"] == 2)
    assert preview.json()["legacy_profile"]["matches"][0]["matched_round"] == "Ronde 7"
    assert row["status"] == "ambiguous"
    assert row["original"]["duplicate_program_bay"] is True
    assert {c["row_number"] for c in row["original"]["program_candidates"]} == {12, 16}
    body["program_record_selection"] = {"Ronde 7:1": 12}
    assert client.post(url, json=body, cookies=cookies).status_code == 422
    body["program_record_selection_reason"] = "bronrij 12 met afzonderlijke teeltkaart gecontroleerd"
    reviewed = client.post(url, json=body, cookies=cookies)
    assert reviewed.status_code == 200, reviewed.text
    row = next(x for x in reviewed.json()["preview"]["rows"] if x["row_number"] == 2)
    assert row["status"] == "valid"
    assert row["source"]["program_record_row_number"] == 12


def test_conflicting_program_round_title_blocks_auto_match_until_reasoned_selection(setup):
    from openpyxl import load_workbook
    client, factory, _ = setup
    cookies = auth_cookie(factory)
    weights, program = paired_legacy_workbooks()
    wb = load_workbook(BytesIO(program))
    wb["Ronde 7"]["C8"] = "Ronde 99"
    output = BytesIO(); wb.save(output)
    w = upload(client, cookies, weights)
    p = upload(client, cookies, output.getvalue())
    url = f"/api/history-imports/{w['id']}/legacy-preview"
    body = {"program_import_id": p["id"], "round_years": {"Ronde 7": 2024},
            "weekday_convention": "iso_monday_1", "single_species_beds": 6,
            "include_unweighted_planning": True}
    preview = client.post(url, json=body, cookies=cookies)
    assert preview.status_code == 200, preview.text
    match = preview.json()["legacy_profile"]["matches"][0]
    assert match["matched_round"] is None
    assert match["confidence"] == "round_label_conflict"
    body["group_rounds"] = {"1": "Ronde 7"}
    assert client.post(url, json=body, cookies=cookies).status_code == 422
    body["group_round_reason"] = "werkbladnaam met oorspronkelijke rondelijst vergeleken"
    reviewed = client.post(url, json=body, cookies=cookies)
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["preview"]["summary"]["valid"] == 3
    assert "handmatig gekoppeld" in reviewed.json()["preview"]["rows"][0]["reasons"][0]


def test_planned_legacy_confirmation_rechecks_active_variety_and_rolls_back(setup):
    client, factory, _ = setup
    cookies = auth_cookie(factory)
    weights, program = paired_legacy_workbooks()
    w = upload(client, cookies, weights)
    p = upload(client, cookies, program)
    body = {"program_import_id": p["id"], "round_years": {"Ronde 7": 2024},
            "weekday_convention": "iso_monday_1", "single_species_beds": 6,
            "include_unweighted_planning": True, "planning_status": "planned"}
    url = f"/api/history-imports/{w['id']}/legacy-preview"
    preview = client.post(url, json=body, cookies=cookies)
    assert preview.status_code == 200, preview.text
    assert preview.json()["preview"]["summary"]["valid"] == 3
    with factory() as db:
        pjotr = db.scalar(select(Variety).where(Variety.name == "pjotr"))
        pjotr.active = False
        db.commit()
    confirmed = client.post(f"/api/history-imports/{w['id']}/confirm",
                            json={"confirmed": True}, cookies=cookies)
    assert confirmed.status_code == 409, confirmed.text
    with factory() as db:
        assert db.scalars(select(Cultivation)).all() == []


def test_planned_legacy_confirmation_rechecks_existing_bed_capacity(setup):
    client, factory, _ = setup
    cookies = auth_cookie(factory)
    weights, program = paired_legacy_workbooks()
    w = upload(client, cookies, weights)
    p = upload(client, cookies, program)
    body = {"program_import_id": p["id"], "round_years": {"Ronde 7": 2024},
            "weekday_convention": "iso_monday_1", "single_species_beds": 6,
            "include_unweighted_planning": True, "planning_status": "planned"}
    preview = client.post(f"/api/history-imports/{w['id']}/legacy-preview",
                          json=body, cookies=cookies)
    assert preview.status_code == 200, preview.text
    with factory() as db:
        antonov = db.scalar(select(Variety).where(Variety.name == "antonov"))
        db.add(Cultivation(round_name="Ronde 7", bay_number=3, variety_id=antonov.id,
                           beds=4, category="standard", status="planned", label_quality="unknown"))
        db.commit()
    confirmed = client.post(f"/api/history-imports/{w['id']}/confirm",
                            json={"confirmed": True}, cookies=cookies)
    assert confirmed.status_code == 409, confirmed.text
    with factory() as db:
        assert len(db.scalars(select(Cultivation)).all()) == 1


def test_antonov_tutu_triple_mix_cannot_receive_a_shared_weight(setup):
    client, factory, _ = setup
    cookies = auth_cookie(factory)
    imported = upload(client, cookies, workbook([sample_row("R1", 1, "2a2t2p")]))
    preview = map_file(client, cookies, imported["id"])
    assert preview.status_code == 200, preview.text
    row = preview.json()["rows"][0] if "rows" in preview.json() else preview.json()["preview"]["rows"][0]
    assert row["status"] == "ambiguous"
    assert "antonov/tutu" in row["reasons"][0]


def test_future_planned_weight_is_not_a_historical_harvest(setup):
    client, factory, _ = setup
    cookies = auth_cookie(factory)
    row = sample_row('future-round', 1, 'a', plant='01-01-2099')
    row[4:7] = ['20-01-2099', '01-03-2099', '02-03-2099']
    imported = upload(client, cookies, workbook([row]))
    preview = map_file(client, cookies, imported['id'])
    assert preview.status_code == 200, preview.text
    assert preview.json()['summary']['ambiguous'] == 1
    confirmation = client.post(f"/api/history-imports/{imported['id']}/confirm", cookies=cookies,
                               json={'confirmed': True})
    assert confirmation.status_code == 409
    with factory() as db:
        assert db.scalars(select(Cultivation)).all() == []


def test_explicit_configured_variety_name_supports_later_extensions(setup):
    client, factory, _ = setup
    cookies = auth_cookie(factory)
    with factory() as db:
        variety = Variety(name='new flower', target_weight_g=110, active=False)
        db.add(variety); db.commit(); variety_id=variety.id
    imported=upload(client,cookies,workbook([sample_row('new-historical',1,'New Flower')]))
    preview=map_file(client,cookies,imported['id'])
    assert preview.status_code==200 and preview.json()['summary']['valid']==1
    confirmation=client.post(f"/api/history-imports/{imported['id']}/confirm",cookies=cookies,
                             json={'confirmed':True})
    assert confirmation.status_code==200,confirmation.text
    with factory() as db:
        crop=db.scalar(select(Cultivation))
        assert crop.variety_id==variety_id and crop.status=='historical'
        assert db.get(Variety,variety_id).active is False


def test_encoded_xml_entities_are_rejected_by_safe_workbook_parser(setup):
    import zipfile
    client,factory,_=setup
    cookies=auth_cookie(factory)
    original=workbook([sample_row('encoded-xml',1,'a')])
    output=BytesIO()
    with zipfile.ZipFile(BytesIO(original)) as source, zipfile.ZipFile(output,'w') as destination:
        for item in source.infolist():
            blob=source.read(item.filename)
            if item.filename=='xl/workbook.xml':
                text=blob.decode('utf-8').replace('name="Historie"','name="&injected;"')
                blob=('<?xml version="1.0" encoding="UTF-16"?>\n'
                      '<!DOCTYPE workbook [<!ENTITY injected "synthetic">]>\n'+text).encode('utf-16')
            destination.writestr(item,blob)
    response=client.post('/api/history-imports',files={'file':('encoded.xlsx',output.getvalue())},cookies=cookies)
    assert response.status_code==422,response.text
    with factory() as db:
        assert db.scalars(select(hi.HistoryImport)).all()==[]
