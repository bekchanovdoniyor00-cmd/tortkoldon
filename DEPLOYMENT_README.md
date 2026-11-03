# TO‘RTKO‘LDONMAXSULOTI ERP — IT mutaxassisiga topshirish

## Paket maqomi

Bu paket 4-C sinov nusxasidan tayyorlangan. Undagi `data/tortkuldon_clean.sqlite`
42 ta jadval, 4 ta faol foydalanuvchi va nol operatsion yozuvlarga ega.
Ushbu baza faqat boshlang‘ich sinov nusxasi: uni ishlab chiqarish bazasi deb
qabul qilmang va real qoldiq yoki savdoni ruxsatsiz kiritmang.

Serverga joylashtirish bajarilmagan. Domen, DNS, bulutli hisob, production
maxfiy kaliti va foydalanuvchi parollari paketga kiritilmagan.
`app.py` ni bevosita production server sifatida ishga tushirmang: u
sinov/legacy yo‘llarini o‘z ichiga oladi. Production uchun faqat quyida
ko‘rsatilgan tekshiriladigan entrypoint’dan foydalaning.

## Arxitektura

Tavsiya etilgan Linux arxitekturasi:

`Telefon/kompyuter → HTTPS Nginx → Gunicorn (127.0.0.1:8000) → Flask → SQLite`

`linux_wsgi.py` production rejimi, tashqi konfiguratsiya va maxfiy kalitni
talab qiladi; DB sxemasi va SQLite yaxlitligini tekshiradi; HTTPS-proxy
headerlarini faqat ruxsat etilgan localhost proxy’dan qabul qiladi. Gunicorn
`deploy/linux/gunicorn.conf.py` orqali bir worker va 8 thread bilan ishga
tushadi. Gunicorn portini internetga ochmang.

Windows server tanlansa, `production_server.py` + Waitress va
`windows_service_host.py` + tasdiqlangan WinSW wrapper namunalarini ko‘rib
chiqing. `WINDOWS_PRODUCTION_GUIDE.md` deployment namunalarini beradi.
Linux/Windows tanlovi hali tasdiqlanmagan; xizmat, unit, ACL va reverse-proxy
konfiguratsiyasi tegishli server mutaxassisi tomonidan alohida ko‘rib chiqilsin.

## Tarkib va Python paketlari

- Flask ilovasi va mahalliy modullar: `app.py`, moliya, sana, Excel, tarjima,
  hisobot, kassa va SQLite backup modullari.
- `templates/`, `static/`, `export_templates/` — joriy interfeys va hisobot
  resurslari.
- `requirements.txt` — Flask, openpyxl, Pillow, Waitress.
- `requirements-linux.txt` — Linux production uchun Gunicorn qo‘shilgan
    dependency ro‘yxati.
- `deploy/linux/` — Gunicorn, Nginx, systemd, runtime-config, backup-config
  namunalari.
- `windows_service_host.py`, `.bat` launch/control/migration skriptlari,
  `ToRtkoERP.xml.example` — Windows uchun namunalar.
- `test_*.py` va `waitress_test_server.py` — testlar va izolyatsiyalangan test
  serveri; production ishga tushirish uchun emas.
- `data/tortkuldon_clean.sqlite` — toza baza nusxasi.

Python versiyasi, OS kutubxonalari va paket versiyalari tanlangan serverda
alohida o‘rnatilib tekshirilsin. Windows `.venv` nusxasini Linux’ga ko‘chirmang.
Staging’dagi `test_production_server.py` va `test_windows_service.py`
subprocess testlari `.venv` katalogini talab qilmasligi uchun joriy
`sys.executable` interpreteridan foydalanadi; bu faqat test harness nusxasidagi
o‘zgarish, runtime ERP kodiga ta’sir qilmaydi.

## Ma’lumotlar bazasi

