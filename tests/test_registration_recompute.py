from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TEST_DIR = tempfile.mkdtemp(prefix="meter-recon-register-test-")
os.environ["DB_PATH"] = str(Path(_TEST_DIR) / "test-ledger.db")
os.environ["SEED_DEMO_TREE"] = "false"
os.environ["ALLOW_RESET"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db, reset_database_file  # noqa: E402
from app.main import app  # noqa: E402

HOUR = "2026-01-01T00:00:00Z"
OTHER_HOUR = "2026-01-01T01:00:00Z"


@pytest.fixture()
def client():
    init_db()
    reset_database_file()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def register(client: TestClient, meters: list[dict]) -> None:
    response = client.post("/meters", json=meters)
    assert response.status_code == 200, response.text


def report(client: TestClient, meter_id: str, amount: int, hour: str = HOUR) -> None:
    response = client.post("/readings", json={"meter_id": meter_id, "hour": hour, "amount": amount})
    assert response.status_code == 200, response.text


def conclusion(client: TestClient, meter_id: str, hour: str = HOUR) -> dict:
    return client.get(f"/reconciliations/{meter_id}", params={"hour": hour}).json()


def test_adding_children_under_reported_leaf_creates_its_conclusion(client: TestClient):
    register(client, [
        {"meter_id": "P"},
        {"meter_id": "A", "parent_id": "P"},
        {"meter_id": "A1", "parent_id": "A"},
        {"meter_id": "B", "parent_id": "P"},
    ])
    report(client, "A", 300)
    report(client, "B", 700)
    report(client, "P", 1000)

    before_p = conclusion(client, "P")
    assert before_p["used_meter_ids"] == ["A", "B"]
    assert before_p["complete"] is True
    assert before_p["difference"] == 0
    # B is a leaf, so it has no conclusion yet.
    assert client.get("/reconciliations/B", params={"hour": HOUR}).status_code == 404

    register(client, [
        {"meter_id": "B1", "parent_id": "B"},
        {"meter_id": "B2", "parent_id": "B"},
    ])

    b = conclusion(client, "B")
    assert b["own_reading"] == 700
    assert b["complete"] is False
    assert b["missing_meter_ids"] == ["B1", "B2"]
    assert b["difference"] is None

    # P still uses B's report, which covers the new branch: nothing churns.
    after_p = conclusion(client, "P")
    assert after_p["used_meter_ids"] == ["A", "B"]
    assert after_p["difference"] == 0
    assert after_p["version"] == before_p["version"]
    assert after_p["computed_at"] == before_p["computed_at"]


def test_missing_list_is_replaced_with_new_leaf_ids(client: TestClient):
    register(client, [
        {"meter_id": "P"},
        {"meter_id": "A", "parent_id": "P"},
        {"meter_id": "A1", "parent_id": "A"},
        {"meter_id": "B", "parent_id": "P"},
    ])
    report(client, "A", 300)
    report(client, "P", 1000)

    before = conclusion(client, "P")
    assert before["complete"] is False
    assert before["missing_meter_ids"] == ["B"]

    register(client, [
        {"meter_id": "B1", "parent_id": "B"},
        {"meter_id": "B2", "parent_id": "B"},
    ])

    after = conclusion(client, "P")
    assert after["complete"] is False
    assert after["missing_meter_ids"] == ["B1", "B2"]
    assert after["version"] == before["version"] + 1


def test_middle_report_covers_new_branches_keeps_ancestor_untouched(client: TestClient):
    register(client, [
        {"meter_id": "P"},
        {"meter_id": "M", "parent_id": "P"},
        {"meter_id": "L", "parent_id": "M"},
    ])
    report(client, "M", 500)
    report(client, "M", 550, hour=OTHER_HOUR)
    report(client, "P", 900)
    report(client, "P", 910, hour=OTHER_HOUR)

    before = conclusion(client, "P")
    before_other = conclusion(client, "P", hour=OTHER_HOUR)
    assert before["used_meter_ids"] == ["M"]

    register(client, [
        {"meter_id": "L1", "parent_id": "L"},
        {"meter_id": "L2", "parent_id": "L"},
    ])

    after = conclusion(client, "P")
    assert after["used_meter_ids"] == ["M"]
    assert after["version"] == before["version"]
    assert after["computed_at"] == before["computed_at"]

    after_other = conclusion(client, "P", hour=OTHER_HOUR)
    assert after_other["version"] == before_other["version"]
    assert after_other["computed_at"] == before_other["computed_at"]

    # Hours without the former leaf's own report do not appear as conclusions.
    assert client.get("/reconciliations/L", params={"hour": HOUR}).status_code == 404


def test_former_leaf_conclusion_added_for_every_reported_hour(client: TestClient):
    register(client, [
        {"meter_id": "P"},
        {"meter_id": "B", "parent_id": "P"},
    ])
    report(client, "P", 1000)
    report(client, "P", 1100, hour=OTHER_HOUR)
    report(client, "B", 700)
    report(client, "B", 800, hour=OTHER_HOUR)

    register(client, [{"meter_id": "B1", "parent_id": "B"}])

    for hour, own in ((HOUR, 700), (OTHER_HOUR, 800)):
        b = conclusion(client, "B", hour=hour)
        assert b["own_reading"] == own
        assert b["missing_meter_ids"] == ["B1"]
        assert b["complete"] is False

    # The hour with no B report has no B conclusion.
    assert client.get("/reconciliations/B", params={"hour": "2026-01-01T02:00:00Z"}).status_code == 404


def test_side_branch_conclusions_are_not_touched(client: TestClient):
    register(client, [
        {"meter_id": "P"},
        {"meter_id": "A", "parent_id": "P"},
        {"meter_id": "A1", "parent_id": "A"},
        {"meter_id": "B", "parent_id": "P"},
        {"meter_id": "B1", "parent_id": "B"},
    ])
    for mid, amount in (("A1", 100), ("A", 100), ("B1", 400), ("B", 400), ("P", 500)):
        report(client, mid, amount)

    before_a = conclusion(client, "A")
    before_b = conclusion(client, "B")
    assert before_a["complete"] is True
    assert before_b["complete"] is True

    register(client, [{"meter_id": "A2", "parent_id": "A"}])

    after_a = conclusion(client, "A")
    after_b = conclusion(client, "B")
    assert after_b == before_b
    assert after_a["missing_meter_ids"] == ["A2"]
    assert after_a["version"] == before_a["version"] + 1


def test_failed_registration_keeps_old_ledger(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    register(client, [
        {"meter_id": "P"},
        {"meter_id": "B", "parent_id": "P"},
    ])
    report(client, "P", 1000)
    report(client, "B", 700)
    before_p = conclusion(client, "P")

    import app.service as service

    def fail_recompute(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "recompute_one", fail_recompute)
    response = client.post("/meters", json=[{"meter_id": "B1", "parent_id": "B"}])
    assert response.status_code == 500
    monkeypatch.undo()

    # Neither the new meter nor a changed conclusion survived.
    assert client.get("/meters/B1").status_code == 404
    after_p = conclusion(client, "P")
    assert after_p == before_p

    # A valid retry succeeds and applies the expected conclusion.
    register(client, [{"meter_id": "B1", "parent_id": "B"}])
    assert conclusion(client, "B")["missing_meter_ids"] == ["B1"]


def test_registration_response_reports_added_and_changed_meters(client: TestClient):
    register(client, [
        {"meter_id": "P"},
        {"meter_id": "B", "parent_id": "P"},
    ])
    report(client, "P", 1000)
    report(client, "B", 700)

    response = client.post("/meters", json=[{"meter_id": "B1", "parent_id": "B"}])
    assert response.status_code == 200
    body = response.json()
    assert body["added_meter_ids"] == ["B1"]
    assert body["recomputed_meter_ids"] == ["B"]
    assert body["preserved_meter_ids"] == ["P"]
    assert sorted(item["meter_id"] for item in body["conclusions"]) == ["B", "P"]
    assert any(meter["meter_id"] == "B1" for meter in body["meters"])


def test_registration_is_serializable_with_reading_mutations(client: TestClient):
    from concurrent.futures import ThreadPoolExecutor

    register(client, [
        {"meter_id": "P"},
        {"meter_id": "A", "parent_id": "P"},
        {"meter_id": "B", "parent_id": "P"},
    ])
    for mid, amount in (("A", 100), ("B", 200), ("P", 300)):
        report(client, mid, amount)

    from app.service import apply_reading, register_meters

    def add_meter(_: int):
        return register_meters([{"meter_id": "B1", "parent_id": "B"}])

    def correct(_: int):
        return apply_reading(action="correct", meter_id="A", hour_value=HOUR, amount=150)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(add_meter, 0), pool.submit(correct, 0)]
        results = [future.result() for future in futures]
    assert all(results)

    b = conclusion(client, "B")
    assert b["missing_meter_ids"] == ["B1"]
    p = conclusion(client, "P")
    assert p["used_meter_ids"] == ["A", "B"]
    assert p["difference"] == 300 - (150 + 200)
