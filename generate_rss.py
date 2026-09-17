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

FEED_URL = "https://hon-bob.github.io/rss-lunch/rss.xml"

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
    """Load and validate restaurant definitions from sources.json."""

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
                "Every source must be a JSON object."
            )

        missing_fields = required_fields - set(source.keys())

        if missing_fields:
            raise ValueError(
                "Source is missing required fields: "
                + ", ".join(sorted(missing_fields))
            )

        source_id = source["id"]

        if source_id in known_ids:
            raise ValueError(
                f"Duplicate source ID: {source_id}"
            )

        known_ids.add(source_id)

        if not source["url"].startswith(
            ("https://", "http://")
        ):
            raise ValueError(
                f"Invalid URL for {source['name']}: "
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
    """Convert HTML into cleaned visible text lines."""

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

    result = []

    for text_line in soup.get_text(
        "\n",
        strip=True,
    ).splitlines():
        text_line = re.sub(
            r"\s+",
            " ",
            text_line,
        ).strip()

        if not text_line:
            continue

        if not result or result[-1] != text_line:
            result.append(text_line)

    return result


def normalize_text(value):
    """Normalize text for reliable comparisons."""

    value = value.casefold()
    value = value.replace("\u00a0", " ")
    value = value.replace("–", "-")
    value = value.replace("—", "-")
    value = re.sub(r"\s+", "", value)

    return value


def get_date_text(now):
    """Return today's date in the format used in the RSS feed."""

    return f"{now.day}.{now.month}.{now.year}"


def date_variants(now):
    """Return common representations of today's date."""

    return {
        f"{now.day}.{now.month}.{now.year}",
        f"{now.day:02d}.{now.month:02d}.{now.year}",
        f"{now.day}. {now.month}. {now.year}",
        f"{now.day:02d}. {now.month:02d}. {now.year}",
    }


def contains_today(value, now):
    """Return True if the text contains today's date."""

    normalized_value = normalize_text(value)

    return any(
        normalize_text(date_value) in normalized_value
        for date_value in date_variants(now)
    )


def is_date_line(value):
    """Return True if the line contains a Czech-style date."""

    normalized_value = normalize_text(value)

    return bool(
        re.fullmatch(
            r"(0?[1-9]|[12][0-9]|3[01])\."
            r"(0?[1-9]|1[0-2])\."
            r"20[0-9]{2}",
            normalized_value,
        )
    )


def is_weekday_line(value):
    """Return True if the line is a Czech weekday heading."""

    normalized_value = normalize_text(value)

    normal_days = {
        normalize_text(day)
        for day in DAY_NAMES
    }

    spaced_days = {
        normalize_text(day)
        for day in SPACED_DAY_NAMES
    }

    return (
        normalized_value in normal_days
        or normalized_value in spaced_days
    )


def find_text(lines, search_text, start=0):
    """Find the first line containing the specified text."""

    normalized_search = normalize_text(search_text)

    for index in range(start, len(lines)):
        if normalized_search in normalize_text(lines[index]):
            return index

    return None


def looks_like_price(value):
    """Return True if the line looks like a Czech menu price."""

    normalized_value = value.strip()

    return bool(
        re.fullmatch(
            r"\d+\s*(Kč|,-|-)",
            normalized_value,
            flags=re.IGNORECASE,
        )
    )


def normalize_price(value):
    """Normalize a price into a consistent Czech format."""

    value = value.strip()
    value = re.sub(r"\s+", " ", value)

    match = re.search(r"(\d+)", value)

    if not match:
        return value

    return f"{match.group(1)} Kč"


def clean_name(value):
    """Clean a soup or dish name."""

    value = re.sub(r"\s+", " ", value).strip()
    value = value.strip(":- ")

    return value


def remove_common_noise(lines):
    """Remove navigation and timing lines."""

    ignored_values = {
        normalize_text("Každý všední den"),
        normalize_text("11:00 - 15:00"),
        normalize_text("10:00 - 14:00"),
        normalize_text("Denní menu"),
        normalize_text("Týdenní nabídka"),
        normalize_text("Recommended"),
        normalize_text("."),
    }

    result = []

    for line in lines:
        if normalize_text(line) in ignored_values:
            continue

        if result and result[-1] == line:
            continue

        result.append(line)

    return result


def find_today_section(lines, now):
    """Extract lines from today's date to the next date."""

    start = None

    for index, line in enumerate(lines):
        if contains_today(line, now):
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

            if (
                index > start
                and is_weekday_line(lines[index - 1])
            ):
                end = index - 1

            break

        normalized_line = normalize_text(lines[index])

        if normalized_line in {
            normalize_text("Týdenní menu"),
            normalize_text(
                "Nabídka obědů na celý týden"
            ),
            normalize_text("Snídaně"),
        }:
            end = index
            break

    result = lines[start:end]

    if start > 0 and is_weekday_line(lines[start - 1]):
        result.insert(0, lines[start - 1])

    return remove_common_noise(result)


def parse_numbered_menu(lines):
    """
    Parse a menu where dishes use separate numeric lines.

    Expected structure:
    1
    Dish name
    150,-
    """

    soups = []
    dishes = []

    soup_heading = None

    for index, line in enumerate(lines):
        if normalize_text(line) in {
            normalize_text("Polévka"),
            normalize_text("Polévka:"),
            normalize_text("Polévky"),
        }:
            soup_heading = index
            break

    first_dish = None

    for index, line in enumerate(lines):
        if line.strip().isdigit():
            first_dish = index
            break

    if soup_heading is not None:
        soup_end = (
            first_dish
            if first_dish is not None
            else len(lines)
        )

        index = soup_heading + 1

        while index < soup_end:
            name = lines[index]

            if looks_like_price(name):
                index += 1
                continue

            price = ""

            if (
                index + 1 < soup_end
                and looks_like_price(lines[index + 1])
            ):
                price = normalize_price(lines[index + 1])
                index += 1

            soup_name = clean_name(name)

            if soup_name:
                soups.append(
                    {
                        "name": soup_name,
                        "price": price,
                    }
                )

            index += 1

    if first_dish is not None:
        index = first_dish

        while index < len(lines):
            if not lines[index].strip().isdigit():
                index += 1
                continue

            number = int(lines[index].strip())
            index += 1

            name_parts = []
            price = ""

            while index < len(lines):
                current = lines[index]

                if current.strip().isdigit():
                    break

                if looks_like_price(current):
                    price = normalize_price(current)
                    index += 1
                    break

                name_parts.append(current)
                index += 1

            dish_name = clean_name(
                " ".join(name_parts)
            )

            if dish_name:
                dishes.append(
                    {
                        "number": number,
                        "name": dish_name,
                        "price": price,
                    }
                )

    return soups, dishes


def split_blocks_by_price(lines):
    """Split unnumbered menu lines into blocks ending with a price."""

    blocks = []
    current_block = []

    for line in lines:
        current_block.append(line)

        if looks_like_price(line):
            blocks.append(current_block)
            current_block = []

    return blocks


def block_to_menu_item(block):
    """Convert a price-terminated block into a menu item."""

    if not block:
        return None

    price = ""

    if looks_like_price(block[-1]):
        price = normalize_price(block[-1])
        content = block[:-1]
    else:
        content = block

    ignored_patterns = [
        r"^\d+([a-z],?)+$",
        r"^\d+([a-z]?,?)+$",
        r"^\d+([,.]\d+)?\s*l$",
        r"^\d+([,.]\d+)?\s*g$",
        r"^orientační energetická hodnota",
    ]

    name_parts = []

    for line in content:
        normalized_line = line.strip()

        if any(
            re.search(
                pattern,
                normalized_line,
                flags=re.IGNORECASE,
            )
            for pattern in ignored_patterns
        ):
            continue

        name_parts.append(normalized_line)

    name = clean_name(" ".join(name_parts))

    if not name:
        return None

    return {
        "name": name,
        "price": price,
    }


def parse_slatina(lines, now):
    """Parse today's Slatina Bistro menu."""

    section = find_today_section(lines, now)
    soups, dishes = parse_numbered_menu(section)

    return {
        "day": DAY_NAMES[now.weekday()],
        "date": get_date_text(now),
        "soups": soups,
        "dishes": dishes,
    }


def parse_turanka(lines, now):
    """Parse today's Tackarna Turanka menu."""

    section = find_today_section(lines, now)

    soup_start = find_text(section, "Polévka")
    main_start = find_text(section, "Hlavní chod")

    soups = []
    dishes = []

    if soup_start is not None:
        soup_end = (
            main_start
            if main_start is not None
            else len(section)
        )

        soup_lines = section[
            soup_start + 1:soup_end
        ]

        for block in split_blocks_by_price(soup_lines):
            item = block_to_menu_item(block)

            if item:
                soups.append(item)

    if main_start is not None:
        main_lines = section[main_start + 1:]

        for block in split_blocks_by_price(main_lines):
            item = block_to_menu_item(block)

            if item:
                item["number"] = len(dishes) + 1
                dishes.append(item)

    return {
        "day": DAY_NAMES[now.weekday()],
        "date": get_date_text(now),
        "soups": soups,
        "dishes": dishes,
    }


def extract_jomsom_section(lines, now):
    """Extract today's Jomsom weekday section."""

    target_day = normalize_text(
        SPACED_DAY_NAMES[now.weekday()]
    )

    all_day_markers = {
        normalize_text(day)
        for day in SPACED_DAY_NAMES
    }

    start = None

    for index, line in enumerate(lines):
        if normalize_text(line) == target_day:
            start = index
            break

    if start is None:
        raise ValueError(
            "Today's Jomsom weekday heading was not found."
        )

    end = len(lines)

    for index in range(start + 1, len(lines)):
        normalized_line = normalize_text(lines[index])

        if normalized_line in all_day_markers:
            end = index
            break

        if (
            "informaceopřítomnostialergenů"
            in normalized_line
        ):
            end = index
            break

    return lines[start + 1:end]


def parse_jomsom_item(value):
    """Parse a Jomsom item ending with the :- PRICE Kč pattern."""

    value = re.sub(r"\s+", " ", value).strip()

    match = re.match(
        r"^(.*?)(?::\s*-\s*|:-)\s*(\d+)\s*Kč$",
        value,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    name = clean_name(match.group(1))
    price = f"{match.group(2)} Kč"

    return {
        "name": name,
        "price": price,
    }


def parse_jomsom(lines, now):
    """Parse today's Jomsom menu."""

    section = extract_jomsom_section(lines, now)

    # Join lines because the website sometimes puts Kč on a new line.
    combined_text = " ".join(section)
    combined_text = re.sub(
        r"\s+",
        " ",
        combined_text,
    ).strip()

    # Split the text immediately after every complete price.
    raw_items = re.split(
        r"(?<=Kč)\s+",
        combined_text,
        flags=re.IGNORECASE,
    )

    parsed_items = []

    for raw_item in raw_items:
        parsed_item = parse_jomsom_item(raw_item)

        if parsed_item:
            parsed_items.append(parsed_item)

    soups = []
    dishes = []

    for item in parsed_items:
        normalized_name = normalize_text(item["name"])

        if (
            "soup" in normalized_name
            or "polévka" in normalized_name
        ):
            soups.append(item)
            continue

        number_match = re.match(
            r"^(\d+)\.\s*(.*)$",
            item["name"],
        )

        if number_match:
            dishes.append(
                {
                    "number": int(number_match.group(1)),
                    "name": clean_name(
                        number_match.group(2)
                    ),
                    "price": item["price"],
                }
            )
        else:
            dishes.append(
                {
                    "number": len(dishes) + 1,
                    "name": item["name"],
                    "price": item["price"],
                }
            )

    return {
        "day": DAY_NAMES[now.weekday()],
        "date": get_date_text(now),
        "soups": soups,
        "dishes": dishes,
    }


def parse_generic(lines, now):
    """Parse an unknown source with basic date-based extraction."""

    section = find_today_section(lines, now)
    soups, dishes = parse_numbered_menu(section)

    return {
        "day": DAY_NAMES[now.weekday()],
        "date": get_date_text(now),
        "soups": soups,
        "dishes": dishes,
    }


def parse_source(source, lines, now):
    """Select and run the correct restaurant parser."""

    parsers = {
        "slatina": parse_slatina,
        "turanka": parse_turanka,
        "jomsom": parse_jomsom,
    }

    parser = parsers.get(
        source["id"],
        parse_generic,
    )

    menu = parser(lines, now)

    if not menu["soups"] and not menu["dishes"]:
        raise ValueError(
            "No soups or dishes were extracted."
        )

    return menu


def format_name_and_price(item):
    """Format a menu item with a consistent name and price."""

    item_name = html.escape(item["name"])
    item_price = html.escape(item.get("price", ""))

    if item_price:
        return (
            f"{item_name}, "
            f"<strong>{item_price}</strong>"
        )

    return item_name


def format_menu(source, menu):
    """Create consistent HTML output for Power Automate and Teams."""

    output = [
        f"<h2>🍽️ {html.escape(source['name'])}</h2>",
        (
            f"<p><strong>{html.escape(menu['day'])} "
            f"{html.escape(menu['date'])}</strong></p>"
        ),
    ]

    if menu["soups"]:
        output.append("<h3>🍲 Polévky</h3>")
        output.append("<ul>")

        for soup in menu["soups"]:
            output.append(
                "<li>"
                + format_name_and_price(soup)
                + "</li>"
            )

        output.append("</ul>")

    if menu["dishes"]:
        output.append("<h3>🍽️ Denní menu</h3>")
        output.append("<ol>")

        ordered_dishes = sorted(
            menu["dishes"],
            key=lambda dish: dish.get("number", 999),
        )

        for dish in ordered_dishes:
            output.append(
                "<li>"
                + format_name_and_price(dish)
                + "</li>"
            )

        output.append("</ol>")

    output.append(
        '<p>'
        + html.escape(source[        + '">Otevřít menu na webu</a></p>'
    )

    return "\n".join(output)


def add_rss_item(channel, source, menu, now):
    """Add a successfully parsed restaurant to the RSS feed."""

    item = ET.SubElement(channel, "item")

    title = (
        f"{source['name']} | "
        f"{menu['day']} {menu['date']}"
    )

    ET.SubElement(item, "title").text = title
    ET.SubElement(item, "link").text = source["url"]

    guid_input = (
        f"{source['id']}:{now.strftime('%Y-%m-%d')}"
    )

    guid_value = hashlib.sha256(
        guid_input.encode("utf-8")
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

    description.text = format_menu(
        source,
        menu,
    )


def add_error_item(channel, source, error, now):
    """Add a source error as an RSS entry."""

    item = ET.SubElement(channel, "item")

    ET.SubElement(item, "title").text = (
        f"{source['name']} | menu se nepodařilo načíst"
    )

    ET.SubElement(item, "link").text = source["url"]

    guid = ET.SubElement(
        item,
        "guid",
        {"isPermaLink": "false"},
    )

    guid.text = (
        f"{source['id']}-error-"
        f"{now.strftime('%Y-%m-%d')}"
    )

    ET.SubElement(item, "pubDate").text = (
        format_datetime(now)
    )

    description = ET.SubElement(
        item,
        "description",
    )

    description.text = (
        "<strong>Menu se nepodařilo načíst.</strong>"
        "<br>"
        + html.escape(str(error))
    )


def create_rss():
    """Download all sources and generate the combined RSS feed."""

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

    ET.SubElement(channel, "link").text = FEED_URL

    ET.SubElement(channel, "description").text = (
        "Denní menu vybraných restaurací"
    )

    ET.SubElement(channel, "language").text = "cs-CZ"

    ET.SubElement(channel, "lastBuildDate").text = (
        format_datetime(now)
    )

    ET.SubElement(channel, "ttl").text = "60"

    successful = 0
    failed = 0

    for source in sources:
        print()
        print(f"Loading: {source['name']}")
        print(f"URL: {source['url']}")

        try:
            page_html = download_page(source["url"])
            lines = html_to_lines(page_html)
            menu = parse_source(source, lines, now)

            add_rss_item(
                channel,
                source,
                menu,
                now,
            )

            successful += 1

            print(
                f"Soups found: {len(menu['soups'])}"
            )
            print(
                f"Dishes found: {len(menu['dishes'])}"
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