Production loader ilova kodidan tashqaridagi, oldindan mavjud DB yo‘lini talab
qiladi va startup vaqtida `init_db()` yoki migratsiyani chaqirmaydi. Paketdagi
`data/tortkuldon_clean.sqlite` ni release papkasidan deployment ma’lumot
papkasiga **yangi fayl sifatida** nusxalash mumkin, masalan:

- Linux: `/var/lib/tortkuldon/tortkuldon.sqlite`
- Windows: `C:\ERPData\tortkuldon.sqlite`

Avval nusxa hashini qayd qiling, `integrity_check`, `foreign_key_check`, jadval
soni, foydalanuvchilar va nol biznes yozuvlarini tekshiring. Nusxani tanlangan
production config’dagi `database_path` bilan aynan bir xil qiling. DB hamda
SQLite journal/WAL fayllari uchun faqat xizmat hisobiga kerakli katalog
huquqini bering. SQLite faylini NFS/SMB yoki boshqa tarmoq fayl tizimida
ishlatmang.

Paket ichidagi bazani to‘g‘ridan-to‘g‘ri production’da ishlatmang: code
release daraxti ichidagi DB production validator tomonidan rad etiladi va
release rollback/yangilanish jarayoniga biznes ma’lumotlarini aralashtiradi.
Paketdagi bazani saqlab, data katalogiga alohida nusxa yarating.

Amaldagi kod `user_auth_versions` jadvalini talab qiladi. Eski, ma’lumotli
baza tanlansa, avval alohida backup va staging restore’da migratsiya
sinovidan o‘ting; tasdiqlangan maintenance oynasisiz production DB’ga
migratsiya qilmang.

## Toza bazadagi foydalanuvchilar

| Login nomi | Rol |
|---|---|
| Buxgalter | `buxgalter` |
| Boshliq | `rahbar` |
| Umid | `umid` |
| Maxmud | `maxmud` |

Baza faqat password hashlarini saqlaydi; bu paketda parollar yo‘q.
Hashlar ishlab chiqarish uchun tasdiqlangan parol hisoblanmaydi. Birinchi
public kirishdan oldin har bir hisob uchun alohida, kuchli parolni xavfsiz
tarzda o‘rnating va foydalanuvchiga parol menejeri yoki boshqa tasdiqlangan
maxfiy kanal orqali yetkazing. Parol, session secret, token yoki SSH kalitini
ZIP, source, JSON config, shell tarixi yoki ticket’ga yozmang. Parol
almashtirilganda eski sessiyalar bekor qilinishini va loginni qayta tekshiring.

## Linux VPS’da ishga tushirish

1. Qo‘llab-quvvatlanadigan Ubuntu LTS/x86-64 VPS yarating. Dastlabki
   rejalashtirish o‘lchami: 2 vCPU, 4 GB RAM, 40–80 GB lokal SSD; real
   Excel/foydalanuvchi yuklamasida o‘lchang va kerak bo‘lsa kattalashtiring.
2. Maxsus sudo huquqi bo‘lmagan `tortkuldon` servis hisobini, versioned
   release papkasini va alohida `/var/lib`, `/var/backups`, `/var/log`
   kataloglarini yarating.
3. Virtual environment yarating va `pip install -r requirements-linux.txt`
   bajaring. Har bir paket versiyasini deploymentdan oldin pin/lock qilib,
   qayta o‘rnatish sinovidan o‘tkazing.
4. `deploy/linux/production_config.example.json` ni
   `/etc/tortkuldon/production.json` ga nusxalang. `database_path`,
   `backup_directory`, trusted host, loopback proxy IP va log yo‘llarini real
   serverga moslang. Secret’i JSON’da saqlamang.
5. Kamida 32 belgili tasodifiy `ERP_SECRET_FILE` yarating; root va servis
   hisobidan boshqalarga o‘qish huquqi bermang. `ERP_MODE=production`,
   `ERP_CONFIG_PATH` va `ERP_SECRET_FILE` systemd unit’da tashqi yo‘l sifatida
   belgilanadi.
