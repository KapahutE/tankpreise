# Tankpreis-Monitor mit Bases

Feste Orte („Bases“) stehen in `bases.json` – aktuell **Base F** (Uhingen), **Base H** (Leinfelden) **Homebase Sailers** (Metzingen) und **Homebase M&J** (Wernau am Neckar), je 25 km. Für sie werden alle 15 Minuten Preise gesammelt und ausgewertet, inklusive der Uhrzeiten, zu denen Preise erhöht oder gesenkt werden. In der App lassen sich Ort (Base, eigener Standort, Ortssuche) und Radius (1–25 km) frei wählen; außerhalb der Bases zeigt die App Live-Preise, wenn unter ⚙ ein Tankerkönig-Schlüssel hinterlegt ist.


Ruft **stündlich** die Spritpreise (Super E5, E10, Diesel) aller Tankstellen im Umkreis von Uhingen ab, speichert sie und erstellt **jeden Montag** eine Wochenauswertung. Alles läuft kostenlos bei **GitHub**, auch wenn dein iPhone aus ist. Auf dem iPhone bedienst du es über eine **Web-App auf dem Home-Bildschirm**.

Datenquelle: [Tankerkönig-API](https://creativecommons.tankerkoenig.de/) (amtliche Daten der Markttransparenzstelle MTS-K, CC BY 4.0).

## Was die iPhone-App zeigt

- die gerade günstigste Tankstelle, mit Knopf „Route starten“ (Apple Karten)
- alle Tankstellen nach Preis sortiert, umschaltbar zwischen E5, E10 und Diesel
- einen Tipp zur besten Tankzeit und ob sich Tanken gerade lohnt. Tipps, Überschriften und das „Fazit vo dr Woch“ im Wochenreport sind standardmäßig auf Schwäbisch; Zahlen, Diagramme und Hinweise bleiben hochdeutsch (unter ⚙ „Auf Schwäbisch“ abschaltbar)
- **„Lohnt sich am meisten“**: die günstigste Tankfüllung inklusive Hin- und Rückfahrt (Fahrstrecke ≈ 1,3 × Luftlinie; Tankmenge und Verbrauch unter ⚙ einstellbar), dazu die Sortierung „Inkl. Anfahrt“
- den **12-Uhr-Hinweis** mit Countdown: Erhöhungen gibt es nur zwischen 12 und 13 Uhr (aus den Daten erkannt), danach sinken die Preise bis zum Abend
- den Preisverlauf der letzten 7 Tage und die Abweichung nach Uhrzeit
- eine Erinnerung, bevor der GitHub-Zugangsschlüssel abläuft (Ablaufdatum unter ⚙ eintragen)
- die Wochenreports
- den Knopf ↻ für einen **sofortigen Abruf**, und in den Einstellungen „Wochenreport jetzt erstellen“

## Einrichtung (einmalig, ca. 10 Minuten, geht auch komplett am iPhone in Safari)

1. **Tankerkönig-Schlüssel holen:** Auf https://onboarding.tankerkoenig.de/ registrieren. Der API-Key kommt per E-Mail.
2. **GitHub-Konto** anlegen (kostenlos), falls noch keins vorhanden: https://github.com/signup
3. **Repository anlegen:** Diesen Ordner als **öffentliches** Repository namens `tankpreise` hochladen (Branch `main`). GitHub Pages ist im kostenlosen Tarif nur für öffentliche Repositories verfügbar. Die Tankpreise sind ohnehin öffentliche Daten, und der API-Key bleibt geheim (siehe Schritt 4).
4. **API-Key hinterlegen:** im Repository unter *Settings → Secrets and variables → Actions → New repository secret*
   - Name: `TANKERKOENIG_API_KEY`
   - Secret: dein Tankerkönig-Key
5. **Webseite einschalten:** *Settings → Pages → Build and deployment → Source:* **GitHub Actions**
6. **Erster Lauf:** *Actions → Tankpreise → Run workflow → Run workflow*. Nach 1–2 Minuten ist die App erreichbar unter
   `https://DEIN-BENUTZERNAME.github.io/tankpreise/`
7. **Auf dem iPhone:** Die Adresse in **Safari** öffnen → Teilen-Symbol → **„Zum Home-Bildschirm“**. Danach startet die App wie eine normale App.
8. **Optional: Knopf ↻ „Jetzt abrufen“ freischalten.**
   - Unter https://github.com/settings/personal-access-tokens/new einen *Fine-grained token* erstellen:
     - Repository access: nur `tankpreise`
     - Permissions → **Actions: Read and write**
   - In der App auf ⚙ tippen und den Schlüssel einfügen. Er wird nur auf deinem iPhone gespeichert.

Danach läuft alles automatisch:
- Abruf etwa alle 15 Minuten: Der Zeitplan startet alle 5 Minuten, abgerufen wird frühestens 13 Minuten nach dem letzten Abruf. So fängt der nächste Termin auf, wenn GitHub einen Lauf auslässt.
- Wochenreport jeden Montag früh für die Vorwoche; fällt der Montagslauf aus, holt ihn der nächste Abruf nach
- Die App liest die Daten direkt aus dem Repository; die frühere Webseite dieses Repositorys leitet nur noch auf die App um (`weiterleitung.yml`)

Schlägt ein Lauf fehl, etwa wegen eines falschen API-Keys, schickt GitHub dir eine E-Mail.

## Dateien

| Pfad | Inhalt |
|---|---|
| `tankpreise.py` | Programm: Abruf, Speicherung, Auswertung (nur Python-Standardbibliothek) |
| `.github/workflows/tankpreise.yml` | Zeitplan bei GitHub (stündlich + wöchentlich) |
| `data/changes-JJJJ-MM.csv` | Änderungsprotokoll: eine Zeile, wenn sich Preis oder Öffnungsstatus einer Tankstelle ändert (Monatsanfang: kompletter Stand) |
| `data/polls-JJJJ-MM.txt` | Zeitpunkte aller Abrufe |
| `data/state.json` | letzter Stand aller Tankstellen |
| `data/prices-JJJJ-MM.csv` | ältere Daten im früheren Format (ein kompletter Stand je Abruf), werden weiter gelesen |
| `bases.json` | Feste Orte mit Mittelpunkt und Radius (max. 25 km) |
| `data/stations.json` | Stammdaten der Tankstellen |
| `data/opening_times.json` | Öffnungszeiten (werden wöchentlich und für neue Tankstellen abgerufen) |
| `docs/index.html` | die iPhone-Web-App |
| `docs/reports/<Base>/` | Wochenreports je Base (HTML + Markdown) |
| `docs/data/bases.json`, `docs/data/<Base>/*.json` | aufbereitete Daten für die App |

## Lokal auf einem PC/Raspberry Pi (Alternative ohne GitHub)

```
cp config.example.ini config.ini     # API-Key eintragen
python3 tankpreise.py fetch          # einmal abrufen
python3 tankpreise.py report         # Wochenreport der letzten 7 Tage
python3 tankpreise.py export         # Daten für die App aktualisieren
python3 tankpreise.py run            # Dauerbetrieb (stündlich + Montags-Report)
python3 tankpreise.py demo           # Testdaten erzeugen (nur zum Ausprobieren!)
```

Für den Dauerbetrieb gibt es `crontab.example` und `tankpreise.service` (systemd). Die App lässt sich lokal mit `python3 -m http.server -d docs` ansehen.

## Hinweise

- Laut Tankerkönig-Nutzungsbedingungen sind automatische Abrufe höchstens alle 5 Minuten erlaubt; stündlich ist unproblematisch. Der Suchradius beträgt höchstens 25 km und ist in `config.example.ini` bzw. `tankpreise.py` einstellbar (Standard: 10 km).
- Preise ändern sich oft mehrmals pro Stunde. Ein stündlicher Abruf erfasst deshalb nicht jede Änderung, für Tageszeit-Muster reicht er aber gut aus.
- Die „beste Tankzeit“ ist die mittlere Abweichung jeder Stunde vom Tagesmittel der jeweiligen Tankstelle über die letzten 7 Tage.

## Preisprognose

Bei jedem Abruf lernt ein kleines Modell aus den Daten der letzten 21 Tage (nur Python-Bordmittel, kein Fremddienst):

- Für jede Tankstelle und Sorte wird gelernt, **wie sich der Preis je Viertelstunde des Tages typischerweise ändert**
  (z. B. Sprung um 12 Uhr, Senkungen am Nachmittag). Mit wenigen Daten stützt sich das auf das Muster der Marke bzw. aller Tankstellen.
- **Prognose** = letzter Preis + Summe der typischen Änderungen bis zur Zielzeit. Ein allgemeiner Trend steckt darin automatisch.
- **Rückblick-Test:** Das Modell lernt ohne die letzten 1–2 **ganzen** Tage (inklusive 12-Uhr-Sprung) und sagt diese dann alle 2 Stunden
  für 1–12 Stunden voraus. Angezeigt wird die Prognose nur, wenn sie die Annahme „Preis bleibt gleich“ um mindestens 10 % schlägt;
  der gemessene Fehler erscheint in der App als Band (± ct).
- Ergebnis je Base in `docs/data/<Base>/forecast.json`. Frühestens nach 3 Tagen Daten verfügbar.
