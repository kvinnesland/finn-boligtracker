#!/usr/bin/env python3
"""
Finn.no Boligtracker v1.1
Scraper → Airtable database + e-post oppsummering
Bruker Firecrawl for å omgå geo-blokkering fra GitHub Actions
"""

import os, re, time, json, smtplib, logging
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, Dict, List

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pyairtable import Api

load_dotenv()

# ── KONFIG ───────────────────────────────────────────────────────────────────

FINN_SEARCH_URL = (
    "https://www.finn.no/realestate/homes/search.html"
    "?radius=7000"
    "&lat=69.67389373629754"
    "&lon=18.947615794412513"
    "&price_collective_to=9000000"
    "&facilities=23"
    "&area_from=120"
)

AT_API_KEY   = os.getenv("AIRTABLE_API_KEY")
AT_BASE_ID   = os.getenv("AIRTABLE_BASE_ID")
AT_PROPS_TBL = os.getenv("AIRTABLE_PROPERTIES_TABLE_ID")
AT_CHG_TBL   = os.getenv("AIRTABLE_CHANGES_TABLE_ID")

EMAIL_FROM   = os.getenv("EMAIL_SENDER")
EMAIL_PASS   = os.getenv("EMAIL_PASSWORD")
EMAIL_TO     = [r.strip() for r in os.getenv("EMAIL_RECIPIENT", "").split(",") if r.strip()]
SMTP_HOST    = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT    = int(os.getenv("EMAIL_SMTP_PORT", "587"))

FIRECRAWL_KEY = os.getenv("FIRECRAWL_API_KEY")
FIRECRAWL_URL = "https://api.firecrawl.dev/v1/scrape"

REQUEST_DELAY = 2.0  # sekunder mellom requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "nb-NO,nb;q=0.9,no;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("finn")


# ── HJELPEFUNKSJONER ─────────────────────────────────────────────────────────

def parse_int(text: str) -> Optional[int]:
    if not text:
        return None
    cleaned = re.sub(r"[^0-9]", "", str(text))
    return int(cleaned) if cleaned else None


def fetch_via_firecrawl(url: str, retries: int = 2) -> Optional[BeautifulSoup]:
    """Henter side via Firecrawl med full JS-rendering."""
    for attempt in range(retries):
        try:
            resp = requests.post(
                FIRECRAWL_URL,
                headers={
                    "Authorization": f"Bearer {FIRECRAWL_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "url": url,
                    "formats": ["html"],
                    "waitFor": 4000,
                    "onlyMainContent": False,
                },
                timeout=90,
            )
            resp.raise_for_status()
            data = resp.json()
            html = data.get("data", {}).get("html", "")
            if html:
                soup = BeautifulSoup(html, "html.parser")
                articles = soup.find_all("article")
                log.info(f"  Firecrawl: {len(html)} tegn HTML, {len(articles)} article-elementer")
                return soup
            log.warning(f"Firecrawl returnerte tom HTML for {url} — svar: {str(data)[:200]}")
        except Exception as e:
            log.warning(f"Firecrawl forsøk {attempt+1}/{retries} feilet: {url} — {e}")
            time.sleep(5)
    return None