6. `deploy/linux/tortkuldon.service.example` ni ko‘rib chiqib systemd unit
   yarating. So‘ng Nginx namunasini server_name, TLS sertifikat va upstream
   bilan moslang. `nginx -t`, `systemd-analyze verify` va offline smoke test
   muvaffaqiyatli bo‘lmaguncha DNS yoki user traffic’ni yoqmang.
7. Local DB’dagi bir worker/8 thread boshlang‘ich tanlovdir. Bir nechta
   Gunicorn worker yoki parallel app instance qo‘shishdan oldin SQLite writer
   contention, backup va load sinovini bajaring.

Namunaviy launch (faqat config/secret/DB tayyor va tekshirilgach):

```sh
cd /opt/tortkuldon/current
ERP_MODE=production \
ERP_CONFIG_PATH=/etc/tortkuldon/production.json \
ERP_SECRET_FILE=/etc/tortkuldon/session_secret \
/opt/tortkuldon/venv/bin/gunicorn \
  --config /opt/tortkuldon/current/deploy/linux/gunicorn.conf.py
```

Production’da odatiy ishga tushirish systemd orqali bo‘lsin, shell’dan
foreground jarayon sifatida emas. `gunicorn app:app`, Flask development
server, `debug=True`, va public `:8000` listener taqiqlanadi.

## Windows bulut serveri varianti

Server Windows bo‘lishi tanlansa, Windows uchun tasdiqlangan production
platforma, Python va `waitress` o‘rnating. `production_server.py` va
`windows_service_host.py` sozlamalarini, WinSW versiyasini, xizmat
hisobini/ACL’larini, tashqi JSON config va maxfiy kalit faylini IT mutaxassis
tekshirsin. `start_production_server.bat` interaktiv/manual ishga tushirish
namunasi; avtomatik qayta yuklanish uchun Windows Service rejimini afzal
ko‘ring. Real service/install/start hozir bajarilmagan.

## tortkoldon.uz, HTTPS va mobil kirish

1. Domenni DNS boshqaradigan tomon va VPS public IP’ni aniqlang.
2. `tortkoldon.uz` va ishlatilsa `www.tortkoldon.uz` uchun DNS yozuvlarini
   faqat egasi tasdiqlagandan keyin yarating.
3. Nginx’da sertifikat oling/yangilanishini tekshiring (masalan, ACME/Let’s
   Encrypt); HTTP’ni HTTPS’ga yo‘naltiring. TLS 1.2/1.3, xavfsiz cookie va
   HSTS’ni tekshiring.
4. Firewall’da faqat SSH’ni belgilangan admin IP’lariga va 80/443’ni zarur
   holatda oching. Gunicorn porti faqat loopback’da qolsin. `trusted_hosts`
   ichida faqat ishlatiladigan aniq domenlar bo‘lsin.
5. Interfeys viewport, mobil breakpoint, PWA manifest va HTTPS bo‘lganda
   service worker’ni qo‘llaydi. Xodimlarning Umid/Maxmud, buxgalter va rahbar
   sahifalarini iPhone/Android’da haqiqiy HTTPS domen orqali sinang. PWA
   service worker operatsiyalarni offline’da saqlamaydi; offline sotuv
   kiritmang.

## SQLite, to‘rtta foydalanuvchi va keyingi PostgreSQL

SQLite kichik ERP va to‘rtta foydalanuvchi uchun **bitta VPS, bitta app
instance, past/odatdagi yozuv yuklamasi** sharoitida maqbul boshlang‘ich
variant bo‘lishi mumkin. Kodda `busy_timeout=30000` va muhim operatsiyalarda
`BEGIN IMMEDIATE` ishlatiladi; mavjud regressiyada 8 parallel test yozuvchisi
160 yozuvni vaqtinchalik DB’da bajargan. Bu kichik regressiya testi, production
load/availability kafolati emas. SQLite bir vaqtda bitta writer’ni
seriallashtiradi.

