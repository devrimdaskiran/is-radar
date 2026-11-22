# AI Career Radar 📡

Kişisel iş ilanı radarı. **GitHub sunucularında kendi kendine çalışır**: bilgisayarının açık olması gerekmez.
Uygun yeni bir ilan bulduğunda Telegram botun sana yazar: *"🚀 Bu ilan kriterlerine uyuyor, bir göz at."*

## Ne yapar?

Gündüz her 30 dakikada bir (06:00–23:30), gece bir kez GitHub Actions şunları yapar:

1. **İlanları toplar**
   - Kariyer.net, Eleman.net, İşin Olsun il listeleri (Gaziantep, Kahramanmaraş, Osmaniye, Hatay, Adana, Kilis).
   - Gemini + Google Arama ile keşif. Gelen her bağlantı açılıp doğrulanır.
   - İsteğe bağlı: Jooble API ve Gmail'e gelen iş alarmları (LinkedIn/Indeed).
2. **İlanı okur ve şartları çıkarır.** Eğitim seviyesi, bölüm, deneyim, askerlik, ehliyet, vardiya ve ikamet, ilandan birebir alıntıyla çıkarılır.
3. **Zorunlu şartları ayrı kontrol eder.** "En az lisans", "askerliğini tamamlamış" ya da zorunlu "en az 3 yıl deneyim" varsa ilan **gönderilmez**, "Kariyer Fırsatları"na yazılır.
4. **100 üzerinden puanlar.** Ağırlıklar: Eğitim %30, Teknik %25, Pozisyon %15, Deneyim %10, Konum %10, Diğer %10. İstenirse **Gemini** (ücretsiz) ilanı ayrıca yorumlar.
5. **60 puan ve üstü yeni ilanları Telegram'a gönderir.** Mesajda 6 buton olur: İlanı Görüntüle, Başvuru Sayfası, Favori, İlgilenmiyorum, Başvuru Yapıldı, Benzer İlanlar. Gece bulunanlar sabah gelir; aynı ilan iki kez gelmez.
6. **Rapor ve hatırlatma gönderir.** Her akşam 20:30'da günlük rapor, pazar günleri haftalık kariyer raporu gelir. Başvurudan 5 gün sonra hatırlatma yapılır.

## Gizlilik

Depo herkese açıktır (GitHub Actions dakikaları ücretsiz ve sınırsız olsun diye). Kişisel profil
(`configs/profile.yaml.enc`) ve tüm kayıtlar (`state` dalı) **şifrelidir**. Anahtar yalnızca senin
`RADAR_KEY` secret'ındadır. Kodda kişisel bilgi yoktur. Yapay zekâya adın, telefonun ve e-postan gönderilmez.

## Kurulum: GitHub Secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

| Secret | Zorunlu mu | Nereden alınır |
| --- | --- | --- |
| `RADAR_KEY` | **Evet** | Kurulumda sana verilen şifreleme anahtarı (kaybetme!) |
| `TELEGRAM_BOT_TOKEN` | **Evet** | Telegram'da @BotFather → /newbot (Pipaloo botunu da kullanabilirsin) |
| `GEMINI_API_KEY` | Önerilir | Pipaloo'daki anahtarın aynısı (aistudio.google.com) |
| `TELEGRAM_CHAT_ID` | Hayır | Boş bırak: botuna `/start` yazınca otomatik bağlanır |
| `JOOBLE_API_KEY` | Hayır | jooble.org/api/about formu |
| `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` | Hayır | Google Hesabı → Güvenlik → Uygulama şifreleri (LinkedIn/Indeed alarmları için) |

Sonra Telegram'da botuna **/start** yaz. İlk turda (en geç 30 dakika içinde) "AI Career Radar bağlandı!" mesajı gelir.
Hemen denemek için: **Actions → AI Career Radar → Run workflow**.

## Telegram komutları

`/jobs` `/best` `/today` `/week` `/career` `/search Gaziantep bakım` `/favorites` `/applications` `/stats`
`/sources` `/settings` `/profile` `/pause` `/resume` `/help`. Doğal dilde de yazabilirsin, örneğin
*"Gaziantep'te deneyim istemeyen işler"* ya da *"son 3 günde otomasyon ilanları"*.
Bot dönemsel çalıştığı için cevaplar birkaç dakika (en fazla bir sonraki tura kadar) gecikebilir.

## Dosyalar

- `radar/collectors/`: kaynak modülleri (site listeleri, Gemini arama, Jooble, e-posta alarmları, kariyer sayfaları)
- `radar/extract.py`: ilandan şart çıkarma
- `radar/matching.py`: zorunlu şart kapısı ve puanlama
- `radar/pipeline.py`: toplama, tekrar önleme ve bildirim kuyruğu
- `radar/runner.py`: GitHub Actions turu (Telegram etkileşimi, raporlar)
- `configs/sources.yaml`: kaynaklar, iller, aramalar · `configs/settings.yaml`: puan ağırlıkları, bildirim saatleri
- `tests/`: 63 test (gerçek Gaziantep ilanlarından alınmış örneklerle)

Bilgisayarda sürekli çalışan bot sürümü de mevcuttur (`python -m radar run`), ama gerekli değildir.
