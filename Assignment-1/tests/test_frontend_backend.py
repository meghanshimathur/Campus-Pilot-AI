"""Backend support for the dashboard (static serving, evaluation-report endpoints) and static audits of the frontend files."""
import json
import re
import shutil
import subprocess
from dataclasses import replace
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from campuspilot.api import create_app
from campuspilot.config import ROOT

FRONTEND = ROOT / "frontend"
JS = sorted((FRONTEND / "js").glob("*.js"))
CSS = sorted((FRONTEND / "css").glob("*.css"))


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


# ---------- serving ----------
def test_dashboard_served_and_api_still_wins(client):
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "CampusPilot AI" in page.text and 'id="chat-log"' in page.text
    assert client.get("/api/health").json()["status"] == "ok"      # API routes are not shadowed by the static mount
    assert client.get("/api/does-not-exist").status_code == 404
    assert "javascript" in client.get("/js/app.js").headers["content-type"]
    assert client.get("/css/base.css").status_code == 200
    assert client.get("/js/nope.js").status_code == 404


def test_app_works_without_frontend_dir(settings, tmp_path):
    c = TestClient(create_app(replace(settings, frontend_dir=tmp_path / "missing")))
    assert c.get("/api/health").status_code == 200 and c.get("/").status_code == 404


# ---------- evaluation report endpoints ----------
def report(mode="offline", ts="2026-10-22 00:04:02", runs=13):
    return {"mode": mode, "meta": {"timestamp": ts, "provider": "p", "model": "m"},
            "metrics": {"runs": runs, "cases": 12}, "runs": []}


def with_reports(settings, tmp_path):
    d = tmp_path / "reports"
    d.mkdir()
    return replace(settings, reports_dir=d), d


def test_reports_list_and_get(settings, tmp_path):
    s, d = with_reports(settings, tmp_path)
    (d / "evaluation_offline_20261022_000402.json").write_text(json.dumps(report()))
    (d / "evaluation_live_20261023_101010.json").write_text(json.dumps(report("live", "2026-10-23 10:10:10", 13)))
    (d / "evaluation_offline_20261022_000402.md").write_text("# not json")
    c = TestClient(create_app(s))
    listing = c.get("/api/evaluation/reports").json()["reports"]
    # newest first across modes (a name sort would put 'offline' before 'live' regardless of time)
    assert [r["name"] for r in listing] == ["evaluation_live_20261023_101010.json", "evaluation_offline_20261022_000402.json"]
    assert {r["mode"] for r in listing} == {"offline", "live"}
    assert all(set(r) == {"name", "mode", "timestamp", "runs", "integrity"} for r in listing)
    got = c.get("/api/evaluation/reports/evaluation_live_20261023_101010.json").json()
    assert got["mode"] == "live" and got["metrics"]["cases"] == 12  # returned exactly as saved


def test_reports_empty_or_missing_dir_returns_empty_list_not_fake_data(settings, tmp_path):
    s, _ = with_reports(settings, tmp_path)
    assert TestClient(create_app(s)).get("/api/evaluation/reports").json() == {"reports": []}
    s2 = replace(settings, reports_dir=tmp_path / "does-not-exist")
    assert TestClient(create_app(s2)).get("/api/evaluation/reports").json() == {"reports": []}


def test_malformed_reports_are_skipped_not_invented(settings, tmp_path):
    s, d = with_reports(settings, tmp_path)
    (d / "evaluation_offline_20261022_000001.json").write_text("{not json")
    (d / "evaluation_offline_20261022_000002.json").write_text(json.dumps({"mode": "offline"}))  # missing metrics
    (d / "evaluation_offline_20261022_000003.json").write_text(json.dumps(report()))
    c = TestClient(create_app(s))
    assert [r["name"] for r in c.get("/api/evaluation/reports").json()["reports"]] == ["evaluation_offline_20261022_000003.json"]
    assert c.get("/api/evaluation/reports/evaluation_offline_20261022_000001.json").status_code == 404


