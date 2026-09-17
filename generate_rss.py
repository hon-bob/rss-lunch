import hashlib
import html
import json
import re
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup


SOURCES_FILE = Path("sources.json")
OUTPUT_FILE = Path("docs/rss.xml")
TIME_ZONE = ZoneInfo("Europe/Prague")

USER_AGENT = (
    "Mozilla/5.0 (compatible; LunchMenuRSS/1.0; "
    "+https://github.com/hon-bob/rss-lunch)"
)

DAY_NAMES = [
    "Pondělí",
    "Úterý",
    "Středa",
    "Čtvrtek",
    "Pátek",
    "Sobota",
    "Neděle",
]

SPACED_DAY_NAMES = [
    "P O N D Ě L Í",
    "Ú T E R Ý",
    "S T Ř E D A",
    "Č T V R T E K",
    "P Á T E K",
    "S O B O T A",
    "N E D Ě L E",
]


def load_sources():
    """Načte seznam restaurací ze sources.json."""

    if not SOURCES_FILE.exists():
        raise FileNotFoundError(
            f"Soubor {SOURCES_FILE} nebyl nalezen."
        )

    sources = json.loads(
        SOURCES_FILE.read_text(encoding="utf-8")
    )

    if not isinstance(sources, list):
        raise ValueError("sources.json musí obsahovat JSON pole.")

    required_fields = {"id", "name", "url"}

    for source in sources:
        missing = required_fields - set(source.keys())

        if missing:
            raise ValueError(
                f"Zdroj nemá povinná pole: {', '.join(missing)}"
            )

    return sources


def download_page(url):
    """Stáhne HTML stránku restaurace."""

    response = requests.get(
        url,
        timeout=30,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
        },
    )

    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding

    return response.text


def html_to_lines(page_html):
    """Převede HTML stránku na seznam čistých textových řádků."""

    soup = BeautifulSoup(page_html, "html.parser")

    for element in soup(
        [
            "script",
            "style",
            "noscript",
            "svg",
            "nav",
            "footer",
        ]
    ):
        element.decompose()

    lines = []

    for line in soup.get_text("\n", strip=True).splitlines():
        line = re.sub(r"\s+", " ", line).strip()

        if not line:
            continue

        if not lines or lines[-1] != line:
            lines.append(line)

    return lines


def normalize_text(value):
    """Normalizuje text pro bezpečnější porovnávání."""

    value = value.casefold()
    value = re.sub(r"\s+", "", value)
    value = value.replace("–", "-")
    value = value.replace("—", "-")

    return value


def is_date_line(value):
    """Zjistí, zda řádek obsahuje pouze datum."""

    normalized = normalize_text(value)

    return bool(
        re.fullmatch(
            r"(0?[1-9]|[12][0-9]|3[01])\."
            r"(0?[1-9]|1[0-2])\."
            r"20[0-9]{2}",
            normalized,
        )
    )


def date_variants(today):
    """Vrátí podporované zápisy dnešního data."""

    return {
        f"{today.day}.{today.month}.{today.year}",
        f"{today.day:02d}.{today.month:02d}.{today.year}",
        f"{today.day}. {today.month}. {today.year}",
        f"{today.day:02d}. {today.month:02d}. {today.year}",
    }


def contains_today(value, today):
    """Zjistí, zda řádek obsahuje dnešní datum."""

    normalized_value = normalize_text(value)

    return any(
        normalize_text(date_value) in normalized_value
        for date_value in date_variants(today)
    )


def find_text(lines, search_text, start=0):
    """Najde první řádek obsahující zadaný text."""

    normalized_search = normalize_text(search_text)

    for index in range(start, len(lines)):
        if normalized_search in normalize_text(lines[index]):
            return index

    return None


def clean_menu_lines(lines):
    """Odstraní technické a nepotřebné řádky."""

    ignored_lines = {
        "Každý všední den",
        "11:00 - 15:00",
        "10:00 - 14:00",
        ".",
        "Recommended",
    }

    result = []

    for line in lines:
        line = line.strip()

        if not line:
            continue

        if line in ignored_lines:
            continue

        if result and result[-1] == line:
            continue

        result.append(line)

    return result


