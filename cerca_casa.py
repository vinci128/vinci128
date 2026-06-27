#!/usr/bin/env python3
"""
Cerca casa a Roma - Zona Torrino
Budget: 350.000€ (incluse eventuali ristrutturazioni)
Requisiti: minimo 3 locali

Uso:
    python3 cerca_casa.py             # scraping completo
    python3 cerca_casa.py --links     # stampa solo i link di ricerca (apri nel browser)

Dipendenze:
    pip install requests beautifulsoup4 lxml
"""

import re
import sys
import time
import json
import argparse
from dataclasses import dataclass, field
from typing import Optional
import requests
from bs4 import BeautifulSoup

# ─── Configurazione ────────────────────────────────────────────────────────────

BUDGET_TOTALE = 350_000          # € budget massimo tutto incluso
MARGINE_TRATTATIVA = 0.12        # 12% sopra budget per annunci trattabili
PREZZO_MAX_RICERCA = int(BUDGET_TOTALE * (1 + MARGINE_TRATTATIVA))  # 392.000€

# Costi di ristrutturazione per mq (€/mq)
COSTO_RISTRUTTURAZIONE = {
    "leggera": 350,   # tinteggiatura, piccoli interventi
    "media":   700,   # bagni, cucina, impianti parziali
    "completa": 1100, # tutto da rifare (impianti + finiture)
}

LOCALI_MINIMI = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ─── Struttura dati annuncio ────────────────────────────────────────────────────

@dataclass
class Annuncio:
    titolo: str
    prezzo: int
    locali: Optional[int]
    mq: Optional[int]
    piano: Optional[str]
    indirizzo: str
    url: str
    fonte: str
    condizione: str = "unknown"          # "ottimo", "ristrutturare", "unknown"
    costo_ristrutturazione: int = 0
    costo_totale: int = 0
    note: list = field(default_factory=list)

    def __post_init__(self):
        self.costo_totale = self.prezzo + self.costo_ristrutturazione


# ─── Analisi condizione ─────────────────────────────────────────────────────────

PAROLE_RISTRUTTURARE = [
    "da ristrutturare", "da ristrut", "abitabile", "da rinnovare",
    "necessita lavori", "da riqualificare", "originale", "anni '",
    "da sistemare", "da rimodernare", "parzialmente ristrutturato",
    "lavori", "potenzialità",
]

PAROLE_OTTIMO = [
    "ristrutturato", "ottimo stato", "ottime condizioni", "nuovo",
    "appena ristrutturato", "recente", "moderno", "chiavi in mano",
    "perfetto stato", "completo di tutto", "arredato", "luxury",
    "pregio", "di lusso", "finemente ristrutturato",
]


def analizza_condizione(testo: str) -> str:
    t = testo.lower()
    punteggio_rif = sum(1 for p in PAROLE_RISTRUTTURARE if p in t)
    punteggio_ok = sum(1 for p in PAROLE_OTTIMO if p in t)
    if punteggio_rif > punteggio_ok:
        return "ristrutturare"
    if punteggio_ok > 0:
        return "ottimo"
    return "unknown"


def stima_costo_ristrutturazione(mq: Optional[int], condizione: str) -> int:
    if condizione != "ristrutturare":
        return 0
    superficie = mq if mq else 75  # stima se mancante
    # Per semplicità usiamo costo medio (ristrutturazione media)
    return superficie * COSTO_RISTRUTTURAZIONE["media"]


# ─── Utility di parsing ─────────────────────────────────────────────────────────

def estrai_numero(testo: str) -> Optional[int]:
    nums = re.findall(r"[\d\.]+", testo.replace(",", "."))
    for n in nums:
        try:
            val = int(float(n.replace(".", "")))
            if val > 10:
                return val
        except ValueError:
            pass
    return None


def normalizza_prezzo(testo: str) -> Optional[int]:
    t = testo.replace(".", "").replace(",", "").replace(" ", "")
    m = re.search(r"(\d{4,9})", t)
    if m:
        return int(m.group(1))
    return None


# ─── Scraper Immobiliare.it ─────────────────────────────────────────────────────

