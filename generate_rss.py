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
    "Mozilla/5.0 "
    "(compatible; LunchMenuRSS/1.0; "
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

    required_fields = {
        "id",
        "name",
        "url",
    }

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

        source_id = str(source["id"]).strip()
        source_name = str(source["name"]).strip()
        source_url = str(source["url"]).strip()

        if not source_id:
            raise ValueError(
                "Source ID must not be empty."
            )

        if not source_name:
            raise ValueError(
                f"Source name must not be empty: {source_id}"
            )

        if source_id in known_ids:
            raise ValueError(
                f"Duplicate source ID: {source_id}"
            )

        if not source_url.startswith(
            ("https://", "http://")
        ):
            raise ValueError(
                f"Invalid URL for {source_name}: {source_url}"
            )

        known_ids.add(source_id)

    return sources


def download_page(url):
    """Download a restaurant web page."""

    response = requests.get(
        url,
        timeout=30,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,"
                "application/xhtml+xml"
            ),
            "Accept-Language": (
                "cs-CZ,cs;q=0.9,en;q=0.8"
            ),
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

    soup = BeautifulSoup(
        page_html,
        "html.parser",
    )

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

    raw_text = soup.get_text(
        "\n",
        strip=True,
    )

    for line in raw_text.splitlines():
        line = re.sub(
            r"\s+",
            " ",
            line,
        ).strip()

        if not line:
            continue

        if not lines or lines[-1] != line:
            lines.append(line)

    return lines


def normalize_text(value):
    """Normalize text for reliable comparisons."""

    value = str(value).casefold()
    value = value.replace("\u00a0", " ")
    value = value.replace("–", "-")
    value = value.replace("—", "-")
    value = re.sub(r"\s+", "", value)

    return value


def get_date_text(now):
    """Return today's date in Czech display format."""

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
    """Return True when text contains today's date."""

    normalized_value = normalize_text(value)

    return any(
        normalize_text(date_value) in normalized_value
        for date_value in date_variants(now)
    )


def contains_any_full_date(value):
    """Return True when text contains a full Czech-style date."""

    normalized_value = normalize_text(value)

    return bool(
        re.search(
            r"(0?[1-9]|[12][0-9]|3[01])\."
            r"(0?[1-9]|1[0-2])\."
            r"20[0-9]{2}",
            normalized_value,
        )
    )


def is_date_line(value):
    """Return True when a line contains only a date."""

    normalized_value = normalize_text(value)

    return bool(
        re.fullmatch(
            r"(0?[1-9]|[12][0-9]|3[01])\."
            r"(0?[1-9]|1[0-2])\."
            r"20[0-9]{2}",
            normalized_value,
        )
    )


def is_date_range_line(value):
    """Return True when a line contains a weekly date range."""

    value = str(value).strip()

    return bool(
        re.fullmatch(
            r"\d{1,2}\.\s*\d{1,2}\.\s*"
            r"[–—-]\s*"
            r"\d{1,2}\.\s*\d{1,2}\.\s*"
            r"\d{4}",
            value,
        )
    )


def is_weekday_line(value):
    """Return True when a line is a Czech weekday heading."""

    normalized_value = normalize_text(value)

    weekday_variants = {
        normalize_text(day)
        for day in DAY_NAMES
    }

    weekday_variants.update(
        normalize_text(day)
        for day in SPACED_DAY_NAMES
    )

    return normalized_value in weekday_variants


def find_text(lines, search_text, start=0):
    """Find the first line containing specified text."""

    normalized_search = normalize_text(search_text)

    for index in range(start, len(lines)):
        if normalized_search in normalize_text(lines[index]):
            return index

    return None


def find_exact_text(lines, search_text, start=0):
    """Find the first line exactly matching specified text."""

    normalized_search = normalize_text(search_text)

    for index in range(start, len(lines)):
        if normalize_text(lines[index]) == normalized_search:
            return index

    return None


def looks_like_price(value):
    """Return True when a line contains only a Czech menu price."""

    value = str(value).strip()

    return bool(
        re.fullmatch(
            r"\d+\s*(Kč|,-|-)",
            value,
            flags=re.IGNORECASE,
        )
    )


def normalize_price(value):
    """Normalize a price to the format NUMBER Kč."""

    match = re.search(
        r"\d+",
        str(value),
    )

    if not match:
        return str(value).strip()

    return f"{match.group(0)} Kč"


