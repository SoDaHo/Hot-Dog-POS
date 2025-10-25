# Hot‑Dog POS

Kleines, lokales Point‑of‑Sale System für Imbissstände / kleine Läden.  
Frontend: HTML/JS, Backend: Python (Flask), DB: SQLite.  
Kurz: Touch‑freundliche Kasse, Benutzer/PIN, Artikelverwaltung, Zahlarten, Admin‑Übersicht, CSV‑Export.

## Quickstart — lokal
1. Klonen:
   ```bash
   git clone https://github.com/SoDaHo/Hot-Dog-POS.git
   cd Hot-Dog-POS
   ```
2. (Optional) virtuelles Env:
   ```bash
   python -m venv venv
   source venv/bin/activate   # Windows: venv\Scripts\activate
   ```
3. Abhängigkeiten:
   ```bash
   pip install -r requirements.txt
   ```
4. Starten:
   ```bash
   export POS_SECRET="change_me"
   export POS_DB="sales.db"
   python POS.py
   ```
5. Öffnen: http://localhost:8000

## Docker
Dockerfile (minimal):
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -r requirements.txt
ENV POS_SECRET="please_change_me" POS_DB="/data/sales.db"
VOLUME /data
EXPOSE 8000
CMD ["python", "POS.py"]
```
Build & Run:
```bash
docker build -t hot-dog-pos .
docker run -p 8000:8000 -v "$(pwd)/data":/data --env POS_SECRET="change_me" hot-dog-pos
```

## Konfiguration
- POS_SECRET — Flask session secret (unbedingt ändern in Produktion)
- POS_DB — Pfad zur SQLite DB (Default: sales.db)
- CURRENCY — in POS.py als Konstante gesetzt (z. B. "CHF")

## Hinweise
- Beim ersten Start werden DB‑Tabellen erstellt und Beispiel‑Daten (Artikel, Zahlarten, Nutzer) angelegt.
- SQLite ist für kleine Setups gedacht; in Produktion auf HTTPS/TLS und sichere PINs achten.

## Lizenz
MIT (abhängigkeitskompatibel; einzelne Abhängigkeiten unterliegen ggf. ihren eigenen, ebenfalls permissiven Lizenzen).