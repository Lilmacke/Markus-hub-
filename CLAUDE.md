# garmin-hub

Markus personliga "hub": ett Python-script som samlar Garmin-träningsdata, aktiekurser,
kost och en 30-dagars-utmaning till en enda sida, byggd och serverad live av server.py.

## Komponenter

- **hub.py** — datainsamlingsscriptet. Loggar in på Garmin Connect (`garminconnect`),
  hämtar aktiekurser (`yfinance`) för listan i `BRANSCHER`, läser Strong/TikTok-exporter,
  räknar ut nyckeltal och sparar allt till `historik.csv`/`aktier_cache.json`/
  `utmaning_status.json`. Bygger **ingen** HTML själv längre — det gör server.py, live,
  varje sidladdning. Kör med `py hub.py` (via schemaläggning en gång i timmen).
- **server.py** — Flask-server (port 5000), den enda platsen sidan faktiskt visas.
  Bygger sidan on-the-fly via `hub.bygg_html()` från samma cachade filer som hub.py
  skriver, och gör "30 dagars-utmaning"-checkboxarna, kost-inmatningen och "Fråga din
  hub" klickbara direkt. chdir:ar till sin egen mapp vid start, så den funkar oavsett
  varifrån den startas. Endpoints: `/` (sidan), `/manifest.json` (PWA), `POST /bocka`
  (checkbox), `POST /kost` (kostfält), `POST /fraga` (fri fråga, se nedan),
  `POST /uppdatera` (kör hub.py på nytt, blockerande, timeout 240s).
- **"Fråga din hub"** — chattruta på sidan (`bygg_fraga_html()` i hub.py). Skickar frågan
  till `/fraga`, som via `hub.svara_pa_fraga()` bygger ihop kontext (senaste
  träningspass, daglig historik, kost, utmaning) med `hub.fraga_data_sammanfattning()`
  och frågar Claude (`AI_MODELL`). Kräver att AI-nyckeln finns i Credential Manager
  (se nedan).
- Vid fel i den schemalagda körningen (t.ex. Garmin-inloggning som misslyckas) skickas en
  Windows-notis via `skicka_notis()` (paketet `winotify`) istället för att felet bara
  hamnar tyst i `log.txt`.
- **run_hub.bat** — körs av schemaläggning (Schemalagda aktiviteter, uppgiften
  "GarminHub"), anropar `py hub.py` och loggar till `debug_log.txt`/`log.txt`.
- **run_server.bat** + startmappen — server.py startas automatiskt vid inloggning via
  en `.bat`-fil i `shell:startup`
  (`C:\Users\marku\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\GarminHub-Server.bat`)
  som anropar `run_server.bat`. Loggar till `server_log.txt`. (Ett `schtasks /create`-försök
  för samma sak gav "Åtkomst nekad" från Claude Code-sessionen — startmappen funkade
  utan extra rättigheter.)
- Sidan nås över Tailscale (`http://<tailscale-ip>:5000`) förutom lokalt/localhost.
  Det finns ingen statisk `min_hub.html` längre — den skapade förvirring (föråldrad,
  och alla interaktiva delar var trasiga öppnad som `file://`) och togs bort helt
  2026-08-14, tillsammans med en gammal kopia i OneDrive som synkades dit "för
  mobilåtkomst". Mobilåtkomst sker nu via Tailscale-adressen i telefonens webbläsare.

## Data / state (lokal, mestadels inte i git — se nedan)

- Garmin-inloggning och Anthropic API-nyckel ligger i **Windows Credential Manager**
  (via `keyring`-paketet, service `garmin-hub`), inte i klartext på disk. `garmin_konto.json`
  och `ai_nyckel.json` migreras dit automatiskt engångsvis om de hittas (döps sedan om till
  `.migrerad`) — se `_migrera_garmin_konto()`/`_migrera_ai_nyckel()` i hub.py. Genomfört
  2026-08-14.
- `historik.csv` — daglig historik av nyckeltal (i git)
- `raw_debug.json` — senaste rådata från Garmin (i git)
- `aktier_cache.json`, `ai_analys_cache.json` — cachar för att undvika onödiga API-anrop
- `vecko_analys_cache.json` — cachad AI-veckoanalys ("Veckans mönster" på hemskärmen),
  genereras högst en gång per ISO-vecka av hub.py:s schemalagda körning
  (`hämta_eller_generera_veckoanalys`); server.py läser bara cachen, genererar aldrig
  själv (`hämta_veckoanalys_text`)
- `kost.db` (SQLite) — kostloggning, ersatte tidigare `kost_status.json`
- `utmaning_status.json` — dagens/tidigare dagars status för 30-dagars-utmaningen
- `strong-export/`, `tiktok-export/` — manuellt exporterade CSV:er som läses in
- `server_log.txt` — output från server.py när den körs headless (via `pythonw.exe`,
  startad vid inloggning)

## Viktigt att veta om git

`hub.py` och `server.py` var tidigare gitignorade och hade aldrig committats — bara
`.gitignore`, `historik.csv` och `raw_debug.json` trackades, och de täta "automatisk
uppdatering"-commiten var bara data-snapshots. Det ändrades 2026-08-14: källkoden är
nu committad och pushad (commit `19619f7`), så det finns numera en riktig historik/
backup av koden också.

## Språk och stil

Kod, variabelnamn och kommentarer är på svenska (t.ex. `hämta_garmin_data`,
`bygg_html`, `spara_kost_fält`) — följ samma konvention vid ändringar.