def parse_slatina(lines, today):
    """
    Slatina Bistro:
    najde dnešní datum a vezme obsah do následujícího data.
    """

    start = None

    for index, line in enumerate(lines):
        if contains_today(line, today):
            start = index
            break

    if start is None:
        raise ValueError("Dnešní datum nebylo na stránce nalezeno.")

    end = len(lines)

    for index in range(start + 1, len(lines)):
        if is_date_line(lines[index]):
            end = index

            if index > start and lines[index - 1] in DAY_NAMES:
                end = index - 1

            break

        if normalize_text(lines[index]) == normalize_text("Snídaně"):
            end = index
            break

    result = lines[start:end]

    if start > 0 and lines[start - 1] in DAY_NAMES:
        result.insert(0, lines[start - 1])

    return clean_menu_lines(result)


def parse_turanka(lines, today):
    """
    Táckárna Tuřanka:
    najde dnešní datum v sekci Denní menu
    a ukončí výběr před Týdenním menu.
    """

    daily_menu_start = find_text(lines, "Denní menu")

    if daily_menu_start is None:
        daily_menu_start = 0

    start = None

    for index in range(daily_menu_start, len(lines)):
        if contains_today(lines[index], today):
            start = index
            break

    if start is None:
        # Některé varianty stránky zobrazují datum v záložce před menu.
        today_name = DAY_NAMES[today.weekday()]
        start = find_text(lines, today_name, daily_menu_start)

    if start is None:
        raise ValueError("Dnešní menu nebylo na stránce nalezeno.")

    end = find_text(lines, "Týdenní menu", start + 1)

    if end is None:
        end = len(lines)

    result = lines[start:end]

    return clean_menu_lines(result)


def parse_jomsom(lines, today):
    """
    Jomsom:
    vybere část mezi názvem dnešního dne
    a názvem následujícího dne.
    """

    target_day = SPACED_DAY_NAMES[today.weekday()]
    normalized_target = normalize_text(target_day)

    day_markers = {
        normalize_text(day_name)
        for day_name in SPACED_DAY_NAMES
    }

    start = None

    for index, line in enumerate(lines):
        if normalize_text(line) == normalized_target:
            start = index
            break

    if start is None:
        # Záložní hledání pro variantu bez mezer.
        normal_day = DAY_NAMES[today.weekday()]
        start = find_text(lines, normal_day)

    if start is None:
        raise ValueError("Název dnešního dne nebyl nalezen.")

    end = len(lines)

    for index in range(start + 1, len(lines)):
        if normalize_text(lines[index]) in day_markers:
            end = index
            break

        if "informaceopřítomnostialergenů" in normalize_text(
            lines[index]
        ):
            end = index
            break

    result = lines[start:end]

    return clean_menu_lines(result)


def parse_generic(lines, today):
    """
    Obecný parser pro další restaurace.

    Pokusí se najít dnešní datum. Pokud datum nenajde,
    pokusí se najít název dne.
    """

    start = None

    for index, line in enumerate(lines):
        if contains_today(line, today):
            start = index
            break

    if start is None:
        today_name = DAY_NAMES[today.weekday()]
        start = find_text(lines, today_name)

    if start is None:
        raise ValueError(
            "Obecný parser nenalezl dnešní datum ani název dne."
        )

    end = min(start + 100, len(lines))

    for index in range(start + 1, end):
        if is_date_line(lines[index]):
            end = index
            break

    return clean_menu_lines(lines[start:end])


def parse_source(source, lines, today):
    """Vybere parser podle id restaurace."""

    parser_by_id = {
        "slatina": parse_slatina,
        "turanka": parse_turanka,
        "jomsom": parse_jomsom,
    }

    parser = parser_by_id.get(
        source["id"],
        parse_generic,
    )

    return parser(lines, today)


def looks_like_price(value):
    """Zjistí, zda řádek vypadá jako cena."""

    normalized = value.strip()

    return bool(
        re.fullmatch(
            r"\d+\s*(Kč|,-|-)",
            normalized,
            flags=re.IGNORECASE,
        )
    )


def format_menu(menu_lines):
    """Převede menu do HTML vhodného pro RSS a Teams."""

    output = []
    index = 0

    while index < len(menu_lines):
        line = menu_lines[index].strip()
        escaped_line = html.escape(line)

        if line in DAY_NAMES:
            output.append(
                f"<h3>📅 {escaped_line}</h3>"
            )

        elif is_date_line(line):
            output.append(
                f"<strong>{escaped_line}</strong><br>"
            )

        elif normalize_text(line) in {
            normalize_text("Polévka"),
            normalize_text("Polévka:"),
