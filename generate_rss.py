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
    """Load and validate restaurants from sources.json."""

    if not SOURCES_FILE.exists():
        raise FileNotFoundError(
            f"Source file {SOURCES_FILE} was not found."
        )

    sources = json.loads(
        SOURCES_FILE.read_text(encoding="utf-8")
    )

    if not isinstance(sources, list):
        raise ValueError(
            "sources.json must contain a JSON array."
        )

    required_fields = {"id", "name", "url"}
    known_ids = set()

    for source in sources:
        if not isinstance(source, dict):
            raise ValueError(
                "Every source in sources.json must be a JSON object."
            )

        missing_fields = required_fields - set(source.keys())

        if missing_fields:
            raise ValueError(
                f"Source is missing required fields: "
                f"{', '.join(sorted(missing_fields))}"
            )

        if source["id"] in known_ids:
            raise ValueError(
                f"Duplicate source ID: {source['id']}"
            )

        known_ids.add(source["id"])

        if not source["url"].startswith(("https://", "http://")):
            raise ValueError(
                f"Invalid URL for source {source['name']}: "
                f"{source['url']}"
            )

    return sources


def download_page(url):
    """Download a restaurant web page."""

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
    response.encoding = (
        response.apparent_encoding
        or response.encoding
        or "utf-8"
    )

    return response.text


def html_to_lines(page_html):
    """Convert an HTML document into clean visible text lines."""

    soup = BeautifulSoup(page_html, "html.parser")

    # Remove elements that should not be part of the menu.
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

        # Prevent consecutive duplicate lines.
        if not lines or lines[-1] != line:
            lines.append(line)

    return lines


def normalize_text(value):
    """Normalize text for reliable case-insensitive comparison."""

    value = value.casefold()
    value = re.sub(r"\s+", "", value)
    value = value.replace("–", "-")
    value = value.replace("—", "-")
    value = value.replace("\u00a0", "")

    return value


def is_date_line(value):
    """Return True when a line contains only a Czech-style date."""

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
    """Return common text representations of today's date."""

    return {
        f"{today.day}.{today.month}.{today.year}",
        f"{today.day:02d}.{today.month:02d}.{today.year}",
        f"{today.day}. {today.month}. {today.year}",
        f"{today.day:02d}. {today.month:02d}. {today.year}",
    }


def contains_today(value, today):
    """Return True when a text line contains today's date."""

    normalized_value = normalize_text(value)

    return any(
        normalize_text(date_value) in normalized_value
        for date_value in date_variants(today)
    )


def find_text(lines, search_text, start=0):
    """Find the first line containing the requested text."""

    normalized_search = normalize_text(search_text)

    for index in range(start, len(lines)):
        if normalized_search in normalize_text(lines[index]):
            return index

    return None


def clean_menu_lines(lines):
    """Remove utility, schedule, and duplicate lines from a menu."""

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
    Extract today's section from the Slatina Bistro page.

    The section starts at today's date and ends at the next date
    or at the breakfast section.
    """

    start = None

    for index, line in enumerate(lines):
        if contains_today(line, today):
            start = index
            break

    if start is None:
        raise ValueError(
            "Today's date was not found on the page."
        )

    end = len(lines)

    for index in range(start + 1, len(lines)):
        if is_date_line(lines[index]):
            end = index

            if lines[index - 1] in DAY_NAMES:
                end = index - 1

            break

        if normalize_text(lines[index]) == normalize_text("Snídaně"):
            end = index
            break

    result = lines[start:end]

    # Include the weekday located immediately before the date.
    if start > 0 and lines[start - 1] in DAY_NAMES:
        result.insert(0, lines[start - 1])

    return clean_menu_lines(result)


def parse_turanka(lines, today):
    """
    Extract today's daily menu from the Tackarna Turanka page.

    The parser looks for today's date inside the daily-menu section
    and stops before the weekly-menu section.
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
        # Fall back to the Czech weekday name.
        today_name = DAY_NAMES[today.weekday()]
        start = find_text(
            lines,
            today_name,
            daily_menu_start,
        )

    if start is None:
        raise ValueError(
            "Today's menu was not found on the page."
        )

    end = find_text(
        lines,
        "Týdenní menu",
        start + 1,
    )

    if end is None:
        end = len(lines)

    result = lines[start:end]

    return clean_menu_lines(result)


def parse_jomsom(lines, today):
    """
    Extract today's section from the Jomsom weekly lunch menu.

    The section starts at today's spaced weekday heading and ends
    at the following weekday heading.
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
        # Fall back to a weekday name without spaces.
        normal_day = DAY_NAMES[today.weekday()]
        start = find_text(lines, normal_day)

    if start is None:
        raise ValueError(
            "Today's weekday heading was not found."
        )

    end = len(lines)

    for index in range(start + 1, len(lines)):
        normalized_line = normalize_text(lines[index])

        if normalized_line in day_markers:
            end = index
            break

        if (
            "informaceopřítomnostialergenů"
            in normalized_line
        ):
            end = index
            break

    result = lines[start:end]

    return clean_menu_lines(result)


def parse_generic(lines, today):
    """
    Try to extract today's menu from an unknown restaurant page.

    The parser first looks for today's date and then falls back
    to today's Czech weekday name.
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
            "The generic parser could not find today's date "
            "or weekday name."
        )

    # Limit generic extraction to avoid returning the entire page.
    end = min(start + 100, len(lines))

    for index in range(start + 1, end):
        if is_date_line(lines[index]):
            end = index

            if lines[index - 1] in DAY_NAMES:
                end = index - 1

            break

    return clean_menu_lines(lines[start:end])


