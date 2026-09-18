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
        raise FileNotFoundError(f"Source file {SOURCES_FILE} was not found.")

    sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))

    if not isinstance(sources, list):
        raise ValueError("sources.json must contain a JSON array.")

    required_fields = {"id", "name", "url"}
    known_ids = set()

    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Every source must be a JSON object.")

        missing_fields = required_fields - set(source)
        if missing_fields:
            raise ValueError(
                "Source is missing required fields: "
                + ", ".join(sorted(missing_fields))
            )

        source_id = str(source["id"]).strip()
        source_name = str(source["name"]).strip()
        source_url = str(source["url"]).strip()

        if not source_id:
            raise ValueError("Source ID must not be empty.")
        if not source_name:
            raise ValueError(f"Source name must not be empty: {source_id}")
        if source_id in known_ids:
            raise ValueError(f"Duplicate source ID: {source_id}")
        if not source_url.startswith(("https://", "http://")):
            raise ValueError(f"Invalid URL for {source_name}: {source_url}")

        source["id"] = source_id
        source["name"] = source_name
        source["url"] = source_url
        known_ids.add(source_id)

    return sources


def download_page(url):
    """Download a restaurant web page and return its HTML."""

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
    response.encoding = response.apparent_encoding or response.encoding or "utf-8"
    return response.text


def html_to_lines(page_html):
    """Convert HTML into cleaned visible text lines."""

    soup = BeautifulSoup(page_html, "html.parser")

    for element in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        element.decompose()

    lines = []
    for line in soup.get_text("\n", strip=True).splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)

    return lines


def normalize_text(value):
    """Normalize text for reliable comparisons."""

    value = str(value).casefold()
    value = value.replace("\u00a0", " ").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", "", value)


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

    return bool(
        re.search(
            r"(0?[1-9]|[12][0-9]|3[01])\."
            r"(0?[1-9]|1[0-2])\."
            r"20[0-9]{2}",
            normalize_text(value),
        )
    )


def is_date_range_line(value):
    """Return True when a line contains a weekly date range."""

    return bool(
        re.fullmatch(
            r"\d{1,2}\.\s*\d{1,2}\.\s*[–—-]\s*"
            r"\d{1,2}\.\s*\d{1,2}\.\s*\d{4}",
            str(value).strip(),
        )
    )


def is_weekday_line(value):
    """Return True when a line is a Czech weekday heading."""

    normalized = normalize_text(value)
    variants = {normalize_text(day) for day in DAY_NAMES + SPACED_DAY_NAMES}
    return normalized in variants


def find_text(lines, search_text, start=0):
    """Find the first line containing the specified text."""

    needle = normalize_text(search_text)
    for index in range(start, len(lines)):
        if needle in normalize_text(lines[index]):
            return index
    return None


def find_exact_text(lines, search_text, start=0):
    """Find the first line exactly matching the specified text."""

    needle = normalize_text(search_text)
    for index in range(start, len(lines)):
        if normalize_text(lines[index]) == needle:
            return index
    return None


def looks_like_price(value):
    """Return True when a line contains only a Czech menu price."""

    return bool(
        re.fullmatch(
            r"\d+\s*(Kč|,-|-)",
            str(value).strip(),
            flags=re.IGNORECASE,
        )
    )


def normalize_price(value):
    """Normalize a price to NUMBER Kč."""

    match = re.search(r"\d+", str(value))
    return f"{match.group(0)} Kč" if match else str(value).strip()