def fetch_direct(url: str, retries: int = 2) -> Optional[BeautifulSoup]:
    """Direkte HTTP-henting — brukes for eiendomsverdi.no."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            log.warning(f"Direkte fetch forsøk {attempt+1}/{retries} feilet: {url} — {e}")
            time.sleep(4)
    return None


def fetch(url: str, retries: int = 2) -> Optional[BeautifulSoup]:
    """Bruker Firecrawl hvis nøkkel finnes, ellers direkte."""
    if FIRECRAWL_KEY:
        return fetch_via_firecrawl(url, retries)
    return fetch_direct(url, retries)

    log.error(f"Kunne ikke hente: {url}")
    return None


# ── FINN.NO SCRAPING ─────────────────────────────────────────────────────────

def get_search_listing_ids() -> List[Dict]:
    """Henter alle finn_id + URL fra søkesiden (alle sider)."""
    listings = []
    page = 1

    while True:
        url = FINN_SEARCH_URL + f"&page={page}"
        log.info(f"Søkeside {page}: {url}")
        soup = fetch(url)

        if not soup:
            log.error(f"Klarte ikke hente søkeside {page} — avslutter paginering")
            break

        # Finn.no bruker article-elementer med id="id-XXXXXXXX"
        articles = soup.select("article[id^='id-']")

        if not articles:
            # Fallback: prøv andre selektorer
            articles = soup.select("article[data-testid]") or soup.select("article")

        if not articles:
            log.info(f"Ingen flere annonser på side {page}")
            break

        found_on_page = 0
        for article in articles:
            # Hent finn_id fra article id
            raw_id = article.get("id", "")
            finn_id = re.sub(r"[^0-9]", "", raw_id)

            # Hent URL til annonsen
            link = (
                article.select_one("a[href*='finnkode']")
                or article.select_one("a[href*='/realestate/']")
                or article.find("a", href=True)
            )
            href = link["href"] if link else None
            if href and not href.startswith("http"):
                href = "https://www.finn.no" + href

            if finn_id:
                listings.append({"finn_id": finn_id, "listing_url": href})
                found_on_page += 1

        log.info(f"  Fant {found_on_page} annonser på side {page}")

        # Sjekk om det finnes neste side
        next_btn = (
            soup.select_one("a[aria-label='Neste side']")
            or soup.select_one("a[aria-label='Next page']")
            or soup.select_one("[data-testid='pagination-next-page']")
        )
        if not next_btn or not next_btn.get("href"):
            break

        page += 1
        time.sleep(REQUEST_DELAY)

    log.info(f"Totalt {len(listings)} annonser funnet")
    return listings


def get_listing_details(finn_id: str, url: str) -> Dict:
    """Scraper full informasjon fra en enkelt annonseside."""
    if not url:
        url = f"https://www.finn.no/realestate/homes/ad.html?finnkode={finn_id}"

    soup = fetch(url)
    if not soup:
        return {"finn_id": finn_id, "listing_url": url, "_error": True}

    data: Dict = {"finn_id": finn_id, "listing_url": url}

    # ── JSON-LD strukturert data (mest pålitelig) ─────────────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            ld = json.loads(script.string or "")
            if isinstance(ld, dict) and "@type" in ld:
                addr = ld.get("address", {})
                if isinstance(addr, dict):
                    parts = [
                        addr.get("streetAddress", ""),
                        addr.get("addressLocality", ""),
                    ]
                    data["address"] = ", ".join(p for p in parts if p)
                data["description"] = ld.get("description", "")[:3000]
                break
        except Exception:
            pass

    # ── Bilder ─────────────────────────────────────────────────────────────
    imgs = set()
    for img in soup.select("img[src*='images.finn']") + soup.select("img[data-src*='images.finn']"):
        src = img.get("src") or img.get("data-src", "")
        if src:
            # Hent høyoppløselig versjon
            src = re.sub(r"\?.*", "", src)
            imgs.add(src)
    data["images"] = ", ".join(imgs)

    # ── Nøkkelinfo fra definisjonslister (dl/dt/dd) ───────────────────────
    kv: Dict[str, str] = {}
    for dl in soup.select("dl"):
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            key = dt.get_text(strip=True).lower().strip(":")
            val = dd.get_text(strip=True)
            kv[key] = val

    # Prøv også tabellrader
    for tr in soup.select("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) >= 2:
            key = cells[0].get_text(strip=True).lower().strip(":")
            val = cells[1].get_text(strip=True)
            kv[key] = val

    # ── Map norske etiketter → databasefelt ──────────────────────────────
    label_map = {
        "prisantydning":        ("listing_price",   parse_int),
        "fellesgjeld":          ("collective_debt",  parse_int),
        "totalpris":            ("total_price",      parse_int),
        "pris per m²":          ("price_per_sqm",    parse_int),
        "pris per m2":          ("price_per_sqm",    parse_int),
        "primærrom":            ("primary_area_sqm", parse_int),
        "primærrom (p-rom)":    ("primary_area_sqm", parse_int),
        "bruksareal":           ("usable_area_sqm",  parse_int),
        "bruksareal (bra)":     ("usable_area_sqm",  parse_int),
        "tomteareal":           ("plot_size_sqm",    parse_int),
        "tomtestørrelse":       ("plot_size_sqm",    parse_int),
        "hageareale":           ("garden_size_sqm",  parse_int),
        "hage":                 ("garden_size_sqm",  parse_int),
        "hagestørrelse":        ("garden_size_sqm",  parse_int),
        "soverom":              ("bedrooms",         parse_int),
        "antall soverom":       ("bedrooms",         parse_int),
        "bad":                  ("bathrooms",        parse_int),
        "antall bad":           ("bathrooms",        parse_int),
        "byggeår":              ("year_built",       parse_int),
        "etasje":               ("floor",            str),
        "eiendomstype":         ("property_type",    str),
        "energimerking":        ("energy_rating",    lambda x: x[0].upper() if x else None),
        "energimerke":          ("energy_rating",    lambda x: x[0].upper() if x else None),
    }

    for label, (field, transform) in label_map.items():
        if label in kv:
            try:
                val = transform(kv[label])
                if val is not None:
                    data[field] = val
            except Exception:
                data[field] = kv[label]

    # ── Adresse fallback ─────────────────────────────────────────────────
    if not data.get("address"):
        h1 = soup.select_one("h1")
        if h1:
            data["address"] = h1.get_text(strip=True)

    # ── Megler ──────────────────────────────────────────────────────────
    for sel in ["[class*='realtor']", "[class*='broker']", "[class*='megler']", "[class*='agent']"]:
        section = soup.select_one(sel)
        if section:
            name_el = section.select_one("strong, b, [class*='name']")
            agency_el = section.select_one("p, [class*='company'], [class*='agency']")
            if name_el:
                data["realtor_name"] = name_el.get_text(strip=True)
            if agency_el:
                data["realtor_agency"] = agency_el.get_text(strip=True)
            break

    # ── Beskrivelse fallback ─────────────────────────────────────────────
    if not data.get("description"):
        for sel in ["[class*='description']", "[class*='ingress']", "[class*='body']"]:
            el = soup.select_one(sel)
            if el and len(el.get_text(strip=True)) > 50:
                data["description"] = el.get_text(strip=True)[:3000]
                break

    # ── Sist oppdatert på Finn ───────────────────────────────────────────
    page_text = soup.get_text()
    match = re.search(r"[Oo]ppdatert[:\s]+(\d{1,2}\.\d{1,2}\.\d{4})", page_text)
    if match:
        data["last_updated_finn"] = match.group(1)

    log.info(
        f"  Scraped {finn_id}: {data.get('address', '(ingen adresse)')} "
        f"— {data.get('listing_price', '?')} kr"
    )
    return data


# ── EIENDOMSVERDI.NO OPPSLAG ─────────────────────────────────────────────────

def find_sold_price(address: str) -> Dict:
    """Søker etter salgssum på eiendomsverdi.no."""
    if not address:
        return {}
    try:
        search_url = f"https://eiendomsverdi.no/adresse?query={requests.utils.quote(address)}"
        soup = fetch(search_url)
        if not soup:
            return {}

        # Prøv å finne salgssum
        for sel in [
            "[class*='sold-price']",
            "[class*='salgspris']",
            "[class*='sale-price']",
            "[class*='solgt']",
        ]:
            el = soup.select_one(sel)
            if el:
                price = parse_int(el.get_text())
                if price and price > 100_000:
                    date_el = el.find_next(string=re.compile(r"\d{4}"))
                    return {
                        "sold_price": price,
                        "sold_date": date_el.strip() if date_el else None,
                    }
    except Exception as e:
        log.warning(f"Eiendomsverdi feil for '{address}': {e}")
    return {}


# ── AIRTABLE ─────────────────────────────────────────────────────────────────

def get_tables():
    api = Api(AT_API_KEY)
    return api.table(AT_BASE_ID, AT_PROPS_TBL), api.table(AT_BASE_ID, AT_CHG_TBL)


def load_existing(props_table) -> Dict[str, Dict]:
    records = props_table.all()
    result = {}
    for rec in records:
        fid = str(rec["fields"].get("finn_id", ""))
        if fid:
            result[fid] = {"record_id": rec["id"], "fields": rec["fields"]}
    log.info(f"Lastet {len(result)} eksisterende poster fra Airtable")
    return result


def log_change(changes_table, finn_id: str, field: str, old, new):
    try:
        changes_table.create({
            "finn_id": str(finn_id),
            "field_name": field,
            "old_value": str(old) if old is not None else "",
            "new_value": str(new) if new is not None else "",
            "changed_at": datetime.utcnow().isoformat(),
        })
    except Exception as e:
        log.warning(f"Klarte ikke logge endring for {finn_id}/{field}: {e}")


TRACKED_FIELDS = [
    "listing_price", "collective_debt", "total_price", "price_per_sqm",
    "primary_area_sqm", "usable_area_sqm", "plot_size_sqm", "garden_size_sqm",
    "bedrooms", "bathrooms", "year_built", "floor", "property_type",
    "energy_rating", "realtor_name", "realtor_agency", "images",
    "last_updated_finn", "status",
]


# ── HOVED-RUN ─────────────────────────────────────────────────────────────────

def run():
    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    log.info(f"{'='*50}")
    log.info(f"Finn Tracker kjøring startet: {run_ts}")
    log.info(f"{'='*50}")

    summary = {
        "run_ts": run_ts,
        "new": [],
        "updated": [],
        "sold_or_delisted": [],
        "errors": [],
    }

    # Koble til Airtable
    props_table, changes_table = get_tables()
    existing = load_existing(props_table)

    # Steg 1: Hent alle annonse-IDer fra søkeresultater
    search_results = get_search_listing_ids()
    if not search_results:
        log.error("Ingen søkeresultater — sjekk nettverkstilkobling og Finn.no URL")
        summary["errors"].append("Finn.no søk returnerte ingen resultater")
        send_email(summary)
        return

    scraped_ids = set()

    # Steg 2: Scrape hver enkelt annonse
    for i, item in enumerate(search_results, 1):
        finn_id = str(item["finn_id"])
        url = item["listing_url"]
        scraped_ids.add(finn_id)
        log.info(f"[{i}/{len(search_results)}] Behandler {finn_id}")
        time.sleep(REQUEST_DELAY)

        details = get_listing_details(finn_id, url)

        if details.get("_error"):
            summary["errors"].append(f"Kunne ikke hente annonse {finn_id} ({url})")
            continue

        today = date.today().isoformat()

        if finn_id not in existing:
            # NY annonse
            details["status"] = "active"
            details["first_seen_date"] = today
            details["last_seen_date"] = today
            clean = {k: v for k, v in details.items() if v is not None and not k.startswith("_")}
            try:
                props_table.create(clean)
                summary["new"].append(details)
                log.info(f"  ✅ NY: {finn_id} — {details.get('address', '?')}")
            except Exception as e:
                log.error(f"  Feil ved innsetting av {finn_id}: {e}")
                summary["errors"].append(f"Klarte ikke lagre ny annonse {finn_id}: {e}")
        else:
            # EKSISTERENDE — sjekk for endringer
            stored = existing[finn_id]["fields"]
            record_id = existing[finn_id]["record_id"]
            updates: Dict = {"last_seen_date": today}
            changed_fields = []

            for field in TRACKED_FIELDS:
                old_val = stored.get(field)
                new_val = details.get(field)
                if new_val is not None and str(old_val).strip() != str(new_val).strip():
                    updates[field] = new_val
                    log_change(changes_table, finn_id, field, old_val, new_val)
                    changed_fields.append({"field": field, "old": old_val, "new": new_val})

            try:
                props_table.update(record_id, updates)
                if changed_fields:
                    summary["updated"].append({
                        "finn_id": finn_id,
                        "address": stored.get("address", finn_id),
                        "changes": changed_fields,
                    })
                    log.info(f"  🔄 ENDRET: {finn_id} — {len(changed_fields)} felt endret")
                else:
                    log.info(f"  ✓ Ingen endring: {finn_id}")
            except Exception as e:
                log.error(f"  Feil ved oppdatering av {finn_id}: {e}")
                summary["errors"].append(f"Klarte ikke oppdatere {finn_id}: {e}")

    # Steg 3: Håndter fjernede annonser
    active_in_db = {
        fid: rec for fid, rec in existing.items()
        if rec["fields"].get("status") == "active"
    }
    removed_ids = set(active_in_db.keys()) - scraped_ids
    log.info(f"Fjernede annonser: {len(removed_ids)}")

    for finn_id in removed_ids:
        rec = active_in_db[finn_id]
        record_id = rec["record_id"]
        address = rec["fields"].get("address", finn_id)
        listing_price = rec["fields"].get("listing_price", 0) or 0

        log.info(f"  🔍 FJERNET: {finn_id} — {address} — søker salgssum...")
        time.sleep(REQUEST_DELAY)
        sold_data = find_sold_price(address)

        if sold_data.get("sold_price"):
            diff = sold_data["sold_price"] - listing_price
            updates = {
                "status": "sold",
                "sold_price": sold_data["sold_price"],
                "sold_date": sold_data.get("sold_date", ""),
                "price_difference": diff,
                "last_seen_date": date.today().isoformat(),
            }
            log_change(changes_table, finn_id, "status", "active", "sold")
            summary["sold_or_delisted"].append({
                "finn_id": finn_id, "address": address,
                "sold_price": sold_data["sold_price"],
                "listing_price": listing_price,
                "diff": diff,
                "status": "sold",
            })
            log.info(f"    SOLGT for {sold_data['sold_price']:,} kr")
        else:
            updates = {
                "status": "delisted",
                "last_seen_date": date.today().isoformat(),
            }
            log_change(changes_table, finn_id, "status", "active", "delisted")
            summary["sold_or_delisted"].append({
                "finn_id": finn_id, "address": address, "status": "delisted"
            })
            log.info(f"    Ingen salgssum funnet — merket som delisted")

        try:
            props_table.update(record_id, updates)
        except Exception as e:
            log.error(f"  Feil ved oppdatering av fjernet annonse {finn_id}: {e}")

    # Steg 4: Send e-post oppsummering
    send_email(summary)
    log.info(f"{'='*50}")
    log.info(f"Kjøring ferdig. Nye: {len(summary['new'])}, "
             f"Endret: {len(summary['updated'])}, "
             f"Fjernet: {len(summary['sold_or_delisted'])}, "
             f"Feil: {len(summary['errors'])}")
    log.info(f"{'='*50}")


# ── E-POST ────────────────────────────────────────────────────────────────────

def fmt_price(val) -> str:
    try:
        return f"{int(val):,} kr".replace(",", "\u202f")
    except Exception:
        return str(val) if val else "?"


def send_email(summary: Dict):
    lines = [
        f"<html><body>",
        f"<h2 style='color:#1a1a2e'>🏡 Finn.no Boligtracker</h2>",
        f"<p style='color:#666'>Kjøring: {summary['run_ts']}</p><hr>",
    ]

    # Nye annonser
    if summary["new"]:
        lines.append(f"<h3>🆕 Nye annonser ({len(summary['new'])})</h3><ul>")
        for p in summary["new"]:
            price = fmt_price(p.get("listing_price"))
            sqm = p.get("primary_area_sqm", "?")
            url = p.get("listing_url", "#")
            addr = p.get("address", p.get("finn_id", "?"))
            lines.append(
                f"<li><b>{addr}</b><br>"
                f"Prisantydning: {price} &nbsp;|&nbsp; {sqm} m²<br>"
                f"<a href='{url}'>Se annonse på Finn.no</a></li><br>"
            )
        lines.append("</ul>")
    else:
        lines.append("<p>Ingen nye annonser.</p>")

    # Oppdaterte annonser
    if summary["updated"]:
        lines.append(f"<h3>🔄 Oppdaterte annonser ({len(summary['updated'])})</h3><ul>")
        for p in summary["updated"]:
            lines.append(f"<li><b>{p['address']}</b><ul>")
            for ch in p["changes"]:
                lines.append(f"<li>{ch['field']}: <s>{ch['old']}</s> → <b>{ch['new']}</b></li>")
            lines.append("</ul></li>")
        lines.append("</ul>")
    else:
        lines.append("<p>Ingen oppdateringer.</p>")

    # Solgt / fjernet
    if summary["sold_or_delisted"]:
        lines.append(f"<h3>🏁 Solgt / fjernet ({len(summary['sold_or_delisted'])})</h3><ul>")
        for p in summary["sold_or_delisted"]:
            if p["status"] == "sold":
                diff = p.get("diff", 0)
                diff_str = f"+{fmt_price(diff)}" if diff >= 0 else fmt_price(diff)
                lines.append(
                    f"<li><b>{p['address']}</b><br>"
                    f"Solgt for {fmt_price(p.get('sold_price'))} "
                    f"(prisantydning: {fmt_price(p.get('listing_price'))}, diff: {diff_str})</li><br>"
                )
            else:
                lines.append(
                    f"<li><b>{p['address']}</b> — Fjernet fra Finn.no "
                    f"(ingen salgssum funnet)</li>"
                )
        lines.append("</ul>")

    # Feil
    if summary["errors"]:
        lines.append(f"<h3>⚠️ Feil ({len(summary['errors'])})</h3><ul>")
        for e in summary["errors"]:
            lines.append(f"<li>{e}</li>")
        lines.append("</ul>")

    if not any([summary["new"], summary["updated"], summary["sold_or_delisted"]]):
        lines.append("<p><i>Ingen endringer siden forrige kjøring.</i></p>")

    lines.append("</body></html>")
    html = "\n".join(lines)

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Finn.no Bolig — {summary['run_ts']}"
        msg["From"] = EMAIL_FROM
        msg["To"] = ", ".join(EMAIL_TO)
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_PASS)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        log.info("E-post sendt ✅")
    except Exception as e:
        log.error(f"Klarte ikke sende e-post: {e}")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()
