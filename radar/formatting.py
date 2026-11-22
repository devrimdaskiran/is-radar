"""Telegram mesaj biçimleri (bot kütüphanesinden bağımsız; hem sürekli bot hem GitHub çalıştırıcısı kullanır)."""
from __future__ import annotations

import html
from datetime import time

E = html.escape


# ------------------------------------------------------------------ biçimlendirme
def fmt_date(d) -> str:
    return d.strftime("%d.%m.%Y") if d else "Belirtilmemiş"


def card(job, ev, reason: str = "yeni") -> str:
    sub = ev.subscores
    if getattr(job, "sector", None) == "kamu":
        return kamu_card(job, ev, reason)
    head = "🚀 <b>YENİ İŞ FIRSATI!</b>" if reason == "yeni" else "🔄 <b>İLAN GÜNCELLENDİ</b>"
    gate_line = {"Uygun": "✅ Zorunlu şartlar: karşılanıyor",
                 "Doğrulanmalı": "⚠️ Zorunlu şartlar: bazıları doğrulanmalı",
                 "Başvurulamaz": "⛔ Zorunlu şart karşılanmıyor"}.get(ev.gate, ev.gate)
    lines = [head, "",
             f"<b>Pozisyon:</b> {E(job.title)}",
             f"<b>Şirket:</b> {E(job.company or 'Belirtilmemiş')}",
             f"<b>Lokasyon:</b> {E(', '.join(x for x in [job.district, job.city] if x) or 'Belirtilmemiş')}",
             f"<b>Uygunluk:</b> %{ev.score:.0f} ({E(ev.category)})", gate_line, "",
             f"Eğitim %{sub['egitim']} · Teknik %{sub['teknik']} · Pozisyon %{sub['pozisyon']} · "
             f"Deneyim %{sub['deneyim']} · Konum %{sub['konum']}"]
    ex = ev.explanation
    if ex.get("neden_uygun"):
        lines += ["", "<b>NEDEN SANA UYGUN?</b>"] + [f"• {E(x)}" for x in ex["neden_uygun"][:4]]
    att = (ex.get("karsilanmayan") or []) + (ex.get("dikkat") or [])
    if att:
        lines += ["", "<b>DİKKAT EDİLMESİ GEREKENLER:</b>"] + [f"• {E(x)}" for x in att[:5]]
    if ex.get("eksik_bilgi"):
        lines += [f"<i>İlanda belirtilmemiş: {E(', '.join(ex['eksik_bilgi']))}</i>"]
    if ex.get("ai_yorum"):
        lines += ["", f"🤖 <i>{E(ex['ai_yorum'])}</i>"]
    perks = [p["ad"] for p in (job.fields or {}).get("yan_haklar", [])]
    if perks:
        lines += [f"🚌 Yan haklar: {E(', '.join(perks))}"]
    if (job.fields or {}).get("maas") or job.salary:
        lines += [f"💰 Maaş: {E((job.fields or {}).get('maas') or job.salary)}"]
    lines += ["", f"İlan Tarihi: {fmt_date(job.published_at or job.first_seen)}",
              f"Son Başvuru: {fmt_date(job.deadline)}",
              f"Kaynak: {E(', '.join(sorted({l.source for l in job.links})))}"]
    return "\n".join(lines)


def keyboard_rows(job) -> list[list[dict]]:
    urls = [l.url for l in job.links if l.url.startswith("http")]
    rows = []
    if urls:
        rows.append([{"text": "🔗 İlanı Görüntüle", "url": urls[0]},
                     {"text": "📝 Başvuru Sayfasına Git", "url": urls[-1]}])
    rows.append([{"text": "⭐ Favorilere Ekle", "callback_data": f"fav:{job.id}"},
                 {"text": "🙅 İlgilenmiyorum", "callback_data": f"ni:{job.id}"}])
    rows.append([{"text": "✅ Başvuru Yapıldı", "callback_data": f"app:{job.id}"},
                 {"text": "🔍 Benzer İlanlar", "callback_data": f"sim:{job.id}"}])
    return rows


def short(job, ev) -> str:
    flag = {"Uygun": "✅", "Doğrulanmalı": "⚠️", "Başvurulamaz": "⛔"}.get(ev.gate, "")
    flag = ("🏛" + flag) if getattr(job, "sector", None) == "kamu" else flag
    return f"{flag} <b>%{ev.score:.0f}</b> {E(job.title)} — {E(job.company or '?')} ({E(job.city or '?')}) /ilan_{job.id}"


def in_quiet_hours(st: dict, now: datetime) -> bool:
    b = st["bildirim"]
    if b.get("anlik"):
        return False
    s = time.fromisoformat(b["sessiz_baslangic"])
    e = time.fromisoformat(b["sessiz_bitis"])
    t = now.time()
    return (s <= t or t < e) if s > e else (s <= t < e)




def kamu_card(job, ev, reason: str = "yeni") -> str:
    head = "🏛 <b>KAMU İLANI</b>" if reason == "yeni" else "🔄 <b>KAMU İLANI GÜNCELLENDİ</b>"
    gate_line = {"Uygun": "✅ Görünen şartları karşılıyorsun",
                 "Doğrulanmalı": "⚠️ Bazı şartları kendin doğrulamalısın",
                 "Başvurulamaz": "⛔ Bir zorunlu şart karşılanmıyor"}.get(ev.gate, ev.gate)
    ex = ev.explanation
    lines = [head, "", f"<b>{E(job.title)}</b>", f"<b>Uygunluk:</b> %{ev.score:.0f} ({E(ev.category)})", gate_line]
    if ex.get("uygun_kadrolar"):
        lines += ["", "<b>SANA UYGUN KADROLAR (yapay zekâ, ilandan alıntıyla):</b>"]
        lines += [f"• {E(str(k.get('kadro', '')))} {('(' + E(str(k['adet'])) + ')') if k.get('adet') else ''}" for k in ex["uygun_kadrolar"]]
    if ex.get("neden_uygun"):
        lines += ["", "<b>NEDEN BAKMALISIN?</b>"] + [f"• {E(x)}" for x in ex["neden_uygun"][:4]]
    att = (ex.get("karsilanmayan") or []) + (ex.get("dikkat") or [])
    if att:
        lines += ["", "<b>KONTROL ET:</b>"] + [f"• {E(x)}" for x in att[:5]]
    if ex.get("ai_yorum"):
        lines += ["", f"🤖 <i>{E(ex['ai_yorum'])}</i>"]
    lines += ["", f"Yayın: {fmt_date(job.published_at or job.first_seen)} · Son başvuru: {fmt_date(job.deadline)}",
              f"Kaynak: {E(', '.join(sorted({l.source for l in job.links})))}"]
    return "\n".join(lines)