def clean_name(value):
    """Clean a dish or soup name and remove calorie information."""

    value = re.sub(r"\s+", " ", str(value)).strip()
    value = re.sub(
        r",?\s*Orientační energetická hodnota porce:\s*\d+\s*Kcal",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", value).strip().strip(":- ")


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


def find_today_section(lines, now, stop_texts=None, prefer_last=False):
    """Extract text from today's heading to the following day or stop marker."""

    stop_texts = stop_texts or []
    candidates = [
        index for index, line in enumerate(lines) if contains_today(line, now)
    ]

    if not candidates:
        raise ValueError("Today's date was not found on the page.")

    start = candidates[-1] if prefer_last else candidates[0]
    end = len(lines)
    normalized_stops = {normalize_text(value) for value in stop_texts}

    for index in range(start + 1, len(lines)):
        line = lines[index]
        normalized_line = normalize_text(line)

        if contains_any_full_date(line):
            end = index - 1 if is_weekday_line(lines[index - 1]) else index
            break

        if normalized_line in normalized_stops:
            end = index
            break

    result = lines[start:end]
    if start > 0 and is_weekday_line(lines[start - 1]):
        result.insert(0, lines[start - 1])

    return remove_common_noise(result)


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
    return any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in patterns)


def block_to_menu_item(block):
    """Convert a price-terminated text block into a menu item."""

    if not block or not looks_like_price(block[-1]):
        return None

    price = normalize_price(block[-1])
    name_parts = []

    for line in block[:-1]:
        if is_metadata_line(line) or is_date_range_line(line):
            continue
        cleaned_line = clean_name(line)
        if cleaned_line:
            name_parts.append(cleaned_line)

    name = clean_name(" ".join(name_parts))
    if not name:
        return None

    return {"name": name, "price": price}


def parse_numbered_menu(lines):
    """Parse soups and dishes where dish numbers are separate lines."""

    soups = []
    dishes = []
    soup_headings = {
        normalize_text("Polévka"),
        normalize_text("Polévka:"),
        normalize_text("Polévky"),
    }

    soup_heading = next(
        (i for i, line in enumerate(lines) if normalize_text(line) in soup_headings),
        None,
    )
    first_dish = next(
        (i for i, line in enumerate(lines) if line.strip().isdigit()),
        None,
    )

    if soup_heading is not None:
        soup_end = first_dish if first_dish is not None else len(lines)
        for block in split_blocks_by_price(lines[soup_heading + 1:soup_end]):
            item = block_to_menu_item(block)
            if item:
                soups.append(item)

    if first_dish is not None:
        index = first_dish
        while index < len(lines):
            if not lines[index].strip().isdigit():
                index += 1
                continue

            number = int(lines[index].strip())
            index += 1
            block = []

            while index < len(lines):
                if lines[index].strip().isdigit():
                    break
                block.append(lines[index])
                index += 1
                if block and looks_like_price(block[-1]):
                    break

            item = block_to_menu_item(block)
            if item:
                item["number"] = number
                dishes.append(item)

    return soups, dishes


def parse_category_sections(lines):
    """Parse soup, regular dish, and pizza sections ending with prices."""

    soups = []
    dishes = []
    pizzas = []
    current_category = "dishes"
    current_block = []

    soup_headings = {normalize_text(x) for x in ["Polévka", "Polévka:", "Polévky"]}
    pizza_headings = {normalize_text(x) for x in ["Pizza", "Pizzy"]}
    dish_headings = {
        normalize_text(x)
        for x in [
            "Hlavní chod",
            "Hlavní chody",
            "Poke",
            "Salát",
            "Saláty",
            "Dezert",
            "Dezerty",
        ]
    }
    ignored_headings = {
        normalize_text("Denní menu"),
        normalize_text("Týdenní menu"),
    }

    def save_block():
        nonlocal current_block
        item = block_to_menu_item(current_block)
        current_block = []
        if not item:
            return

        if current_category == "soups":
            soups.append(item)
        elif current_category == "pizzas":
            item["number"] = len(pizzas) + 1
            pizzas.append(item)
        else:
            item["number"] = len(dishes) + 1
            dishes.append(item)

    for line in lines:
        normalized = normalize_text(line)

        if normalized in soup_headings:
            save_block()
            current_category = "soups"
            continue
        if normalized in pizza_headings:
            save_block()
            current_category = "pizzas"
            continue
        if normalized in dish_headings:
            save_block()
            current_category = "dishes"
            continue
        if normalized in ignored_headings or is_date_range_line(line):
            save_block()
            continue

        current_block.append(line)
        if looks_like_price(line):
            save_block()

    save_block()
    return soups, dishes, pizzas