def parse_source(source, lines, today):
    """Select a specialized parser based on the source ID."""

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
    """Return True when a line looks like a menu price."""

    normalized = value.strip()

    return bool(
        re.fullmatch(
            r"\d+\s*(Kč|,-|-)",
            normalized,
            flags=re.IGNORECASE,
        )
    )


def format_menu(menu_lines):
    """Convert extracted menu lines into HTML suitable for RSS."""

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
            normalize_text("Polévky"),
        }:
            output.append(
                "<h4>🍲 Polévky</h4>"
            )

        elif normalize_text(line) in {
            normalize_text("Hlavní chod"),
            normalize_text("Hlavní chody"),
        }:
            output.append(
                "<h4>🍽️ Hlavní jídla</h4>"
            )

        elif line.isdigit():
            dish_number = line

            if index + 1 < len(menu_lines):
                dish_name = menu_lines[index + 1]
                price = None

                if (
                    index + 2 < len(menu_lines)
                    and looks_like_price(menu_lines[index + 2])
                ):
                    price = menu_lines[index + 2]
                    index += 1

                output.append("<p>")
                output.append(
                    f"<strong>{html.escape(dish_number)}. "
                    f"{html.escape(dish_name)}</strong>"
                )

                if price:
                    output.append(
                        f"<br>💰 {html.escape(price)}"
                    )

                output.append("</p>")
                index += 1

        elif looks_like_price(line):
            output.append(
                f"<strong>💰 {escaped_line}</strong><br><br>"
            )

        else:
            output.append(
                f"{escaped_line}<br>"
            )

        index += 1

    return "\n".join(output)


def add_rss_item(channel, source, menu_lines, now):
    """Add one successful restaurant result to the RSS channel."""

    item = ET.SubElement(channel, "item")
    date_text = f"{now.day}.{now.month}.{now.year}"

    ET.SubElement(item, "title").text = (
        f"{source['name']} – menu {date_text}"
    )

    ET.SubElement(item, "link").text = source["url"]

    guid_source = (
        f"{source['id']}:{now.strftime('%Y-%m-%d')}"
    )

    guid_value = hashlib.sha256(
        guid_source.encode("utf-8")
    ).hexdigest()

    guid = ET.SubElement(
        item,
        "guid",
        {"isPermaLink": "false"},
    )
    guid.text = guid_value

    ET.SubElement(item, "pubDate").text = (
        format_datetime(now)
    )

    description = ET.SubElement(
        item,
        "description",
    )
    description.text = format_menu(menu_lines)


def add_error_item(channel, source, error, now):
    """Add a restaurant loading error to the RSS channel."""

    item = ET.SubElement(channel, "item")

    ET.SubElement(item, "title").text = (
        f"{source['name']} – menu se nepodařilo načíst"
    )

    ET.SubElement(item, "link").text = source["url"]

    guid = ET.SubElement(
        item,
        "guid",
        {"isPermaLink": "false"},
    )

    guid.text = (
        f"{source['id']}-error-{now.strftime('%Y-%m-%d')}"
    )

    ET.SubElement(item, "pubDate").text = (
        format_datetime(now)
    )

    description = ET.SubElement(
        item,
        "description",
    )

    description.text = (
        "<strong>Menu se nepodařilo načíst.</strong><br>"
        f"{html.escape(str(error))}"
    )


def create_rss():
    """Download all configured sources and create a combined RSS feed."""

    now = datetime.now(TIME_ZONE)
    sources = load_sources()

    rss = ET.Element(
        "rss",
        {"version": "2.0"},
    )

    channel = ET.SubElement(
        rss,
        "channel",
    )

    ET.SubElement(channel, "title").text = (
        "Polední menu restaurací"
    )

    ET.SubElement(channel, "link").text = (
        "https://hon-bob.github.io/rss-lunch/rss.xml"
    )

    ET.SubElement(channel, "description").text = (
        "Denní menu vybraných restaurací"
    )

    ET.SubElement(channel, "language").text = (
        "cs-CZ"
    )

    ET.SubElement(channel, "lastBuildDate").text = (
        format_datetime(now)
    )

    ET.SubElement(channel, "ttl").text = "60"

    successful = 0
    failed = 0

    for source in sources:
        print(f"Loading: {source['name']}")
        print(f"URL: {source['url']}")

        try:
            page_html = download_page(source["url"])
            lines = html_to_lines(page_html)

            menu_lines = parse_source(
                source,
                lines,
                now,
            )

            if not menu_lines:
                raise ValueError(
                    "The parser returned an empty menu."
                )

            add_rss_item(
                channel,
                source,
                menu_lines,
                now,
            )

            successful += 1

            print(
                f"Success: {len(menu_lines)} menu lines found"
            )

        except Exception as error:
            failed += 1

            print(
                f"Error for {source['name']}: {error}"
            )

            add_error_item(
                channel,
                source,
                error,
                now,
            )

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tree = ET.ElementTree(rss)

    # Pretty-print XML when supported by the Python version.
    try:
        ET.indent(tree, space="  ")
    except AttributeError:
        pass

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )

    print()
    print(f"RSS created: {OUTPUT_FILE}")
    print(f"Successfully loaded: {successful}")
    print(f"Failed: {failed}")


if __name__ == "__main__":
    create_rss()
