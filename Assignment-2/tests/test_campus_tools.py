import json

import pytest

from campuspilot import campus_tools as ct


@pytest.fixture
def d(settings):
    return settings.data_dir


def _json(d, name):
    return json.loads((d / name).read_text())


# ---- data integrity ----
def test_data_files_consistent(d):
    profs = {p["professor_id"] for p in _json(d, "professors.json")}
    for c in _json(d, "courses.json"):
        assert c["professor_id"] in profs
    for r in _json(d, "rooms.json"):
        for b in r["bookings"]:
            assert b["day"] in ct.DAYS and r["open"] <= b["start"] < b["end"] <= r["close"]
    assert {e["category"] for e in _json(d, "events.json")} <= ct.EVENT_CATEGORIES
    for name in ("courses", "professors", "rooms", "events"):
        assert len(_json(d, f"{name}.json")) >= 3


# ---- search_course_catalog ----
def test_course_search_by_name_id_keyword(d):
    assert ct.search_course_catalog(d, "Machine Learning")["courses"][0]["course_id"] == "AIDS110"
    assert ct.search_course_catalog(d, "bda101")["count"] == 1
    r = ct.search_course_catalog(d, "llm")
    assert r["courses"][0]["name"] == "Agentic AI Engineering"
    assert r["courses"][0]["professor"] == "Dr. Neha Kulkarni"


def test_course_search_department_filter(d):
    assert ct.search_course_catalog(d, "business", "management")["count"] == 1
    assert ct.search_course_catalog(d, "python", "AI")["count"] == 0


def test_course_search_errors(d):
    assert "error" in ct.search_course_catalog(d, "")
    assert "error" in ct.search_course_catalog(d, "x")
    assert "error" in ct.search_course_catalog(d, None)
    assert "error" in ct.search_course_catalog(d, 123)
    assert "error" in ct.search_course_catalog(d, "python", "Underwater Basket Weaving")
    assert "error" in ct.search_course_catalog(d, "python", department="  ")
    empty = ct.search_course_catalog(d, "zzzzz")
    assert empty["count"] == 0 and "error" not in empty


# ---- get_professor_office_hours ----
def test_office_hours_partial_case_insensitive(d):
    r = ct.get_professor_office_hours(d, "kulkarni")
    assert r["professors"][0]["office_room"] == "F-305"
    assert {"day": "Wed", "start": "10:00", "end": "12:00"} in r["professors"][0]["office_hours"]


def test_office_hours_multiple_matches(d):
    assert len(ct.get_professor_office_hours(d, "Prof.")["professors"]) == 2


def test_office_hours_errors(d):
    assert "error" in ct.get_professor_office_hours(d, "Dr. Nobody")
    assert "error" in ct.get_professor_office_hours(d, "")
    assert "error" in ct.get_professor_office_hours(d, None)
    assert "error" in ct.get_professor_office_hours(d, "x")


# ---- check_room_availability ----
def test_room_day_bookings(d):
    r = ct.check_room_availability(d, "a-101", "monday")
    assert r["room_id"] == "A-101" and r["day"] == "Mon" and len(r["bookings"]) == 1
    assert ct.check_room_availability(d, "STUDY-2", "Sun")["bookings"] == []


def test_room_window_conflict_and_free(d):
    busy = ct.check_room_availability(d, "A-101", "Mon", "11:00", "12:00")
    assert busy["available"] is False and busy["conflicts"][0]["purpose"] == "BDA101 lecture"
    free = ct.check_room_availability(d, "A-101", "Mon", "11:30", "12:30")  # touching end is not a conflict
    assert free["available"] is True
    assert ct.check_room_availability(d, "A-101", "Tue", "10:00", "11:00")["available"] is True


def test_room_outside_opening_hours(d):
    r = ct.check_room_availability(d, "STUDY-2", "Mon", "19:30", "21:00")
    assert r["available"] is False and r["within_opening_hours"] is False


def test_room_errors(d):
    assert "error" in ct.check_room_availability(d, "Z-999", "Mon")
    assert "error" in ct.check_room_availability(d, "A-101", "Someday")
    assert "error" in ct.check_room_availability(d, "A-101", None)
    assert "error" in ct.check_room_availability(d, "", "Mon")
    assert "error" in ct.check_room_availability(d, "A-101", "Mon", "10:00")
    assert "error" in ct.check_room_availability(d, "A-101", "Mon", "25:00", "26:00")
    assert "error" in ct.check_room_availability(d, "A-101", "Mon", "noon", "1pm")
    assert "error" in ct.check_room_availability(d, "A-101", "Mon", "12:00", "11:00")


# ---- get_campus_events ----
def test_events_from_date_sorted(d):
    r = ct.get_campus_events(d, from_date="2026-11-01")
    dates = [e["date"] for e in r["events"]]
    assert dates == sorted(dates) and all(x >= "2026-11-01" for x in dates)
    assert "EVT05" not in {e["event_id"] for e in r["events"]}  # 2026-10-15 is before


def test_events_category_filter(d):
    r = ct.get_campus_events(d, category="Career", from_date="2026-01-01")
    assert [e["event_id"] for e in r["events"]] == ["EVT01"]


def test_events_none_found_is_not_error(d):
    r = ct.get_campus_events(d, from_date="2030-01-01")
    assert r["count"] == 0 and "error" not in r
    assert ct.get_campus_events(d)["from_date"]  # default start date (today) works


def test_events_errors(d):
    assert "error" in ct.get_campus_events(d, category="parties")
    assert "error" in ct.get_campus_events(d, category="")
    assert "error" in ct.get_campus_events(d, from_date="11/05/2026")
    assert "error" in ct.get_campus_events(d, from_date="2026-13-45")
    assert "error" in ct.get_campus_events(d, from_date=20261105)