Har bir HTTPS so‘rov Flask signed cookie session’dan foydalanadi; cookie
production’da `Secure`, `HttpOnly`, `SameSite=Lax`, sessiya muddati
chegaralangan. Parol o‘zgarganda auth-version tekshiruvi eski sessiyalarni
rad etadi. Login limiteri process xotirasida; restart’da tiklanadi. Gunicorn
worker sonini oshirish bu limiter holatini process’lar orasida bo‘lishmaydi.
Production konfiguratsiyasida bir worker boshlang‘ich va ehtiyotkor tanlov.

Quyidagi holatlarda PostgreSQL’ga o‘tishni rejalashtiring: bir nechta app
instance/host, muntazam `database is locked` yoki katta lock kutishlari,
yozuvlar ko‘payishi, yuqori mavjudlik, point-in-time recovery yoki audit
talablari. O‘tish oddiy DSN almashtirish emas: SQL dialekt, transaction,
idempotency, schema/migration, backup/restore va hisobotlarni alohida
moslashtirib, to‘liq staging va reconciliation sinovi kerak.

## Avtomatik va off-site backup/restore

- Linux namunalarida `daily_sqlite_backup.py`, systemd oneshot va timer bor.
  U SQLite Online Backup API bilan izchil snapshot yaratib,
  `integrity_check` va `foreign_key_check` bajaradi. Backup source yo‘lini
  aniq ko‘rsating; bo‘sh namuna manbasini production’ga taxmin bilan
  o‘zgartirmang.
- Zaxira katalogi DB’dan va code release’dan alohida bo‘lsin; disk
  shifrlanishi va faqat servis/backup hisoblariga ruxsat bilan himoyalansin.
- Har kuni zaxirani ikkinchi, serverdan va hosting account’dan ajratilgan
  hududga client-side encryption bilan nusxalang (masalan, alohida object
  storage account). Kalit va upload credential’ni server code’dan tashqarida,
  minimal huquq bilan saqlang. C: va D: bir fizik diskdagi nusxalar off-site
  zaxira hisoblanmaydi.
- Backup job muvaffaqiyatsiz bo‘lsa monitoring/ogohlantirish yuboring.
  Retention va RPO/RTO biznes egasi bilan kelishilsin; namunadagi 30 kun
  siyosatni avtomatik qabul qilmang.
- Kamida oyiga bir marta backup’ni **yangi, bo‘sh, alohida test yo‘liga**
  tiklang; hash, integrity/FK, user, sotuv, qarz, kassa, ombor va ishlab
  chiqarish jami qiymatlarini tekshiring. Restore hech qachon DB ustiga
  avtomatik yozmasin.

## Paket oldi test holati

4-C sinov nusxasidagi barcha 22 test moduli alohida Python jarayonlarida
bajarildi. Har bir SQLite testi o‘zining vaqtinchalik yoki `:memory:` bazasidan
foydalandi. HTTP/production/service integratsiyalari faqat vaqtinchalik baza,
soxta foydalanuvchilar va localhost test serverlarida bajarildi. Yakuniy natija:
**134 test, 134 PASS, 0 FAIL, 0 ERROR, 0 SKIP; 22/22 modul PASS**.
Sinovlar Windows 10 muhitida Python 3.14.10 bilan bajarildi.

| Test moduli | Testlar | Natija |
|---|---:|---|
| `test_accountant_cash_add` | 4 | PASS |
| `test_business_date_app_integration` | 3 | PASS |
| `test_business_date_migration` | 2 | PASS |
| `test_combined_finance_report` | 1 | PASS |
| `test_date_sql` | 11 | PASS |
| `test_debt_invoice_date_formats` | 7 | PASS |
| `test_excel_dates_formats` | 9 | PASS |
| `test_finance_calculations_dmy` | 6 | PASS |
| `test_language_switching` | 8 | PASS |
| `test_linux_wsgi` | 4 | PASS |
| `test_monthly_reporting` | 7 | PASS |
| `test_pagination` | 5 | PASS |
| `test_production_server` | 13 | PASS |
| `test_pwa` | 4 | PASS |
| `test_security_fixes` | 5 | PASS |
| `test_sqlite_backup` | 8 | PASS |
| `test_unified_excel_report` | 9 | PASS |
| `test_waitress_http` | 4 | PASS |
| `test_waitress_server` | 4 | PASS |
| `test_wheat_date_validation` | 11 | PASS |
| `test_wheat_opening_stock` | 4 | PASS |
| `test_windows_service` | 5 | PASS |
| **Jami** | **134** | **134 PASS** |

