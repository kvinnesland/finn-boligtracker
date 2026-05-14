# Finn.no Boligtracker

## Installasjon

```bash
pip install -r requirements.txt
```

## Konfig

1. Kopier `.env.example` til `.env`
2. Fyll inn alle verdier i `.env`

```bash
copy .env.example .env
```

## Kjøring

```bash
python finn_tracker.py
```

Kjør manuelt kl. 08:00 og 16:00.

## Hva skriptet gjør

1. Henter alle annonser fra Finn.no med dine filtre
2. Sjekker Airtable — ny annonse settes inn, eksisterende oppdateres
3. Logger alle endringer i Changes-tabellen
4. Annonser som forsvinner → søker salgssum på eiendomsverdi.no
5. Sender e-post oppsummering

## Feilsøking

- Sjekk at alle env-variabler er satt i `.env`
- Finn.no kan endre HTML-struktur — sjekk loggene hvis ingen annonser hentes
- Gmail: bruk app-passord, ikke vanlig passord