IMMOBILIARE_URLS = [
    # Torrino
    f"https://www.immobiliare.it/vendita-case/torrino-roma/?locali=3,4,5,6plus&prezzoMassimo={PREZZO_MAX_RICERCA}",
    # Pagina 2
    f"https://www.immobiliare.it/vendita-case/torrino-roma/?locali=3,4,5,6plus&prezzoMassimo={PREZZO_MAX_RICERCA}&pag=2",
    # Torrino Mezzocammino (zona adiacente)
    f"https://www.immobiliare.it/vendita-case/torrino-mezzocammino-roma/?locali=3,4,5,6plus&prezzoMassimo={PREZZO_MAX_RICERCA}",
    # EUR Torrino
    f"https://www.immobiliare.it/vendita-case/eur-roma/?locali=3,4,5,6plus&prezzoMassimo={PREZZO_MAX_RICERCA}",
]


def scrape_immobiliare() -> list[Annuncio]:
    annunci = []
    for url in IMMOBILIARE_URLS:
        try:
            resp = SESSION.get(url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [!] Immobiliare.it - errore: {e}", file=sys.stderr)
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        # Prova prima il JSON-LD (dati strutturati)
        annunci_pagina = _parse_immobiliare_json(soup, url)
        if not annunci_pagina:
            # Fallback: parsing HTML
            annunci_pagina = _parse_immobiliare_html(soup)

        annunci.extend(annunci_pagina)
        time.sleep(1.5)

    return annunci


def _parse_immobiliare_json(soup: BeautifulSoup, base_url: str) -> list[Annuncio]:
    risultati = []
    # Immobiliare.it inietta i dati come __NEXT_DATA__ o JSON-LD
    scripts = soup.find_all("script", {"type": "application/ld+json"})
    for script in scripts:
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") not in ("Residence", "Apartment", "House", "RealEstateListing"):
                    continue
                prezzo = normalizza_prezzo(str(item.get("price", item.get("offers", {}).get("price", ""))))
                if not prezzo:
                    continue
                a = Annuncio(
                    titolo=item.get("name", "–"),
                    prezzo=prezzo,
                    locali=None,
                    mq=None,
                    piano=None,
                    indirizzo=str(item.get("address", "")),
                    url=item.get("url", base_url),
                    fonte="immobiliare.it",
                )
                risultati.append(a)
        except (json.JSONDecodeError, AttributeError):
            pass
    return risultati


def _parse_immobiliare_html(soup: BeautifulSoup) -> list[Annuncio]:
    risultati = []

    # Selettori principali per le card degli annunci
    cards = (
        soup.select("li[data-testid='result-card']")
        or soup.select("li.nd-list__item")
        or soup.select("article.in-listingCard")
        or soup.select("[class*='listing-card']")
        or soup.select("[class*='resultCard']")
    )

    if not cards:
        # Prova a trovare i link agli annunci direttamente
        links = soup.select("a[href*='/annunci/']")
        for link in links[:30]:
            href = link.get("href", "")
            if not href.startswith("http"):
                href = "https://www.immobiliare.it" + href
            titolo = link.get_text(strip=True)[:80] or "Annuncio"
            prezzo_tag = link.find_next(string=re.compile(r"\d[\d\.]+ €|€ [\d\.]+"))
            prezzo = normalizza_prezzo(prezzo_tag) if prezzo_tag else None
            if prezzo and LOCALI_MINIMI <= 8:
                risultati.append(Annuncio(
                    titolo=titolo, prezzo=prezzo, locali=None, mq=None,
                    piano=None, indirizzo="Roma - Torrino",
                    url=href, fonte="immobiliare.it",
                ))
        return risultati

    for card in cards:
        try:
            # Titolo
            titolo_el = (
                card.select_one("[class*='title']")
                or card.select_one("h2")
                or card.select_one("h3")
            )
            titolo = titolo_el.get_text(strip=True) if titolo_el else "–"

            # URL
            link_el = card.select_one("a[href]")
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://www.immobiliare.it" + href

            # Prezzo
            prezzo_el = (
                card.select_one("[class*='price']")
                or card.select_one("[class*='Price']")
                or card.find(string=re.compile(r"[\d\.]+ €"))
            )
            prezzo_txt = prezzo_el.get_text(strip=True) if hasattr(prezzo_el, "get_text") else str(prezzo_el or "")
            prezzo = normalizza_prezzo(prezzo_txt)
            if not prezzo:
                continue

            # Caratteristiche (locali, mq, piano)
            features_txt = card.get_text(" ", strip=True)
            locali = _estrai_locali(features_txt)
            mq = _estrai_mq(features_txt)
            piano = _estrai_piano(features_txt)

            # Indirizzo
            addr_el = card.select_one("[class*='address']") or card.select_one("[class*='location']")
            indirizzo = addr_el.get_text(strip=True) if addr_el else "Roma - Torrino"

            a = Annuncio(
                titolo=titolo, prezzo=prezzo, locali=locali, mq=mq,
                piano=piano, indirizzo=indirizzo, url=href,
                fonte="immobiliare.it",
                condizione=analizza_condizione(features_txt),
            )
            a.costo_ristrutturazione = stima_costo_ristrutturazione(mq, a.condizione)
            a.costo_totale = a.prezzo + a.costo_ristrutturazione
            risultati.append(a)

        except Exception:
            continue

    return risultati


# ─── Scraper Idealista.it ───────────────────────────────────────────────────────

IDEALISTA_URLS = [
    f"https://www.idealista.it/vendita-case/roma-rm/torrino/?ordine=prezzo-asc&prezzoMax={PREZZO_MAX_RICERCA}&locali=3,4,5,6",
    f"https://www.idealista.it/vendita-case/roma-rm/torrino-mezzocammino/?ordine=prezzo-asc&prezzoMax={PREZZO_MAX_RICERCA}&locali=3,4,5,6",
]


def scrape_idealista() -> list[Annuncio]:
    annunci = []
    for url in IDEALISTA_URLS:
        try:
            resp = SESSION.get(url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [!] Idealista.it - errore: {e}", file=sys.stderr)
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        items = (
            soup.select("article.item")
            or soup.select("[class*='listing-item']")
            or soup.select("[data-adid]")
        )

        for item in items:
            try:
                titolo_el = item.select_one("a.item-link") or item.select_one("[class*='title']")
                titolo = titolo_el.get_text(strip=True) if titolo_el else "–"

                href = titolo_el.get("href", "") if titolo_el else ""
                if href and not href.startswith("http"):
                    href = "https://www.idealista.it" + href

                prezzo_el = item.select_one("[class*='price']") or item.select_one(".price-row")
                prezzo = normalizza_prezzo(prezzo_el.get_text() if prezzo_el else "")
                if not prezzo:
                    continue

                features_txt = item.get_text(" ", strip=True)
                locali = _estrai_locali(features_txt)
                mq = _estrai_mq(features_txt)
                piano = _estrai_piano(features_txt)

                addr_el = item.select_one("[class*='location']") or item.select_one(".item-detail-location")
                indirizzo = addr_el.get_text(strip=True) if addr_el else "Roma - Torrino"

                a = Annuncio(
                    titolo=titolo, prezzo=prezzo, locali=locali, mq=mq,
                    piano=piano, indirizzo=indirizzo, url=href,
                    fonte="idealista.it",
                    condizione=analizza_condizione(features_txt),
                )
                a.costo_ristrutturazione = stima_costo_ristrutturazione(mq, a.condizione)
                a.costo_totale = a.prezzo + a.costo_ristrutturazione
                annunci.append(a)

            except Exception:
                continue

        time.sleep(2)

    return annunci


# ─── Scraper Casa.it ────────────────────────────────────────────────────────────

def scrape_casa() -> list[Annuncio]:
    url = (
        f"https://www.casa.it/vendita/residenziale/roma/pag-1/"
        f"?locali_min=3&prezzo_max={PREZZO_MAX_RICERCA}&q=torrino"
    )
    annunci = []
    try:
        resp = SESSION.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [!] Casa.it - errore: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    items = soup.select("[class*='listing-card']") or soup.select("[data-tracking-id]")

    for item in items:
        try:
            titolo_el = item.select_one("[class*='title']") or item.select_one("h2")
            titolo = titolo_el.get_text(strip=True) if titolo_el else "–"

            link_el = item.select_one("a[href]")
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://www.casa.it" + href

            prezzo_el = item.select_one("[class*='price']")
            prezzo = normalizza_prezzo(prezzo_el.get_text() if prezzo_el else "")
            if not prezzo:
                continue

            features_txt = item.get_text(" ", strip=True)
            locali = _estrai_locali(features_txt)
            mq = _estrai_mq(features_txt)
            piano = _estrai_piano(features_txt)

            a = Annuncio(
                titolo=titolo, prezzo=prezzo, locali=locali, mq=mq,
                piano=piano, indirizzo="Roma - Torrino",
                url=href, fonte="casa.it",
                condizione=analizza_condizione(features_txt),
            )
            a.costo_ristrutturazione = stima_costo_ristrutturazione(mq, a.condizione)
            a.costo_totale = a.prezzo + a.costo_ristrutturazione
            annunci.append(a)

        except Exception:
            continue

    return annunci


# ─── Helper estrazione ──────────────────────────────────────────────────────────

def _estrai_locali(testo: str) -> Optional[int]:
    patterns = [
        r"(\d)\s*local[ei]",
        r"local[ei]\s*(\d)",
        r"(\d)\s*vani",
        r"trilocale",
        r"quadrilocale",
        r"5\s*local",
        r"6\s*local",
    ]
    mapping = {"trilocale": 3, "quadrilocale": 4}
    t = testo.lower()
    for k, v in mapping.items():
        if k in t:
            return v
    for p in patterns:
        m = re.search(p, t)
        if m:
            try:
                return int(m.group(1))
            except (IndexError, ValueError):
                pass
    return None


def _estrai_mq(testo: str) -> Optional[int]:
    m = re.search(r"(\d{2,3})\s*m[²q2]", testo, re.IGNORECASE)
    if m:
        val = int(m.group(1))
        if 30 <= val <= 400:
            return val
    return None


def _estrai_piano(testo: str) -> Optional[str]:
    m = re.search(r"piano\s+(\w+)", testo, re.IGNORECASE)
    if m:
        return m.group(1)
    if "rialzato" in testo.lower():
        return "rialzato"
    if "terra" in testo.lower():
        return "terra"
    if "ultimo piano" in testo.lower():
        return "ultimo"
    return None


# ─── Filtro e ordinamento ───────────────────────────────────────────────────────

def filtra_e_ordina(annunci: list[Annuncio]) -> list[Annuncio]:
    validi = []
    visti = set()

    for a in annunci:
        # Deduplica per URL
        chiave = a.url or f"{a.prezzo}_{a.titolo[:30]}"
        if chiave in visti:
            continue
        visti.add(chiave)

        # Filtro locali
        if a.locali is not None and a.locali < LOCALI_MINIMI:
            continue

        # Filtro budget totale (prezzo + eventuale ristrutturazione)
        if a.costo_totale > PREZZO_MAX_RICERCA:
            continue

        # Note aggiuntive
        if a.condizione == "ristrutturare":
            mq_stima = a.mq or 75
            a.note.append(
                f"Stima ristrutturazione ~{a.costo_ristrutturazione:,}€ "
                f"({mq_stima}mq × 700€/mq) → Totale ~{a.costo_totale:,}€"
            )
        if a.prezzo > BUDGET_TOTALE:
            diff = a.prezzo - BUDGET_TOTALE
            a.note.append(f"Prezzo {diff:,}€ sopra budget → potenzialmente trattabile")

        validi.append(a)

    # Ordina: prima per costo totale, poi per prezzo
    validi.sort(key=lambda x: (x.costo_totale, x.prezzo))
    return validi


# ─── Output ─────────────────────────────────────────────────────────────────────

def stampa_risultati(annunci: list[Annuncio]):
    VERDE  = "\033[92m"
    GIALLO = "\033[93m"
    ROSSO  = "\033[91m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"

    SEPARATORE = "─" * 70

    print(f"\n{BOLD}{'=' * 70}")
    print(f"  RICERCA CASA - ROMA TORRINO")
    print(f"  Budget: {BUDGET_TOTALE:,}€  |  Locali min: {LOCALI_MINIMI}  |  Annunci trovati: {len(annunci)}")
    print(f"{'=' * 70}{RESET}\n")

    if not annunci:
        print(f"{ROSSO}Nessun annuncio trovato con i criteri specificati.{RESET}")
        print("Suggerimento: i siti potrebbero richiedere accesso manuale o avere anti-bot.")
        print("Visita direttamente:")
        print(f"  • https://www.immobiliare.it/vendita-case/torrino-roma/")
        print(f"  • https://www.idealista.it/vendita-case/roma-rm/torrino/")
        print(f"  • https://www.casa.it/vendita/residenziale/roma/?q=torrino")
        return

    # Raggruppa per categoria
    ottimo      = [a for a in annunci if a.condizione == "ottimo"]
    ristrutturo = [a for a in annunci if a.condizione == "ristrutturare"]
    unknown     = [a for a in annunci if a.condizione == "unknown"]

    sezioni = [
        (f"{VERDE}PRONTI AD ABITARE / RISTRUTTURATI{RESET}", ottimo),
        (f"{GIALLO}DA RISTRUTTURARE{RESET}", ristrutturo),
        (f"{CYAN}CONDIZIONE NON SPECIFICATA{RESET}", unknown),
    ]

    for titolo_sezione, lista in sezioni:
        if not lista:
            continue
        print(f"\n{BOLD}{titolo_sezione}{RESET}")
        print(SEPARATORE)

        for i, a in enumerate(lista, 1):
            colore_prezzo = VERDE if a.prezzo <= BUDGET_TOTALE else GIALLO
            print(f"\n{BOLD}[{i}] {a.titolo[:65]}{RESET}")
            print(f"    Fonte:     {a.fonte}")
            print(f"    Prezzo:    {colore_prezzo}{a.prezzo:,}€{RESET}", end="")
            if a.condizione == "ristrutturare" and a.mq:
                print(f"  +  {a.costo_ristrutturazione:,}€ ristrutturazione  =  {BOLD}{a.costo_totale:,}€ totale{RESET}")
            else:
                print()
            if a.locali:
                print(f"    Locali:    {a.locali}")
            if a.mq:
                print(f"    Superficie:{a.mq} mq")
            if a.piano:
                print(f"    Piano:     {a.piano}")
            print(f"    Indirizzo: {a.indirizzo[:60]}")
            print(f"    URL:       {a.url}")
            for nota in a.note:
                print(f"    {GIALLO}⚠ {nota}{RESET}")

        print()

    # Riepilogo finale
    print(f"\n{BOLD}{'=' * 70}")
    print("  RIEPILOGO COSTI DI RISTRUTTURAZIONE")
    print(f"{'=' * 70}{RESET}")
    print(f"  Leggera  (~300 €/mq): tinteggiatura, piccoli lavori")
    print(f"  Media    (~700 €/mq): bagni, cucina, impianti parziali   ← usata nel calcolo")
    print(f"  Completa (~1100€/mq): tutto da rifare (impianti + finiture)")
    print()
    print(f"  Su un appartamento di 75 mq:")
    print(f"    Leggera  → ~{75 * 350:,}€")
    print(f"    Media    → ~{75 * 700:,}€")
    print(f"    Completa → ~{75 * 1100:,}€")
    print()
    print(f"  Bonus fiscale: Superbonus/Bonus Ristrutturazione 50% (verifica disponibilità)")
    print(f"{'=' * 70}\n")


# ─── Link di ricerca diretti (modalità --links) ─────────────────────────────────

LINK_RICERCA = [
    {
        "nome": "Immobiliare.it – Torrino",
        "url": f"https://www.immobiliare.it/vendita-case/torrino-roma/?locali=3,4,5,6plus&prezzoMassimo={PREZZO_MAX_RICERCA}",
    },
    {
        "nome": "Immobiliare.it – Torrino Mezzocammino",
        "url": f"https://www.immobiliare.it/vendita-case/torrino-mezzocammino-roma/?locali=3,4,5,6plus&prezzoMassimo={PREZZO_MAX_RICERCA}",
    },
    {
        "nome": "Immobiliare.it – EUR (zona adiacente)",
        "url": f"https://www.immobiliare.it/vendita-case/eur-roma/?locali=3,4,5,6plus&prezzoMassimo={PREZZO_MAX_RICERCA}",
    },
    {
        "nome": "Idealista.it – Torrino",
        "url": f"https://www.idealista.it/vendita-case/roma-rm/torrino/?ordine=prezzo-asc&prezzoMax={PREZZO_MAX_RICERCA}&locali=3,4,5,6",
    },
    {
        "nome": "Idealista.it – Torrino Mezzocammino",
        "url": f"https://www.idealista.it/vendita-case/roma-rm/torrino-mezzocammino/?ordine=prezzo-asc&prezzoMax={PREZZO_MAX_RICERCA}&locali=3,4,5,6",
    },
    {
        "nome": "Casa.it – Torrino",
        "url": f"https://www.casa.it/vendita/residenziale/roma/pag-1/?locali_min=3&prezzo_max={PREZZO_MAX_RICERCA}&q=torrino",
    },
    {
        "nome": "Subito.it – Torrino",
        "url": f"https://www.subito.it/annunci-lazio/vendita/appartamenti/roma/torrino/?locali_min=3&prezzo_max={PREZZO_MAX_RICERCA}",
    },
    # Da ristrutturare: prezzo max ridotto per lasciare spazio alla ristrutturazione
    {
        "nome": "Immobiliare.it – Torrino DA RISTRUTTURARE (max 270k)",
        "url": f"https://www.immobiliare.it/vendita-case/torrino-roma/?locali=3,4,5,6plus&prezzoMassimo=270000&parole_chiave=da+ristrutturare",
    },
    {
        "nome": "Idealista.it – Torrino DA RISTRUTTURARE (max 270k)",
        "url": f"https://www.idealista.it/vendita-case/roma-rm/torrino/?ordine=prezzo-asc&prezzoMax=270000&locali=3,4,5,6&keywords=da+ristrutturare",
    },
]


def stampa_links():
    BOLD  = "\033[1m"
    CYAN  = "\033[96m"
    RESET = "\033[0m"
    print(f"\n{BOLD}{'=' * 70}")
    print(f"  LINK RICERCA – Roma Torrino  |  Budget: {BUDGET_TOTALE:,}€")
    print(f"{'=' * 70}{RESET}")
    print(f"\nAprire nel browser. Prezzi ricercati fino a {PREZZO_MAX_RICERCA:,}€")
    print(f"(per annunci trattabili: {MARGINE_TRATTATIVA*100:.0f}% sopra budget)\n")
    for i, link in enumerate(LINK_RICERCA, 1):
        print(f"  {BOLD}[{i}] {link['nome']}{RESET}")
        print(f"      {CYAN}{link['url']}{RESET}\n")
    print(f"{BOLD}Nota ristrutturazione{RESET}: con budget 350k e costo medio 700€/mq,")
    print(f"  per un app. da 75mq la ristrutturazione media costa ~52.500€.")
    print(f"  Quindi cercare immobili da ristrutturare fino a ~297.500€.\n")


# ─── Main ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Cerca casa a Roma – Zona Torrino. Budget 350.000€, min 3 locali."
    )
    parser.add_argument(
        "--links", action="store_true",
        help="Stampa solo i link di ricerca da aprire nel browser (nessuno scraping)"
    )
    args = parser.parse_args()

    if args.links:
        stampa_links()
        return

    print("Avvio ricerca casa Roma – Zona Torrino...")
    print(f"Budget: {BUDGET_TOTALE:,}€ | Max ricerca: {PREZZO_MAX_RICERCA:,}€ | Locali min: {LOCALI_MINIMI}")
    print()

    tutti_annunci: list[Annuncio] = []

    print("Scraping Immobiliare.it...")
    risultati = scrape_immobiliare()
    print(f"   → {len(risultati)} annunci trovati")
    tutti_annunci.extend(risultati)

    print("Scraping Idealista.it...")
    risultati = scrape_idealista()
    print(f"   → {len(risultati)} annunci trovati")
    tutti_annunci.extend(risultati)

    print("Scraping Casa.it...")
    risultati = scrape_casa()
    print(f"   → {len(risultati)} annunci trovati")
    tutti_annunci.extend(risultati)

    print(f"\nTotale grezzo: {len(tutti_annunci)} annunci")
    print("Filtro e ordinamento in corso...")

    annunci_filtrati = filtra_e_ordina(tutti_annunci)
    stampa_risultati(annunci_filtrati)

    # Salva JSON
    output = [
        {
            "titolo": a.titolo,
            "prezzo": a.prezzo,
            "locali": a.locali,
            "mq": a.mq,
            "piano": a.piano,
            "indirizzo": a.indirizzo,
            "url": a.url,
            "fonte": a.fonte,
            "condizione": a.condizione,
            "costo_ristrutturazione": a.costo_ristrutturazione,
            "costo_totale": a.costo_totale,
            "note": a.note,
        }
        for a in annunci_filtrati
    ]
    with open("risultati_casa.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\nRisultati salvati in risultati_casa.json ({len(output)} annunci)")

    if not annunci_filtrati:
        print("\nNessun risultato: esegui   python3 cerca_casa.py --links")
        print("per ottenere i link da aprire manualmente nel browser.")


if __name__ == "__main__":
    main()