@pytest.mark.parametrize("name", ["../secret.json", "..%2Fsecret.json", "evaluation_offline_20261022_000402.md",
                                  "evaluation_other_20261022_000402.json", "EVALUATION_offline_20261022_000402.json",
                                  "evaluation_offline_2026_000402.json", "evaluation_offline_20261022_000402.json.bak",
                                  "%2e%2e/%2e%2e/etc/passwd", "evaluation_offline_20261022_000402.json%00.txt"])
def test_report_name_is_strictly_validated(settings, tmp_path, name):
    s, d = with_reports(settings, tmp_path)
    (tmp_path / "secret.json").write_text('{"secret": true}')
    (d / "evaluation_offline_20261022_000402.json.bak").write_text(json.dumps(report()))
    assert TestClient(create_app(s)).get(f"/api/evaluation/reports/{name}").status_code in (404, 422)


def test_real_saved_reports_are_listed(client):
    reports = client.get("/api/evaluation/reports").json()["reports"]
    assert reports, "reports/ should contain the real offline evaluation runs"
    first = client.get(f"/api/evaluation/reports/{reports[0]['name']}").json()
    assert first["mode"] in ("offline", "live") and first["metrics"]["runs"] == reports[0]["runs"]


# ---------- static audits of the frontend source ----------
class _Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


@pytest.fixture(scope="module")
def html():
    c = _Collector()
    c.feed((FRONTEND / "index.html").read_text())
    return c.tags


def test_html_links_resolve(html):
    for tag, a in html:
        for attr in ("href", "src"):
            ref = a.get(attr)
            if ref and not ref.startswith(("#", "data:")):
                assert (FRONTEND / ref).is_file(), f"{tag} {attr}={ref} does not exist"


def test_js_imports_resolve():
    for f in JS:
        for ref in re.findall(r'from\s+"(\./[^"]+)"', f.read_text()):
            assert (f.parent / ref).is_file(), f"{f.name} imports missing {ref}"


def _code(path):
    """Source with comments removed, so the audit inspects code, not prose."""
    text = path.read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


def test_no_external_requests_or_unsafe_apis():
    for f in [FRONTEND / "index.html", *JS, *CSS]:
        text = f.read_text()
        assert not re.search(r"https?://", text), f"{f.name} references an external URL"
        assert "@import" not in text and "url(" not in text
    for f in JS:
        text = _code(f)
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert banned not in text, f"{f.name} uses {banned}"
        assert "localStorage" not in text and "sessionStorage" not in text, f"{f.name} persists data in browser storage"


def test_no_frameworks():
    text = " ".join(f.read_text() for f in [FRONTEND / "index.html", *JS])
    for name in ("react", "streamlit", "jquery", "vue", "angular", "from \"http"):
        assert name not in text.lower(), name


def test_accessibility_basics(html):
    ids = {a["id"] for _, a in html if "id" in a}
    root = next(a for t, a in html if t == "html")
    assert root.get("lang") == "en"
    assert any(t == "meta" and a.get("name") == "viewport" for t, a in html)
    assert any(t == "a" and a.get("class") == "skip-link" and a["href"].lstrip("#") in ids for t, a in html)
    assert sum(t == "main" for t, _ in html) == 1 and sum(t == "h1" for t, _ in html) == 1
    labelled = {a.get("for") for t, a in html if t == "label"}
    for t, a in html:
        if t in ("input", "textarea", "select"):
            assert a.get("id") in labelled or a.get("aria-label"), f"unlabelled {t} #{a.get('id')}"
    tabs = [a for t, a in html if a.get("role") == "tab"]
    panels = {a["id"]: a for t, a in html if a.get("role") == "tabpanel"}
    assert len(tabs) == len(panels) == 4
    for tab in tabs:
        assert tab["aria-controls"] in panels and panels[tab["aria-controls"]]["aria-labelledby"] == tab["id"]
    assert sum(tab.get("aria-selected") == "true" for tab in tabs) == 1
    live = [a for t, a in html if a.get("aria-live") or a.get("role") in ("status", "alert", "log")]
    assert len(live) >= 5


