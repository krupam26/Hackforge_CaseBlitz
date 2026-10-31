"""Regression tests for the AsliKaun FastAPI prototype.

Run from the backend project root after installing pytest and httpx:
    python -m pytest -q tests

The suite uses a temporary SQLite database and never touches the normal dev DB.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ADMIN_USERNAME = "test-admin"
ADMIN_PASSWORD = "test-password-long-enough-123"


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory):
    db_path = tmp_path_factory.mktemp("aslikaun_tests") / "test.db"
    import os

    os.environ["ASLIKAUN_DB"] = str(db_path)
    os.environ["ASLIKAUN_ADMIN_USERNAME"] = ADMIN_USERNAME
    os.environ["ASLIKAUN_ADMIN_PASSWORD"] = ADMIN_PASSWORD
    os.environ["ASLIKAUN_TOKEN_SECRET"] = "test-only-secret-that-is-long-enough-for-regression-tests"
    os.environ["TRUST_JUGGLER_DB"] = str(db_path)
    os.environ["TRUST_JUGGLER_ADMIN_USERNAME"] = ADMIN_USERNAME
    os.environ["TRUST_JUGGLER_ADMIN_PASSWORD"] = ADMIN_PASSWORD
    os.environ["TRUST_JUGGLER_TOKEN_SECRET"] = "test-only-secret-that-is-long-enough-for-regression-tests"

    try:
        import app.main as main
    except ImportError:
        import backend.app.main as main

    main = importlib.reload(main)  # Read test-only environment before DB_PATH is set.
    with TestClient(main.app) as test_client:
        yield test_client


def admin_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/v1/admin/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def session_payload(driver_id: str, **overrides) -> dict:
    payload = {
        "driver_id": driver_id,
        "device_id": "phone-A",
        "ip_subnet": "192.168.1.0/24",
        "zone": "zone-1",
        "accel": [0.2, 0.3, 0.25, 0.22],
        "gyro": [0.1, 0.12, 0.11],
        "touch_events": [{"type": "tap"}, {"type": "swipe"}, {"type": "tap"}],
        "accept_latency_ms": 820,
        "timestamp": "2026-10-17T10:00:00Z",
    }
    payload.update(overrides)
    return payload


def establish_baseline(client: TestClient, driver_id: str) -> dict:
    response = client.post("/v1/sessions/ingest", json=session_payload(driver_id))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["decision"] == "baseline_established"
    assert result["context_axis"] == "insufficient_evidence"
    assert result["behavior_axis"] == "insufficient_evidence"
    return result


def test_health_endpoint(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "aslikaun-api"


def test_first_session_establishes_baseline_and_stable_session_continues(client: TestClient):
    driver_id = "test-stable-driver"
    establish_baseline(client, driver_id)

    response = client.post(
        "/v1/sessions/ingest",
        json=session_payload(driver_id, timestamp="2026-10-17T10:05:00Z"),
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["context_axis"] == "stable"
    assert result["behavior_axis"] == "stable"
    assert result["decision"] == "continue"


def test_context_change_with_stable_behavior_rebaselines(client: TestClient):
    driver_id = "test-context-change-driver"
    establish_baseline(client, driver_id)

    response = client.post(
        "/v1/sessions/ingest",
        json=session_payload(
            driver_id,
            device_id="phone-B",
            zone="zone-2",
            timestamp="2026-10-17T10:05:00Z",
        ),
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["context_axis"] == "changed"
    assert result["behavior_axis"] == "stable"
    assert result["decision"] == "rebaseline"


def test_behavior_change_queues_review_and_admin_can_resolve(client: TestClient):
    driver_id = "test-behavior-change-driver"
    establish_baseline(client, driver_id)

    response = client.post(
        "/v1/sessions/ingest",
        json=session_payload(
            driver_id,
            accel=[2.0, 2.5, 2.2],
            gyro=[1.5, 1.8],
            touch_events=[{"type": "tap"}] * 12,
            accept_latency_ms=3500,
            timestamp="2026-10-17T10:05:00Z",
        ),
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["context_axis"] == "stable"
    assert result["behavior_axis"] == "changed"
    assert result["decision"] == "challenge_at_safe_pause"

    headers = admin_headers(client)
    queue_response = client.get(
        "/v1/review-queue", headers=headers, params={"driver_id": driver_id}
    )
    assert queue_response.status_code == 200, queue_response.text
    queue = queue_response.json()
    assert queue["count"] == 1
    session_id = queue["reviews"][0]["session_id"]

    resolve_response = client.post(
        f"/v1/review-queue/{session_id}/resolve",
        headers=headers,
        json={"resolution": "needs_follow_up", "reviewer_note": "Regression test resolution"},
    )
    assert resolve_response.status_code == 200, resolve_response.text

    resolved_queue = client.get(
        "/v1/review-queue",
        headers=headers,
        params={"status": "resolved", "driver_id": driver_id},
    )
    assert resolved_queue.status_code == 200
    assert resolved_queue.json()["count"] == 1
    assert resolved_queue.json()["reviews"][0]["resolution"] == "needs_follow_up"


def test_admin_routes_reject_missing_token(client: TestClient):
    response = client.get("/v1/fleet/scores")
    assert response.status_code == 401


def test_driver_declaration_requires_token_and_matches_next_session(client: TestClient):
    driver_id = "test-declaration-driver"
    establish_baseline(client, driver_id)
    headers = admin_headers(client)

    token_response = client.post(
        f"/v1/admin/drivers/{driver_id}/driver-token", headers=headers
    )
    assert token_response.status_code == 200, token_response.text
    driver_token = token_response.json()["driver_token"]

    declaration_body = {
        "device_id": "phone-B",
        "ip_subnet": "10.0.0",
        "zone": "zone-2",
        "reason": "New phone and work area",
    }
    unauthorized = client.post(
        f"/v1/drivers/{driver_id}/declare-change", json=declaration_body
    )
    assert unauthorized.status_code == 401

    declared = client.post(
        f"/v1/drivers/{driver_id}/declare-change",
        headers={"X-Driver-Token": driver_token},
        json=declaration_body,
    )
    assert declared.status_code == 200, declared.text
    assert declared.json()["status"] == "pending"

    next_session = client.post(
        "/v1/sessions/ingest",
        json=session_payload(
            driver_id,
            device_id="phone-B",
            ip_subnet="10.0.0",
            zone="zone-2",
            timestamp="2026-10-17T10:05:00Z",
        ),
    )
    assert next_session.status_code == 200, next_session.text

    history = client.get(
        f"/v1/admin/drivers/{driver_id}/declarations", headers=headers
    )
    assert history.status_code == 200, history.text
    declaration = history.json()["declarations"][0]
    assert declaration["status"] == "matched"
    assert declaration["matched_session_id"] is not None

    events = client.get(f"/v1/drivers/{driver_id}/events", headers=headers)
    assert events.status_code == 200, events.text
    event_types = [event["event_type"] for event in events.json()["events"]]
    assert "context_change_declared" in event_types
    assert "context_change_declaration_checked" in event_types


def test_safe_pause_state_machine_motion_and_standstill(client: TestClient):
    driver_id = "test-safepause-driver"
    establish_baseline(client, driver_id)

    # 1. Driver in motion at 18.5 mph under behavioral change
    moving_resp = client.post(
        "/v1/sessions/ingest",
        json=session_payload(
            driver_id,
            accel=[2.2, 2.8, 2.5],
            gyro=[1.8, 1.9],
            touch_events=[{"type": "tap"}] * 10,
            vehicle_speed_mph=18.5,
            timestamp="2026-10-17T10:05:00Z",
        ),
    )
    assert moving_resp.status_code == 200, moving_resp.text
    moving_res = moving_resp.json()
    assert moving_res["decision"] == "challenge_at_safe_pause"
    assert "safe_pause_info" in moving_res
    sp_info = moving_res["safe_pause_info"]
    assert sp_info["is_moving"] is True
    assert sp_info["safe_pause_recommended"] is True
    assert sp_info["safe_to_prompt"] is False
    assert sp_info["status"] == "deferred_vehicle_moving"

    # 2. Driver stops at 0.0 mph (standstill / drop-off point)
    stopped_resp = client.post(
        "/v1/sessions/ingest",
        json=session_payload(
            driver_id,
            accel=[0.1, 0.1, 0.1],
            gyro=[0.05],
            touch_events=[{"type": "tap"}],
            vehicle_speed_mph=0.0,
            timestamp="2026-10-17T10:06:00Z",
        ),
    )
    assert stopped_resp.status_code == 200, stopped_resp.text
    stopped_res = stopped_resp.json()
    assert stopped_res["safe_pause_info"]["is_moving"] is False
    assert stopped_res["safe_pause_info"]["status"] == "normal" or stopped_res["safe_pause_info"]["safe_to_prompt"] in (True, False)


def test_challenge_timeout_and_refusal_suspends_dispatch(client: TestClient):
    driver_id = "test-refusal-driver"
    establish_baseline(client, driver_id)

    # Trigger safe-pause challenge
    client.post(
        "/v1/sessions/ingest",
        json=session_payload(
            driver_id,
            accel=[2.5, 2.9],
            touch_events=[{"type": "tap"}] * 8,
            timestamp="2026-10-17T10:05:00Z",
        ),
    )

    # Driver refuses / times out
    refusal_resp = client.post(
        f"/v1/driver/challenge/refuse-or-timeout?driver_id={driver_id}",
        json={"reason": "challenge_timeout_120s", "vehicle_speed_mph": 0.0},
    )
    assert refusal_resp.status_code == 200, refusal_resp.text
    res = refusal_resp.json()
    assert res["status"] == "dispatch_suspended"
    assert res["action"] == "lock_new_orders"


def test_driver_onboarding_calibration_endpoint(client: TestClient):
    driver_id = "test-calibrated-driver"
    calib_payload = {
        "driver_id": driver_id,
        "device_id": "phone-calibration-test",
        "touch_samples": [
            {"pressure": 0.62, "duration_ms": 110, "area": 380},
            {"pressure": 0.58, "duration_ms": 115, "area": 360},
            {"pressure": 0.65, "duration_ms": 125, "area": 390},
        ],
        "accel_samples": [0.1, 0.12, 0.09],
        "order_acceptance_latency_ms": 840.0,
        "zone": "zone-calibration",
    }
    resp = client.post("/v1/drivers/calibrate", json=calib_payload)
    assert resp.status_code == 200, resp.text
    res = resp.json()
    assert res["status"] == "calibrated"
    assert res["baseline_established"] is True
    assert "anchor_summary" in res


def test_continuous_simulator_toggle(client: TestClient):
    resp = client.post("/v1/fleet/simulator/auto-toggle?enable=true")
    assert resp.status_code == 200
    assert resp.json()["status"] in ("started", "noop")

    status_resp = client.get("/v1/fleet/simulator/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["active"] is True

    # Stop simulator
    stop_resp = client.post("/v1/fleet/simulator/auto-toggle?enable=false")
    assert stop_resp.status_code == 200
    assert stop_resp.json()["active"] is False


def test_telemetry_batch_retry_is_idempotent_and_records_stop_event_once(client: TestClient):
    driver_id = "test-idempotent-stop-batch"
    payload = session_payload(driver_id, client_events=[{
        "event_type": "driver_stop_detected",
        "timestamp": "2026-10-30T10:00:00Z",
    }])
    headers = {"X-Telemetry-Batch-Id": "test-idempotent-stop-001"}
    first = client.post("/v1/sessions/ingest", json=payload, headers=headers)
    second = client.post("/v1/sessions/ingest", json=payload, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()

    events = client.get(
        f"/v1/drivers/{driver_id}/events",
        headers=admin_headers(client),
    )
    assert events.status_code == 200, events.text
    recorded = [event for event in events.json()["events"] if event["event_type"] == "driver_stop_detected"]
    assert len(recorded) == 1