def clean_name(value):
    """Clean a dish or soup name."""

    value = re.sub(
        r"\s+",
        " ",
        str(value),
    ).strip()

    value = re.sub(
        r",?\s*Orientační energetická hodnota porce:"
        r"\s*\d+\s*Kcal",
        "",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

    return value.strip(":- ")


def remove_common_noise(lines):
    """Remove navigation, schedule, and utility lines."""

    ignored_values = {
        normalize_text("Každý všední den"),
        normalize_text("11:00 - 15:00"),
        normalize_text("10:00 - 14:00"),
        normalize_text("10:30 - 14:30"),
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


def find_today_section(lines, now, stop_texts=None):
    """Extract text from today's heading to the following day."""

    if stop_texts is None:
        stop_texts = []

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

    normalized_stop_texts = {
        normalize_text(value)
        for value in stop_texts
    }

    for index in range(start + 1, len(lines)):
        line = lines[index]
        normalized_line = normalize_text(line)

        if contains_any_full_date(line):
            end = index

            if (
                index > start
                and is_weekday_line(lines[index - 1])
            ):
                end = index - 1

            break

        if normalized_line in normalized_stop_texts:
            end = index
            break

    result = lines[start:end]

    if (
        start > 0
        and is_weekday_line(lines[start - 1])
    ):
        result.insert(
            0,
            lines[start - 1],
        )

    return remove_common_noise(result)


def parse_numbered_menu(lines):
    """Parse soups and numbered dishes from separate text lines."""

    soups = []
    dishes = []

    soup_headings = {
        normalize_text("Polévka"),
        normalize_text("Polévka:"),
        normalize_text("Polévky"),
    }

    soup_heading_index = None

    for index, line in enumerate(lines):
        if normalize_text(line) in soup_headings:
            soup_heading_index = index
            break

    first_dish_index = None

    for index, line in enumerate(lines):
        if line.strip().isdigit():
            first_dish_index = index
            break

    if soup_heading_index is not None:
        soup_end = (
            first_dish_index
            if first_dish_index is not None
            else len(lines)
        )

        index = soup_heading_index + 1

        while index < soup_end:
            soup_name = lines[index]

            if looks_like_price(soup_name):
                index += 1
                continue

            soup_price = ""

            if (
                index + 1 < soup_end
                and looks_like_price(lines[index + 1])
            ):
                soup_price = normalize_price(
                    lines[index + 1]
                )
                index += 1

            soup_name = clean_name(soup_name)

            if soup_name:
                soups.append(
                    {
                        "name": soup_name,
                        "price": soup_price,
                    }
                )

            index += 1

    if first_dish_index is not None:
        index = first_dish_index

        while index < len(lines):
            if not lines[index].strip().isdigit():
                index += 1
                continue

            number = int(lines[index].strip())
            index += 1

            name_parts = []
            price = ""

            while index < len(lines):
                current_line = lines[index]

                if current_line.strip().isdigit():
                    break

                if looks_like_price(current_line):
                    price = normalize_price(current_line)
                    index += 1
                    break

                name_parts.append(current_line)
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
    """Split text into menu blocks ending with a price."""

    blocks = []
    current_block = []

    for line in lines:
        current_block.append(line)

        if looks_like_price(line):
            blocks.append(current_block)
            current_block = []

    return blocks


def is_metadata_line(value):
    """Return True for weight, volume, allergens, or calorie metadata."""

    value = str(value).strip()

    patterns = [
        r"^\d+[a-z]?(,\d+[a-z]?)*$",
        r"^\d+([,.]\d+)?\s*l$",
        r"^\d+([,.]\d+)?\s*g$",
        r"^orientační energetická hodnota",
    ]

    return any(
        re.search(
            pattern,
            value,
            flags=re.IGNORECASE,
        )
        for pattern in patterns
    )


def block_to_menu_item(block):
    """Convert a price-terminated text block into a menu item."""

    if not block:
        return None

    price = ""

    if looks_like_price(block[-1]):
        price = normalize_price(block[-1])
        content_lines = block[:-1]
    else:
        content_lines = block

    name_parts = []

    for line in content_lines:
        if is_metadata_line(line):
            continue

        cleaned_line = clean_name(line)

        if cleaned_line:
            name_parts.append(cleaned_line)

    name = clean_name(
        " ".join(name_parts)
    )

    if not name:
        return None

    return {
        "name": name,
        "price": price,
    }


def is_valid_weekly_item(item):
    """Reject footer, allergen, date-range, and contact content."""

    name = item.get("name", "")
    normalized_name = normalize_text(name)
    price = item.get("price", "").strip()

    rejected_phrases = [
        "seznamalergenů",
        "váhymasa",
        "obědovánabídkaplatí",
        "odměňujemevěrnost",
        "věrnostníprogram",
        "kontaktníinformace",
        "těšímesenavás",
        "kdenásnajdete",
        "otevíracídoba",
        "vašejméno",
        "váše-mail",
        "vašezpráva",
        "bezlaktózy",
        "bezmouky",
    ]

    if is_date_range_line(name):
        return False

    if any(
        phrase in normalized_name
        for phrase in rejected_phrases
    ):
        return False

    if not price:
        return False

    return True


def parse_sectioned_menu(lines):
    """Parse menu sections where every item ends with a price."""

    soups = []
    dishes = []

    current_section = "dishes"
    current_block = []

    soup_headings = {
        normalize_text("Polévka"),
        normalize_text("Polévka:"),
        normalize_text("Polévky"),
    }

    dish_headings = {
        normalize_text("Hlavní chod"),
        normalize_text("Hlavní chody"),
        normalize_text("Poke"),
        normalize_text("Salát"),
        normalize_text("Saláty"),
        normalize_text("Dezert"),
        normalize_text("Dezerty"),
        normalize_text("Pizza"),
    }

    ignored_headings = {
        normalize_text("Denní menu"),
        normalize_text("Týdenní menu"),
    }

    def save_current_block():
        """Save the currently collected menu block."""

        nonlocal current_block

        if not current_block:
            return

        item = block_to_menu_item(current_block)
        current_block = []

        if not item:
            return

        if current_section == "soups":
            soups.append(item)
        else:
            item["number"] = len(dishes) + 1
            dishes.append(item)

    for line in lines:
        normalized_line = normalize_text(line)

        if normalized_line in soup_headings:
            save_current_block()
            current_section = "soups"
            continue

        if normalized_line in dish_headings:
            save_current_block()
            current_section = "dishes"
            continue

        if normalized_line in ignored_headings:
            save_current_block()
            continue

        if is_date_range_line(line):
            continue

        current_block.append(line)

        if looks_like_price(line):
            save_current_block()

    save_current_block()

    soups = [
        item
        for item in soups
        if is_valid_weekly_item(item)
    ]

    dishes = [
        item
        for item in dishes
        if is_valid_weekly_item(item)
    ]

    for number, dish in enumerate(
        dishes,
        start=1,
    ):
        dish["number"] = number

    return soups, dishes


def parse_slatina(lines, now):
    """Parse today's Slatina Bistro menu."""

    section = find_today_section(
        lines,
        now,
        stop_texts=[
            "Snídaně",
            "Nabídka obědů na celý týden",
        ],
    )

    soups, dishes = parse_numbered_menu(section)

    return {
        "day": DAY_NAMES[now.weekday()],
        "date": get_date_text(now),
        "soups": soups,
        "dishes": dishes,
        "weekly_soups": [],
        "weekly_dishes": [],
    }

def parse_turanka(lines, now):
    """Parse today's and weekly menus from Tackarna Turanka."""

    daily_soups = []
    daily_dishes = []
    weekly_soups = []
    weekly_dishes = []

    # Find all occurrences of today's date.
    # The page contains today's date in the day selector and again
    # next to the actual daily menu. The last occurrence is normally
    # the one belonging to the real menu content.
    today_indexes = [
        index
        for index, line in enumerate(lines)
        if contains_today(line, now)
    ]

    if not today_indexes:
        raise ValueError(
            "Today's date was not found on the Tackarna page."
        )

    daily_start = None

    # Prefer an occurrence followed by a soup or main-course heading.
    for candidate_index in reversed(today_indexes):
        candidate_end = min(
            candidate_index + 15,
            len(lines),
        )

        nearby_lines = lines[
            candidate_index:candidate_end
        ]

        has_menu_heading = any(
            normalize_text(line) in {
                normalize_text("Polévka"),
                normalize_text("Polévka:"),
                normalize_text("Polévky"),
                normalize_text("Hlavní chod"),
                normalize_text("Hlavní chody"),
            }
            for line in nearby_lines
        )

        if has_menu_heading:
            daily_start = candidate_index
            break

    if daily_start is None:
        daily_start = today_indexes[-1]

    # Stop the daily menu before the next date or weekly menu heading.
    daily_end = len(lines)

    for index in range(
        daily_start + 1,
        len(lines),
    ):
        normalized_line = normalize_text(
            lines[index]
        )

        if normalized_line == normalize_text(
            "Týdenní menu"
        ):
            daily_end = index
            break

        if contains_any_full_date(lines[index]):
            daily_end = index

            if (
                index > daily_start
                and is_weekday_line(lines[index - 1])
            ):
                daily_end = index - 1

            break

    daily_section = lines[
        daily_start:daily_end
    ]

    daily_section = remove_common_noise(
        daily_section
    )

    daily_soup_start = find_exact_text(
        daily_section,
        "Polévka",
    )

    if daily_soup_start is None:
        daily_soup_start = find_exact_text(
            daily_section,
            "Polévka:",
        )

    if daily_soup_start is None:
        daily_soup_start = find_exact_text(
            daily_section,
            "Polévky",
        )

    daily_main_start = find_exact_text(
        daily_section,
        "Hlavní chod",
    )

    if daily_main_start is None:
        daily_main_start = find_exact_text(
            daily_section,
            "Hlavní chody",
        )

    # Parse daily soups.
    if daily_soup_start is not None:
        daily_soup_end = (
            daily_main_start
            if daily_main_start is not None
            else len(daily_section)
        )

        daily_soup_lines = daily_section[
            daily_soup_start + 1:daily_soup_end
        ]

        for block in split_blocks_by_price(
            daily_soup_lines
        ):
            item = block_to_menu_item(block)

            if item:
                daily_soups.append(item)

    # Parse daily main dishes.
    if daily_main_start is not None:
        daily_main_lines = daily_section[
            daily_main_start + 1:
        ]

        for block in split_blocks_by_price(
            daily_main_lines
        ):
            item = block_to_menu_item(block)

            if not item:
                continue

            item["number"] = len(daily_dishes) + 1
            daily_dishes.append(item)

    # Find the exact weekly-menu heading after the daily menu.
    weekly_start = find_exact_text(
        lines,
        "Týdenní menu",
        start=daily_start,
    )

    if weekly_start is not None:
        weekly_end = len(lines)

        stop_phrases = [
            "Seznam alergenů",
            "Váhy masa",
            "Obědová nabídka platí",
            "Odměňujeme věrnost",
            "Věrnostní program",
            "Kontaktní informace",
            "Těšíme se na vás",
            "Kde nás najdete",
            "Otevírací doba",
            "Kontakty",
            "Vaše jméno",
        ]

        normalized_stop_phrases = [
            normalize_text(value)
            for value in stop_phrases
        ]

        for index in range(
            weekly_start + 1,
            len(lines),
        ):
            normalized_line = normalize_text(
                lines[index]
            )

            if any(
                stop_phrase in normalized_line
                for stop_phrase in normalized_stop_phrases
            ):
                weekly_end = index
                break

        weekly_lines = lines[
            weekly_start + 1:weekly_end
        ]

        weekly_lines = remove_common_noise(
            weekly_lines
        )

        # Remove the weekly date range from the list of dishes.
        weekly_lines = [
            line
            for line in weekly_lines
            if not is_date_range_line(line)
        ]

        weekly_soups, weekly_dishes = (
            parse_sectioned_menu(weekly_lines)
        )

    if not daily_soups and not daily_dishes:
        print(
            "Warning: No daily Tackarna menu items were extracted."
        )

    return {
        "day": DAY_NAMES[now.weekday()],
        "date": get_date_text(now),
        "soups": daily_soups,
        "dishes": daily_dishes,
        "weekly_soups": weekly_soups,
        "weekly_dishes": weekly_dishes,
    }


def extract_jomsom_section(lines, now):
    """Extract today's weekday section from the Jomsom page."""

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
        normal_day = normalize_text(
            DAY_NAMES[now.weekday()]
        )

        for index, line in enumerate(lines):
            if normalize_text(line) == normal_day:
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


def parse_jomsom_blocks(lines):
    """Parse Jomsom items ending with the PRICE Kč pattern."""

    combined_text = " ".join(lines)

    combined_text = re.sub(
        r"\s+",
        " ",
        combined_text,
    ).strip()

    item_pattern = re.compile(
        r"(.*?)(?::\s*-\s*|:-)\s*"
        r"(\d+)\s*Kč",
        flags=re.IGNORECASE,
    )

    items = []
    remaining_text = combined_text

    while remaining_text:
        match = item_pattern.search(remaining_text)

        if not match:
            break

        item_name = clean_name(
            match.group(1)
        )

        item_price = f"{match.group(2)} Kč"

        if item_name:
            items.append(
                {
                    "name": item_name,
                    "price": item_price,
                }
            )

        remaining_text = remaining_text[
            match.end():
        ].strip()

    return items


def parse_jomsom(lines, now):
    """Parse today's Jomsom menu."""

    section = extract_jomsom_section(
        lines,
        now,
    )

    parsed_items = parse_jomsom_blocks(
        section
    )

    soups = []
    dishes = []

    for item in parsed_items:
        normalized_name = normalize_text(
            item["name"]
        )

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
                    "number": int(
                        number_match.group(1)
                    ),
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
        "weekly_soups": [],
        "weekly_dishes": [],
    }


def parse_generic(lines, now):
    """Parse an unknown restaurant using basic date extraction."""

    section = find_today_section(
        lines,
        now,
    )

    soups, dishes = parse_numbered_menu(
        section
    )

    return {
        "day": DAY_NAMES[now.weekday()],
        "date": get_date_text(now),
        "soups": soups,
        "dishes": dishes,
        "weekly_soups": [],
        "weekly_dishes": [],
    }


def parse_source(source, lines, now):
    """Select and run a parser based on the source ID."""

    parsers = {
        "slatina": parse_slatina,
        "turanka": parse_turanka,
        "jomsom": parse_jomsom,
    }

    parser = parsers.get(
        source["id"],
        parse_generic,
    )

    menu = parser(
        lines,
        now,
    )

    has_content = any(
        [
            menu.get("soups"),
            menu.get("dishes"),
            menu.get("weekly_soups"),
            menu.get("weekly_dishes"),
        ]
    )

    if not has_content:
        raise ValueError(
            "No soups or dishes were extracted."
        )

    return menu


def format_name_and_price(item):
    """Format an item with a normal, non-bold price."""

    item_name = html.escape(
        item["name"]
    )

    item_price = html.escape(
        item.get("price", "")
    )

    if item_price:
        return f"{item_name}, {item_price}"

    return item_name


def append_numbered_dishes(output, dishes):
    """Append dishes with only their list numbers in bold."""

    ordered_dishes = sorted(
        dishes,
        key=lambda dish: dish.get(
            "number",
            999,
        ),
    )

    for position, dish in enumerate(
        ordered_dishes,
        start=1,
    ):
        number = dish.get(
            "number",
            position,
        )

        output.append(
            "<p>"
            f"<strong>{number}.</strong> "
            + format_name_and_price(dish)
            + "</p>"
        )


def format_menu(source, menu):
    """Create consistent HTML output for RSS and Teams."""

    output = [
        (
            "<h2>🍽️ "
            + html.escape(source["name"])
            + "</h2>"
        ),
        (
            "<p>"
            + html.escape(menu["day"])
            + " "
            + html.escape(menu["date"])
            + "</p>"
        ),
    ]

    soups = menu.get(
        "soups",
        [],
    )

    dishes = menu.get(
        "dishes",
        [],
    )

    weekly_soups = menu.get(
        "weekly_soups",
        [],
    )

    weekly_dishes = menu.get(
        "weekly_dishes",
        [],
    )

    if soups:
        output.append(
            "<h3>🍲 Polévky</h3>"
        )

        for soup in soups:
            output.append(
                "<p>"
                + format_name_and_price(soup)
                + "</p>"
            )

    if dishes:
        output.append(
            "<h3>🍽️ Denní menu</h3>"
        )

        append_numbered_dishes(
            output,
            dishes,
        )

    if weekly_soups or weekly_dishes:
        output.append(
            "<h3>📅 Týdenní menu</h3>"
        )

    if weekly_soups:
        output.append(
            "<h4>🍲 Polévky</h4>"
        )

        for soup in weekly_soups:
            output.append(
                "<p>"
                + format_name_and_price(soup)
                + "</p>"
            )

    if weekly_dishes:
        output.append(
            "<h4>🍽️ Nabídka na celý týden</h4>"
        )

        append_numbered_dishes(
            output,
            weekly_dishes,
        )

    # The source link is available in the RSS item's link element.
    # Power Automate should use the Primary feed link dynamic value.
    return "\n".join(output)


def add_rss_item(channel, source, menu, now):
    """Add a successfully parsed restaurant to the RSS feed."""

    item = ET.SubElement(
        channel,
        "item",
    )

    item_title = (
        f"{source['name']} | "
        f"{menu['day']} "
        f"{menu['date']}"
    )

    ET.SubElement(
        item,
        "title",
    ).text = item_title

    ET.SubElement(
        item,
        "link",
    ).text = source["url"]

    guid_input = (
        f"{source['id']}:"
        f"{now.strftime('%Y-%m-%d')}"
    )

    guid_value = hashlib.sha256(
        guid_input.encode("utf-8")
    ).hexdigest()

    guid = ET.SubElement(
        item,
        "guid",
        {
            "isPermaLink": "false",
        },
    )

    guid.text = guid_value

    ET.SubElement(
        item,
        "pubDate",
    ).text = format_datetime(now)

    description = ET.SubElement(
        item,
        "description",
    )

    description.text = format_menu(
        source,
        menu,
    )


def add_error_item(channel, source, error, now):
    """Add a source loading error as an RSS item."""

    item = ET.SubElement(
        channel,
        "item",
    )

    ET.SubElement(
        item,
        "title",
    ).text = (
        f"{source['name']} | "
        "menu se nepodařilo načíst"
    )

    ET.SubElement(
        item,
        "link",
    ).text = source["url"]

    guid = ET.SubElement(
        item,
        "guid",
        {
            "isPermaLink": "false",
        },
    )

    guid.text = (
        f"{source['id']}-error-"
        f"{now.strftime('%Y-%m-%d')}"
    )

    ET.SubElement(
        item,
        "pubDate",
    ).text = format_datetime(now)

    description = ET.SubElement(
        item,
        "description",
    )

    description.text = (
        "<strong>"
        "Menu se nepodařilo načíst."
        "</strong><br>"
        + html.escape(str(error))
    )


def create_rss():
    """Download all sources and generate the combined RSS feed."""

    now = datetime.now(
        TIME_ZONE
    )

    sources = load_sources()

    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
        },
    )

    channel = ET.SubElement(
        rss,
        "channel",
    )

    ET.SubElement(
        channel,
        "title",
    ).text = (
        "Polední menu restaurací"
    )

    ET.SubElement(
        channel,
        "link",
    ).text = FEED_URL

    ET.SubElement(
        channel,
        "description",
    ).text = (
        "Denní menu vybraných restaurací"
    )

    ET.SubElement(
        channel,
        "language",
    ).text = "cs-CZ"

    ET.SubElement(
        channel,
        "lastBuildDate",
    ).text = format_datetime(now)

    ET.SubElement(
        channel,
        "ttl",
    ).text = "60"

    successful = 0
    failed = 0

    for source in sources:
        print()
        print(
            f"Loading: {source['name']}"
        )

        print(
            f"URL: {source['url']}"
        )

        try:
            page_html = download_page(
                source["url"]
            )

            lines = html_to_lines(
                page_html
            )

            menu = parse_source(
                source,
                lines,
                now,
            )

            add_rss_item(
                channel,
                source,
                menu,
                now,
            )

            successful += 1

            print(
                "Soups found: "
                f"{len(menu.get('soups', []))}"
            )

            print(
                "Daily dishes found: "
                f"{len(menu.get('dishes', []))}"
            )

            print(
                "Weekly soups found: "
                f"{len(menu.get('weekly_soups', []))}"
            )

            print(
                "Weekly dishes found: "
                f"{len(menu.get('weekly_dishes', []))}"
            )

        except Exception as error:
            failed += 1

            print(
                f"Error for "
                f"{source['name']}: "
                f"{error}"
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
        ET.indent(
            tree,
            space="  ",
        )
    except AttributeError:
        pass

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )

    print()
    print(
        f"RSS created: {OUTPUT_FILE}"
    )

    print(
        f"Successfully loaded: {successful}"
    )

    print(
        f"Failed: {failed}"
    )


if __name__ == "__main__":
    create_rss()
