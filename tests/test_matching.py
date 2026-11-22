"""Diploma eşleştirme, zorunlu şart, puanlama: gerçek ilan parçalarıyla (fixtures/real_postings.json)."""
import pytest

from radar.extract import extract_fields
from radar.matching import evaluate, prefilter

from .conftest import TODAY

IDS = [p["id"] for p in __import__("json").loads(
    (__import__("pathlib").Path(__file__).parent / "fixtures" / "real_postings.json").read_text(encoding="utf-8"))]


def run(p, profile, settings):
    f = extract_fields(p["title"], p["description"], TODAY)
    ok, _ = prefilter(p["title"], p["description"], profile, f, None, today=TODAY)
    r = evaluate(p["title"], p["description"], f, profile, settings, city=p["city"], today=TODAY)
    return f, ok, r


@pytest.mark.parametrize("pid", IDS)
def test_real_posting_expectations(pid, postings, profile, settings):
    p = next(x for x in postings if x["id"] == pid)
    exp = p["beklenen"]
    f, ok, r = run(p, profile, settings)
    checks = {c.name: c.status for c in r.checks}
    if "prefilter" in exp:
        assert ok is exp["prefilter"]
    if "gate" in exp:
        assert r.gate == exp["gate"], (r.gate, checks)
    if "gate_not" in exp:
        assert r.gate != exp["gate_not"], checks
    for key, name in [("egitim", "Eğitim"), ("deneyim", "Deneyim"), ("askerlik", "Askerlik"), ("vardiya", "Vardiya"),
                      ("ehliyet", "Ehliyet")]:
        if key in exp:
            assert checks.get(name) == exp[key], (name, checks)
    if "eksik_beceri_icerir" in exp:
        assert exp["eksik_beceri_icerir"] in r.extra["eksik_beceriler"]
    if "kariyer" in exp:
        assert r.career_track == exp["kariyer"]
    if exp.get("maas_var"):
        assert f.get("maas")
    if "bildirim" in exp:
        would_notify = ok and r.gate != "Başvurulamaz" and r.score >= settings["bildirim"]["min_puan"]
        assert would_notify is exp["bildirim"], (r.score, r.gate, checks)


def test_lisans_never_applicable_even_with_high_skill(profile, settings):
    """Spesifikasyon: yalnız lisans kabul eden ilan, yüksek beceri uyumuna rağmen başvurulabilir gösterilmez."""
    desc = ("• Üniversitelerin 4 yıllık Elektrik-Elektronik Mühendisliği bölümünden mezun\n"
            "• AutoCAD, Arduino, Python, Proteus, ESP32 ve teknik çizim bilgisi\n• Deneyim şartı aranmamaktadır")
    f = extract_fields("Elektronik Mühendisi", desc, TODAY)
    r = evaluate("Elektronik Mühendisi", desc, f, profile, settings, city="Gaziantep", today=TODAY)
    assert r.subscores["teknik"] >= 90
    assert r.gate == "Başvurulamaz"


def test_similar_name_is_not_substitute(profile, settings):
    """Mekatronik ön lisans, 'Mekatronik Mühendisliği' şartının yerine geçmez."""
    desc = "• Mekatronik Mühendisliği bölümünden mezun"
    f = extract_fields("Proje Mühendisi", desc, TODAY)
    r = evaluate("Proje Mühendisi", desc, f, profile, settings, city="Gaziantep", today=TODAY)
    assert {c.name: c.status for c in r.checks}["Eğitim"] == "Karşılamıyor"


def test_unknown_fields_are_not_invented(profile, settings):
    f = extract_fields("Teknisyen", "Takım çalışmasına yatkın adaylar.", TODAY)
    for key in ("egitim", "deneyim", "askerlik", "ehliyet", "maas", "son_basvuru"):
        assert key not in f
    r = evaluate("Teknisyen", "Takım çalışmasına yatkın adaylar.", f, profile, settings, city=None, today=TODAY)
    assert "Eğitim" in r.explanation["eksik_bilgi"]


def test_deferral_years_rule(profile, settings):
    ok_desc = "• Askerliğini yapmış veya en az 1 yıl tecilli olmak"
    bad_desc = "• Askerliğini yapmış veya en az 3 yıl tecilli olmak"
    r1 = evaluate("Teknisyen", ok_desc, extract_fields("Teknisyen", ok_desc), profile, settings, today=TODAY)
    r2 = evaluate("Teknisyen", bad_desc, extract_fields("Teknisyen", bad_desc), profile, settings, today=TODAY)
    assert {c.name: c.status for c in r1.checks}["Askerlik"] == "Karşılıyor"
    assert {c.name: c.status for c in r2.checks}["Askerlik"] == "Karşılamıyor"


def test_location_far_is_not_eliminated(profile, settings):
    desc = "• Meslek Yüksekokullarının Mekatronik bölümünden mezun\n• Lojman imkânı sağlanmaktadır."
    f = extract_fields("Mekatronik Teknikeri", desc)
    r = evaluate("Mekatronik Teknikeri", desc, f, profile, settings, city="Kocaeli", today=TODAY)
    assert r.location_tier == "diger"
    assert r.gate == "Uygun"            # elenmez
    assert r.subscores["konum"] == 40   # 25 + lojman 15
    assert any("yakın bölgende değil" in d for d in r.explanation["dikkat"])


def test_explanation_has_evidence(postings, profile, settings):
    p = next(x for x in postings if x["id"] == "erdem-cam-makine-teknikeri")
    _, _, r = run(p, profile, settings)
    exp = next(c for c in r.checks if c.name == "Deneyim")
    assert "en az 3 yıl" in exp.evidence
    assert exp.evidence in p["description"]   # kanıt ilandan birebir