def is_valid_turanka_item(item):
    """Reject footer, contact, allergen, and other non-menu content."""

    name = normalize_text(item.get("name", ""))
    price = item.get("price", "").strip()
    rejected = [
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
    return bool(price) and not any(phrase in name for phrase in rejected)


def extract_turanka_daily_section(lines, now):
    """Find the real daily section, not the date selector at the top."""

    candidates = [
        index for index, line in enumerate(lines) if contains_today(line, now)
    ]
    if not candidates:
        raise ValueError("Today's date was not found on the Tackarna page.")

    headings = {
        normalize_text("Polévka"),
        normalize_text("Polévky"),
        normalize_text("Hlavní chod"),
        normalize_text("Hlavní chody"),
    }

    daily_start = None
    for candidate in reversed(candidates):
        nearby = lines[candidate:min(candidate + 20, len(lines))]
        if any(normalize_text(line) in headings for line in nearby):
            daily_start = candidate
            break

    if daily_start is None:
        daily_start = candidates[-1]

    daily_end = len(lines)
    for index in range(daily_start + 1, len(lines)):
        if normalize_text(lines[index]) == normalize_text("Týdenní menu"):
            daily_end = index
            break
        if contains_any_full_date(lines[index]):
            daily_end = index - 1 if is_weekday_line(lines[index - 1]) else index
            break

    return remove_common_noise(lines[daily_start:daily_end])


def parse_turanka_daily(lines, now):
    """Parse today's Tackarna soup and daily dishes."""

    section = extract_turanka_daily_section(lines, now)
    soup_start = find_exact_text(section, "Polévka")
    if soup_start is None:
        soup_start = find_exact_text(section, "Polévky")

    main_start = find_exact_text(section, "Hlavní chod")
    if main_start is None:
        main_start = find_exact_text(section, "Hlavní chody")

    soups = []
    dishes = []

    if soup_start is not None:
        soup_end = main_start if main_start is not None else len(section)
        for block in split_blocks_by_price(section[soup_start + 1:soup_end]):
            item = block_to_menu_item(block)
            if item:
                soups.append(item)

    if main_start is not None:
        for block in split_blocks_by_price(section[main_start + 1:]):
            item = block_to_menu_item(block)
            if item:
                item["number"] = len(dishes) + 1
                dishes.append(item)

    return soups, dishes


def parse_turanka_weekly(lines):
    """Parse Tackarna weekly soup, regular dishes, and pizzas."""

    weekly_start = find_exact_text(lines, "Týdenní menu")
    if weekly_start is None:
        return [], [], []

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
    normalized_stops = [normalize_text(value) for value in stop_phrases]

    weekly_end = len(lines)
    for index in range(weekly_start + 1, len(lines)):
        normalized_line = normalize_text(lines[index])
        if any(stop in normalized_line for stop in normalized_stops):
            weekly_end = index
            break

    weekly_lines = remove_common_noise(lines[weekly_start + 1:weekly_end])
    weekly_lines = [line for line in weekly_lines if not is_date_range_line(line)]

    soups, dishes, pizzas = parse_category_sections(weekly_lines)
    soups = [item for item in soups if is_valid_turanka_item(item)]
    dishes = [item for item in dishes if is_valid_turanka_item(item)]
    pizzas = [item for item in pizzas if is_valid_turanka_item(item)]

    for number, item in enumerate(dishes, start=1):
        item["number"] = number
    for number, item in enumerate(pizzas, start=1):
        item["number"] = number

    return soups, dishes, pizzas


def parse_slatina(lines, now):
    """Parse today's Slatina Bistro menu."""

    section = find_today_section(
        lines,
        now,
        stop_texts=["Snídaně", "Nabídka obědů na celý týden"],
    )
    soups, dishes = parse_numbered_menu(section)
    return make_menu_result(now, soups, dishes)


def parse_turanka(lines, now):
    """Parse Tackarna daily menu, weekly menu, and pizza selection."""

    soups, dishes = parse_turanka_daily(lines, now)
    weekly_soups, weekly_dishes, pizzas = parse_turanka_weekly(lines)

    return make_menu_result(
        now,
        soups,
        dishes,
        weekly_soups=weekly_soups,
        weekly_dishes=weekly_dishes,
        pizzas=pizzas,
    )


def extract_jomsom_section(lines, now):
    """Extract today's weekday section from the Jomsom page."""

    target = normalize_text(SPACED_DAY_NAMES[now.weekday()])
    markers = {normalize_text(day) for day in SPACED_DAY_NAMES}
    start = next(
        (i for i, line in enumerate(lines) if normalize_text(line) == target),
        None,
    )

    if start is None:
        raise ValueError("Today's Jomsom weekday heading was not found.")

    end = len(lines)
    for index in range(start + 1, len(lines)):
        normalized = normalize_text(lines[index])
        if normalized in markers or "informaceopřítomnostialergenů" in normalized:
            end = index
            break

    return lines[start + 1:end]


def parse_jomsom(lines, now):
    """Parse today's Jomsom soup and numbered dishes."""

    text = re.sub(r"\s+", " ", " ".join(extract_jomsom_section(lines, now))).strip()
    pattern = re.compile(
        r"(.*?)(?::\s*-\s*|:-)\s*(\d+)\s*Kč",
        flags=re.IGNORECASE,
    )

    items = []
    remaining = text
    while remaining:
        match = pattern.search(remaining)
        if not match:
            break
        name = clean_name(match.group(1))
        if name:
            items.append({"name": name, "price": f"{match.group(2)} Kč"})
        remaining = remaining[match.end():].strip()

    soups = []
    dishes = []
    for item in items:
        normalized = normalize_text(item["name"])
        if "soup" in normalized or "polévka" in normalized:
            soups.append(item)
            continue

        number_match = re.match(r"^(\d+)\.\s*(.*)$", item["name"])
        number = int(number_match.group(1)) if number_match else len(dishes) + 1
        name = clean_name(number_match.group(2)) if number_match else item["name"]
        dishes.append({"number": number, "name": name, "price": item["price"]})

    return make_menu_result(now, soups, dishes)


def parse_generic(lines, now):
    """Parse an unknown restaurant using basic date extraction."""

    section = find_today_section(lines, now)
    soups, dishes = parse_numbered_menu(section)
    return make_menu_result(now, soups, dishes)


def make_menu_result(
    now,
    soups=None,
    dishes=None,
    weekly_soups=None,
    weekly_dishes=None,
    pizzas=None,
):
    """Build one consistent menu data structure for every restaurant."""

    return {
        "day": DAY_NAMES[now.weekday()],
        "date": get_date_text(now),
        "soups": soups or [],
        "dishes": dishes or [],
        "weekly_soups": weekly_soups or [],
        "weekly_dishes": weekly_dishes or [],
        "pizzas": pizzas or [],
    }


def parse_source(source, lines, now):
    """Select and run a parser based on the source ID."""

    parsers = {
        "slatina": parse_slatina,
        "turanka": parse_turanka,
        "jomsom": parse_jomsom,
    }
    menu = parsers.get(source["id"], parse_generic)(lines, now)

    if not any(
        menu.get(key)
        for key in [
            "soups",
            "dishes",
            "weekly_soups",
            "weekly_dishes",
            "pizzas",
        ]
    ):
        raise ValueError("No menu items were extracted.")

    return menu


def format_name_and_price(item):
    """Format an item with a normal, non-bold price."""

    name = html.escape(item["name"])
    price = html.escape(item.get("price", ""))
    return f"{name}, {price}" if price else name


def append_numbered_items(output, items):
    """Append items with only the list number displayed in bold."""

    ordered = sorted(items, key=lambda item: item.get("number", 999))
    for position, item in enumerate(ordered, start=1):
        number = item.get("number", position)
        output.append(
            f"<p><strong>{number}.</strong> {format_name_and_price(item)}</p>"
        )


def format_menu(source, menu):
    """Create consistent HTML output for RSS and Teams."""

    output = [
        f"<h2>🍽️ {html.escape(source['name'])}</h2>",
        f"<p>{html.escape(menu['day'])} {html.escape(menu['date'])}</p>",
    ]

    if menu["soups"]:
        output.append("<h3>🍲 Polévky</h3>")
        for soup in menu["soups"]:
            output.append(f"<p>{format_name_and_price(soup)}</p>")

    if menu["dishes"]:
        output.append("<h3>🍽️ Denní menu</h3>")
        append_numbered_items(output, menu["dishes"])

    if menu["weekly_soups"] or menu["weekly_dishes"]:
        output.append("<h3>📅 Týdenní menu</h3>")

    if menu["weekly_soups"]:
        output.append("<h4>🍲 Polévky</h4>")
        for soup in menu["weekly_soups"]:
            output.append(f"<p>{format_name_and_price(soup)}</p>")

    if menu["weekly_dishes"]:
        output.append("<h4>🍽️ Nabídka na celý týden</h4>")
        append_numbered_items(output, menu["weekly_dishes"])

    if menu["pizzas"]:
        output.append("<h3>🍕 Pizza</h3>")
        append_numbered_items(output, menu["pizzas"])

    return "\n".join(output)


def add_rss_item(channel, source, menu, now):
    """Add a successfully parsed restaurant to the RSS feed."""

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = (
        f"{source['name']} | {menu['day']} {menu['date']}"
    )
    ET.SubElement(item, "link").text = source["url"]

    guid_input = f"{source['id']}:{now.strftime('%Y-%m-%d')}"
    guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
    guid.text = hashlib.sha256(guid_input.encode("utf-8")).hexdigest()

    ET.SubElement(item, "pubDate").text = format_datetime(now)
    ET.SubElement(item, "description").text = format_menu(source, menu)


def add_error_item(channel, source, error, now):
    """Add a source loading error as an RSS item."""

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = (
        f"{source['name']} | menu se nepodařilo načíst"
    )
    ET.SubElement(item, "link").text = source["url"]

    guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
    guid.text = f"{source['id']}-error-{now.strftime('%Y-%m-%d')}"

    ET.SubElement(item, "pubDate").text = format_datetime(now)
    ET.SubElement(item, "description").text = (
        "<strong>Menu se nepodařilo načíst.</strong><br>"
        + html.escape(str(error))
    )


def create_rss():
    """Download all sources and generate the combined RSS feed."""

    now = datetime.now(TIME_ZONE)
    sources = load_sources()

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Polední menu restaurací"
    ET.SubElement(channel, "link").text = FEED_URL
    ET.SubElement(channel, "description").text = (
        "Denní menu vybraných restaurací"
    )
    ET.SubElement(channel, "language").text = "cs-CZ"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(now)
    ET.SubElement(channel, "ttl").text = "60"

    successful = 0
    failed = 0

    for source in sources:
        print(f"\nLoading: {source['name']}\nURL: {source['url']}")
        try:
            page_html = download_page(source["url"])
            lines = html_to_lines(page_html)
            menu = parse_source(source, lines, now)
            add_rss_item(channel, source, menu, now)
            successful += 1

            print(f"Soups found: {len(menu['soups'])}")
            print(f"Daily dishes found: {len(menu['dishes'])}")
            print(f"Weekly soups found: {len(menu['weekly_soups'])}")
            print(f"Weekly dishes found: {len(menu['weekly_dishes'])}")
            print(f"Pizzas found: {len(menu['pizzas'])}")
        except Exception as error:
            failed += 1
            print(f"Error for {source['name']}: {error}")
            add_error_item(channel, source, error, now)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(rss)
    try:
        ET.indent(tree, space="  ")
    except AttributeError:
        pass

    tree.write(OUTPUT_FILE, encoding="utf-8", xml_declaration=True)
    print(f"\nRSS created: {OUTPUT_FILE}")
    print(f"Successfully loaded: {successful}")
    print(f"Failed: {failed}")


if __name__ == "__main__":
    create_rss()