Avvalgi 44 FAIL va 10 ERROR test fixture’laridagi eskirgan sessiyalardan kelib
chiqqan: login himoyasi qo‘shilgach qo‘lda tuzilgan sessiyalarda `auth_version`
yo‘q edi; unified Excel testidagi `current_user` mockida ham shu maydon
yetishmagan. Autentifikatsiya kodini o‘zgartirmasdan test fixture’lar real
`auth_version=0` holatiga moslandi. Bundan tashqari, wheat xarid testlari
boshlang‘ich zaxira formasining tokenini xarid formasiga tegishli token o‘rniga
olayotgan edi; token endi xarid formasi doirasidan olinadi. Production va
Windows service testlari interpreter yo‘lini `.venv`ga qattiq bog‘lamaslik
uchun `sys.executable`dan foydalanadi. Ilovaning hisob-kitob yoki xavfsizlik
kodi o‘zgartirilmadi; assertionlar pasaytirilmadi va testlar skip qilinmadi.

`test_linux_wsgi` Linux deployment shartlarini va WSGI konfiguratsiyasini
tekshiradi, ammo testlar haqiqiy Linux hostida bajarilmadi. Linux serverga
joylashtirishdan avval Linux CI yoki serverdagi staging smoke-testini ham
bajarish lozim. Windows service testlari Windows platformasiga xos.
Testlarda ishlatilgan 127.0.0.1:5052 va :5053 serverlari to‘xtatilgan; bu
testlar production serverni, domenni yoki DNS’ni ishga tushirmadi/o‘zgartirmadi.

## Go-live oldi tekshiruv ro‘yxati

- [ ] Server OS, region, administrator, access recovery va support siyosati
      tasdiqlandi.
- [ ] Code release hash/versiyasi va Python dependency lock qayd etildi.
- [ ] Paket bazasining yangi deployment nusxasi olindi, source saqlanib qoldi;
      DB hash va SQLite integrity/FK tekshirildi.
- [ ] Biznes egasi boshlang‘ich ma’lumotlar nol qoladimi yoki qanday tartibda
      tasdiqlanib kiritilishini yozma tasdiqladi.
- [ ] `user_auth_versions` mavjud; 4 account parollari alohida, xavfsiz
      provision qilindi va barcha rollar tekshirildi.
- [ ] Maxfiy kalit, service account, file ACL, host firewall va SSH/MFA
      tekshirildi.
- [ ] Nginx, Gunicorn/systemd yoki Windows service faqat localhost upstream
      bilan tekshirildi.
- [ ] DNS egasi va `tortkoldon.uz` yozuvlari tasdiqlandi; HTTPS/sertifikat
      yangilanishi va public security headers tekshirildi.
- [ ] Off-site encrypted backup, failure alert, retention, restore drill va
      RPO/RTO tasdiqlandi.
- [ ] Login/CSRF/rate-limit, telefon UI, 4 role, savdo→qarz/kassa→ombor,
      25/50 kg qoplar, oylik 7-list Excel va audit hisobotlar staging’da
      sinovdan o‘tdi.
- [ ] Yuqorida sanalgan test failure/error’lar triage qilindi yoki tegishli
      mas’ul tomonidan yozma ravishda qabul qilindi.
- [ ] Maintenance oynasi, rollback egasi va incident aloqa kanali belgilandi.

Faqat barcha bandlar bajarilib, biznes egasi go-live’ni tasdiqlagandan so‘ng
DNS’ni public serverga ulang. Bu README yoki ZIP deploy’ni avtomatik
boshlamaydi.