def test_css_supports_reduced_motion_focus_and_responsive():
    css = " ".join(f.read_text() for f in CSS)
    assert "prefers-reduced-motion" in css and ":focus-visible" in css
    assert "max-width: 1023.98px" in css and "max-width: 640px" in css


# ---------- Node unit tests, run from pytest when Node is available ----------
@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_node_unit_tests_pass():
    files = sorted(str(p) for p in (ROOT / "tests" / "frontend").glob("*.test.mjs"))
    out = subprocess.run(["node", "--test", *files], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]


def test_long_provider_errors_wrap_in_the_chat_badge():
    css = (FRONTEND / "css" / "components.css").read_text()
    assert ".msg-meta .badge-danger" in css and "white-space: normal" in css.split(".msg-meta .badge-danger")[1].split("}")[0]


# ---------- the Evaluation tab must not present scripted results as live ----------
def _real_offline_report():
    return json.loads(sorted((ROOT / "reports").glob("evaluation_offline_*.json"))[-1].read_text())


def test_api_flags_scripted_results_labelled_live(settings, tmp_path):
    """Regression: scripted runs saved with mode='live' used to be listed (and rendered) as a real LIVE run."""
    s, d = with_reports(settings, tmp_path)
    genuine = _real_offline_report()
    (d / "evaluation_offline_20261022_000402.json").write_text(json.dumps(genuine))
    spoof = json.loads(json.dumps(genuine)); spoof["mode"] = "live"
    (d / "evaluation_live_20261023_000000.json").write_text(json.dumps(spoof))
    honest = json.loads(json.dumps(genuine)); honest["mode"] = "live"; honest["meta"]["provider"] = "gemini"
    for r in honest["runs"]:
        r["provider"] = "gemini"
    (d / "evaluation_live_20261024_000000.json").write_text(json.dumps(honest))
    mislabelled_offline = json.loads(json.dumps(honest)); mislabelled_offline["mode"] = "offline"
    (d / "evaluation_offline_20261025_000000.json").write_text(json.dumps(mislabelled_offline))
    c = TestClient(create_app(s))
    by_name = {r["name"]: r for r in c.get("/api/evaluation/reports").json()["reports"]}
    assert by_name["evaluation_offline_20261022_000402.json"]["integrity"] == "ok"
    assert by_name["evaluation_live_20261023_000000.json"]["integrity"] == "inconsistent"      # scripted data called live
    assert by_name["evaluation_live_20261024_000000.json"]["integrity"] == "ok"                # genuinely from a real provider
    assert by_name["evaluation_offline_20261025_000000.json"]["integrity"] == "inconsistent"   # real data called offline
    got = c.get("/api/evaluation/reports/evaluation_live_20261023_000000.json").json()
    assert got["integrity"] == "inconsistent" and got["mode"] == "live" and got["metrics"] == genuine["metrics"]  # content untouched
    assert c.get("/api/evaluation/reports/evaluation_live_20261024_000000.json").json()["integrity"] == "ok"


def test_integrity_is_computed_by_the_server_never_trusted_from_the_file(settings, tmp_path):
    s, d = with_reports(settings, tmp_path)
    spoof = _real_offline_report(); spoof["mode"] = "live"; spoof["integrity"] = "ok"   # file lies about itself
    (d / "evaluation_live_20261023_000000.json").write_text(json.dumps(spoof))
    c = TestClient(create_app(s))
    assert c.get("/api/evaluation/reports").json()["reports"][0]["integrity"] == "inconsistent"
    assert c.get("/api/evaluation/reports/evaluation_live_20261023_000000.json").json()["integrity"] == "inconsistent"


def test_real_saved_reports_report_integrity_ok(client):
    reports = client.get("/api/evaluation/reports").json()["reports"]
    assert reports and all(r["mode"] in ("offline", "live") and r["integrity"] == "ok" for r in reports)
