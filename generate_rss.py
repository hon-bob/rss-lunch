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
BBQ_MENU_FILE = Path("menu_bbq.json")
OUTPUT_FILE = Path("docs/rss.xml")
TIME_ZONE = ZoneInfo("Europe/Prague")
FEED_URL = "https://hon-bob.github.io/rss-lunch/rss.xml"
USER_AGENT = "Mozilla/5.0 (compatible; LunchMenuRSS/1.0)"

DAY_NAMES = ["Pondělí", "Úterý", "Středa", "Čtvrtek", "Pátek", "Sobota", "Neděle"]
SPACED_DAY_NAMES = [
    "P O N D Ě L Í", "Ú T E R Ý", "S T Ř E D A", "Č T V R T E K",
    "P Á T E K", "S O B O T A", "N E D Ě L E",
]


def load_sources():
    """Load and validate sources.json."""
    sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    if not isinstance(sources, list):
        raise ValueError("sources.json must contain a JSON array.")
    for source in sources:
        for field in ("id", "name", "url"):
            if not source.get(field):
                raise ValueError(f"Missing source field: {field}")
    return sources


def download_page(url):
    """Download one HTML page."""
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
    """Convert HTML to deduplicated visible text lines."""
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
    """Normalize text for comparisons."""
    value = str(value).casefold().replace("\u00a0", " ").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", "", value)


def date_text(now):
    return f"{now.day}.{now.month}.{now.year}"


def contains_today(value, now):
    normalized = normalize_text(value)
    variants = {
        f"{now.day}.{now.month}.{now.year}",
        f"{now.day:02d}.{now.month:02d}.{now.year}",
        f"{now.day}. {now.month}. {now.year}",
        f"{now.day:02d}. {now.month:02d}. {now.year}",
    }
    return any(normalize_text(item) in normalized for item in variants)


def contains_full_date(value):
    return bool(re.search(r"\d{1,2}\.\d{1,2}\.20\d{2}", normalize_text(value)))


def is_date_range(value):
    return bool(re.fullmatch(
        r"\d{1,2}\.\s*\d{1,2}\.\s*[–—-]\s*\d{1,2}\.\s*\d{1,2}\.\s*\d{4}",
        str(value).strip(),
    ))


def is_weekday(value):
    variants = {normalize_text(item) for item in DAY_NAMES + SPACED_DAY_NAMES}
    return normalize_text(value) in variants


def find_exact(lines, value, start=0):
    needle = normalize_text(value)
    return next((i for i in range(start, len(lines)) if normalize_text(lines[i]) == needle), None)


def looks_like_price(value):
    return bool(re.fullmatch(r"\d+\s*(Kč|,-|-)", str(value).strip(), re.IGNORECASE))


def normalize_price(value):
    match = re.search(r"\d+", str(value))
    return f"{match.group()} Kč" if match else str(value).strip()


def clean_name(value):
    value = re.sub(r"\s+", " ", str(value)).strip()
    value = re.sub(
        r",?\s*Orientační energetická hodnota porce:\s*\d+\s*Kcal",
        "", value, flags=re.IGNORECASE,
    )
    return value.strip(" ,-:")


def is_metadata(value):
    value = str(value).strip()
    patterns = [
        r"^\d+[a-z]?(,\d+[a-z]?)*$",
        r"^\d+([,.]\d+)?\s*[gl]$",
        r"^Recommended$",
    ]
    return any(re.fullmatch(pattern, value, re.IGNORECASE) for pattern in patterns)


def remove_noise(lines):
    ignored = {normalize_text(x) for x in [
        "Každý všední den", "11:00 - 15:00", "10:00 - 14:00",
        "10:30 - 14:30", "Recommended", ".",
    ]}
    return [line for line in lines if normalize_text(line) not in ignored]


def split_blocks_by_price(lines):
    blocks, current = [], []
    for line in lines:
        current.append(line)
        if looks_like_price(line):
            blocks.append(current)
            current = []
    return blocks


def block_to_item(block):
    if not block or not looks_like_price(block[-1]):
        return None
    parts = []
    for line in block[:-1]:
        if is_metadata(line) or is_date_range(line):
            continue
        cleaned = clean_name(line)
        if cleaned:
            parts.append(cleaned)
    name = clean_name(" ".join(parts))
    return {"name": name, "price": normalize_price(block[-1])} if name else None


def result(now, soups=None, dishes=None, weekly_soups=None, weekly_dishes=None,
           pizzas=None, static_title=None, static_text=None):
    """Build one common result structure."""
    return {
        "day": DAY_NAMES[now.weekday()],
        "date": date_text(now),
        "soups": soups or [],
        "dishes": dishes or [],
        "weekly_soups": weekly_soups or [],
        "weekly_dishes": weekly_dishes or [],
        "pizzas": pizzas or [],
        "static_title": static_title,
        "static_text": static_text,
    }


def find_today_section(lines, now, stop_texts=None):
    """Extract today's section from a weekly text page."""
    candidates = [i for i, line in enumerate(lines) if contains_today(line, now) and not is_date_range(line)]
    if not candidates:
        raise ValueError("Today's date was not found.")
    start = candidates[0]
    stops = {normalize_text(x) for x in (stop_texts or [])}
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if normalize_text(lines[index]) in stops:
            end = index
            break
        if contains_full_date(lines[index]) and not contains_today(lines[index], now):
            end = index - 1 if is_weekday(lines[index - 1]) else index
            break
    return remove_noise(lines[start:end])


def parse_numbered_menu(lines):
    """Parse a menu with separate number, name, and price lines."""
    soup_headings = {normalize_text(x) for x in ["Polévka", "Polévka:", "Polévky"]}
    soup_start = next((i for i, line in enumerate(lines) if normalize_text(line) in soup_headings), None)
    first_number = next((i for i, line in enumerate(lines) if line.strip().isdigit()), None)
    soups, dishes = [], []

    if soup_start is not None:
        soup_end = first_number if first_number is not None else len(lines)
        for block in split_blocks_by_price(lines[soup_start + 1:soup_end]):
            item = block_to_item(block)
            if item:
                soups.append(item)

    if first_number is not None:
        index = first_number
        while index < len(lines):
            if not lines[index].strip().isdigit():
                index += 1
                continue
            number = int(lines[index])
            index += 1
            block = []
            while index < len(lines) and not lines[index].strip().isdigit():
                block.append(lines[index])
                index += 1
                if looks_like_price(block[-1]):
                    break
            item = block_to_item(block)
            if item:
                item["number"] = number
                dishes.append(item)
    return soups, dishes


def parse_slatina(lines, now):
    section = find_today_section(lines, now, ["Snídaně", "Nabídka obědů na celý týden"])
    soups, dishes = parse_numbered_menu(section)
    return result(now, soups, dishes)


def extract_turanka_daily(lines, now):
    """Find the real daily section and ignore the weekly date range."""
    candidates = [i for i, line in enumerate(lines) if contains_today(line, now) and not is_date_range(line)]
    headings = {normalize_text(x) for x in ["Polévka", "Polévky", "Hlavní chod", "Hlavní chody"]}
    start = None
    for candidate in reversed(candidates):
        nearby = lines[candidate + 1:min(candidate + 15, len(lines))]
        if any(normalize_text(line) in headings for line in nearby):
            start = candidate
            break
    if start is None:
        raise ValueError("The current Tackarna daily section was not found.")

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if normalize_text(lines[index]) == normalize_text("Týdenní menu"):
            end = index
            break
        if contains_full_date(lines[index]) and not contains_today(lines[index], now):
            end = index - 1 if is_weekday(lines[index - 1]) else index
            break
    return remove_noise(lines[start:end])


def parse_turanka_daily(lines, now):
    section = extract_turanka_daily(lines, now)
    soup_start = find_exact(section, "Polévka")
    if soup_start is None:
        soup_start = find_exact(section, "Polévky")
    main_start = find_exact(section, "Hlavní chod")
    if main_start is None:
        main_start = find_exact(section, "Hlavní chody")

    soups, dishes = [], []
    if soup_start is not None:
        end = main_start if main_start is not None else len(section)
        for block in split_blocks_by_price(section[soup_start + 1:end]):
            item = block_to_item(block)
            if item:
                soups.append(item)
    if main_start is not None:
        for block in split_blocks_by_price(section[main_start + 1:]):
            item = block_to_item(block)
            if item:
                item["number"] = len(dishes) + 1
                dishes.append(item)
    return soups, dishes


def parse_category_sections(lines):
    """Split weekly Tackarna content into soups, dishes, and pizzas."""
    soup_heads = {normalize_text(x) for x in ["Polévka", "Polévky"]}
    pizza_heads = {normalize_text(x) for x in ["Pizza", "Pizzy"]}
    dish_heads = {normalize_text(x) for x in ["Hlavní chod", "Hlavní chody", "Poke", "Saláty"]}
    category = "dishes"
    data = {"soups": [], "dishes": [], "pizzas": []}
    block = []

    def save():
        nonlocal block
        item = block_to_item(block)
        block = []
        if item:
            key = category
            if key != "soups":
                item["number"] = len(data[key]) + 1
            data[key].append(item)

    for line in lines:
        normalized = normalize_text(line)
        if normalized in soup_heads:
            save(); category = "soups"; continue
        if normalized in pizza_heads:
            save(); category = "pizzas"; continue
        if normalized in dish_heads:
            save(); category = "dishes"; continue
        if is_date_range(line) or normalized in {normalize_text("Týdenní menu"), normalize_text("Denní menu")}:
            save(); continue
        block.append(line)
        if looks_like_price(line):
            save()
    save()
    return data["soups"], data["dishes"], data["pizzas"]


def parse_turanka_weekly(lines):
    start = find_exact(lines, "Týdenní menu")
    if start is None:
        return [], [], []

    stop_phrases = [
        "Seznam alergenů", "Váhy masa", "Obědová nabídka platí",
        "Věrnostní program", "Kontaktní informace", "Kde nás najdete",
        "Otevírací doba", "Vaše jméno",
    ]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        normalized = normalize_text(lines[index])
        if any(normalize_text(stop) in normalized for stop in stop_phrases):
            end = index
            break
    weekly = remove_noise(lines[start + 1:end])
    return parse_category_sections(weekly)


def parse_turanka(lines, now):
    soups, dishes = parse_turanka_daily(lines, now)
    weekly_soups, weekly_dishes, pizzas = parse_turanka_weekly(lines)
    return result(now, soups, dishes, weekly_soups, weekly_dishes, pizzas)


def extract_jomsom_section(lines, now):
    target = normalize_text(SPACED_DAY_NAMES[now.weekday()])
    markers = {normalize_text(day) for day in SPACED_DAY_NAMES}
    start = next((i for i, line in enumerate(lines) if normalize_text(line) == target), None)
    if start is None:
        raise ValueError("Today's Jomsom heading was not found.")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        normalized = normalize_text(lines[index])
        if normalized in markers or "informaceopřítomnostialergenů" in normalized:
            end = index
            break
    return lines[start + 1:end]


def parse_jomsom(lines, now):
    text = re.sub(r"\s+", " ", " ".join(extract_jomsom_section(lines, now))).strip()
    pattern = re.compile(r"(.*?)(?::\s*-\s*|:-)\s*(\d+)\s*Kč", re.IGNORECASE)
    items, remaining = [], text
    while remaining:
        match = pattern.search(remaining)
        if not match:
            break
        items.append({"name": clean_name(match.group(1)), "price": f"{match.group(2)} Kč"})
        remaining = remaining[match.end():].strip()

    soups, dishes = [], []
    for item in items:
        if "soup" in normalize_text(item["name"]) or "polévka" in normalize_text(item["name"]):
            soups.append(item)
            continue
        match = re.match(r"^(\d+)\.\s*(.*)$", item["name"])
        dishes.append({
            "number": int(match.group(1)) if match else len(dishes) + 1,
            "name": clean_name(match.group(2)) if match else item["name"],
            "price": item["price"],
        })
    return result(now, soups, dishes)


def parse_moc_bbq(now):
    """Load the fixed MOC BBQ menu for the current weekday from JSON."""

    if not BBQ_MENU_FILE.exists():
        raise FileNotFoundError(
            f"BBQ menu file {BBQ_MENU_FILE} was not found."
        )

    data = json.loads(
        BBQ_MENU_FILE.read_text(encoding="utf-8")
    )

    weekdays = data.get("weekdays")
    if not isinstance(weekdays, dict):
        raise ValueError(
            "menu_bbq.json must contain a weekdays object."
        )

    weekday_key = str(now.weekday())
    weekday_menu = weekdays.get(weekday_key)

    if weekday_menu is None:
        return result(
            now,
            static_title="Polední menu",
            static_text=(
                "Polední menu je dostupné pouze v pracovních dnech "
                "od 10:30 do 14:00."
            ),
        )

    soups = weekday_menu.get("soups", [])
    dishes = weekday_menu.get("dishes", [])

    if not isinstance(soups, list) or not isinstance(dishes, list):
        raise ValueError(
            f"Invalid menu structure for weekday {weekday_key}."
        )

    normalized_soups = []
    for soup in soups:
        if not isinstance(soup, dict) or not soup.get("name"):
            continue
        normalized_soups.append(
            {
                "name": str(soup["name"]).strip(),
                "price": str(soup.get("price", "")).strip(),
            }
        )

    normalized_dishes = []
    for position, dish in enumerate(dishes, start=1):
        if not isinstance(dish, dict) or not dish.get("name"):
            continue
        normalized_dishes.append(
            {
                "number": int(dish.get("number", position)),
                "name": str(dish["name"]).strip(),
                "price": str(dish.get("price", "")).strip(),
            }
        )

    if not normalized_soups and not normalized_dishes:
        raise ValueError(
            f"No MOC BBQ menu items found for weekday {weekday_key}."
        )

    return result(
        now,
        soups=normalized_soups,
        dishes=normalized_dishes,
    )


def parse_generic(lines, now):
    section = find_today_section(lines, now)
    soups, dishes = parse_numbered_menu(section)
    return result(now, soups, dishes)


def parse_source(source, lines, now):
    """Select a parser by source ID."""
    if source["id"] == "moc-bbq":
        return parse_moc_bbq(now)
    parsers = {"slatina": parse_slatina, "turanka": parse_turanka, "jomsom": parse_jomsom}
    menu = parsers.get(source["id"], parse_generic)(lines, now)
    if not any(menu.get(key) for key in ["soups", "dishes", "weekly_soups", "weekly_dishes", "pizzas", "static_text"]):
        raise ValueError("No menu items were extracted.")
    return menu


def format_item(item):
    name = html.escape(item["name"])
    price = html.escape(item.get("price", ""))
    return f"{name}, {price}" if price else name


def append_numbered(output, items):
    for position, item in enumerate(sorted(items, key=lambda x: x.get("number", 999)), start=1):
        number = item.get("number", position)
        output.append(f"<p><strong>{number}.</strong> {format_item(item)}</p>")


def format_menu(source, menu):
    """Render a menu as HTML stored inside the RSS description."""
    output = [
        f"<h2>🍽️ {html.escape(source['name'])}</h2>",
        f"<p>{html.escape(menu['day'])} {html.escape(menu['date'])}</p>",
    ]

    if menu.get("static_text"):
        output.append(f"<h3>🍽️ {html.escape(menu.get('static_title') or 'Polední menu')}</h3>")
        output.append(f"<p>{html.escape(menu['static_text'])}</p>")
        return "\n".join(output)

    if menu["soups"]:
        output.append("<h3>🍲 Polévky</h3>")
        output.extend(f"<p>{format_item(item)}</p>" for item in menu["soups"])
    if menu["dishes"]:
        output.append("<h3>🍽️ Denní menu</h3>")
        append_numbered(output, menu["dishes"])
    if menu["weekly_soups"] or menu["weekly_dishes"]:
        output.append("<h3>📅 Týdenní menu</h3>")
    if menu["weekly_soups"]:
        output.append("<h4>🍲 Polévky</h4>")
        output.extend(f"<p>{format_item(item)}</p>" for item in menu["weekly_soups"])
    if menu["weekly_dishes"]:
        output.append("<h4>🍽️ Nabídka na celý týden</h4>")
        append_numbered(output, menu["weekly_dishes"])
    if menu["pizzas"]:
        output.append("<h3>🍕 Pizza</h3>")
        append_numbered(output, menu["pizzas"])
    return "\n".join(output)


def add_item(channel, source, menu, now):
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = f"{source['name']} | {menu['day']} {menu['date']}"
    ET.SubElement(item, "link").text = source["url"]
    guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
    guid.text = hashlib.sha256(f"{source['id']}:{now:%Y-%m-%d}".encode()).hexdigest()
    ET.SubElement(item, "pubDate").text = format_datetime(now)
    ET.SubElement(item, "description").text = format_menu(source, menu)


def add_error(channel, source, error, now):
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = f"{source['name']} | menu se nepodařilo načíst"
    ET.SubElement(item, "link").text = source["url"]
    ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = f"{source['id']}-error-{now:%Y-%m-%d}"
    ET.SubElement(item, "pubDate").text = format_datetime(now)
    ET.SubElement(item, "description").text = f"Menu se nepodařilo načíst: {html.escape(str(error))}"


def create_rss():
    """Generate the complete RSS file."""
    now = datetime.now(TIME_ZONE)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Polední menu restaurací"
    ET.SubElement(channel, "link").text = FEED_URL
    ET.SubElement(channel, "description").text = "Denní menu vybraných restaurací"
    ET.SubElement(channel, "language").text = "cs-CZ"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(now)
    ET.SubElement(channel, "ttl").text = "60"

    successful = failed = 0
    for source in load_sources():
        print(f"\nLoading: {source['name']}")
        try:
            if source.get("static") or source["id"] == "moc-bbq":
                lines = []
            else:
                lines = html_to_lines(download_page(source["url"]))
            menu = parse_source(source, lines, now)
            add_item(channel, source, menu, now)
            successful += 1
            if menu.get("static_text"):
                print("Static menu notice created.")
            else:
                print(f"Soups: {len(menu['soups'])}; daily: {len(menu['dishes'])}; weekly: {len(menu['weekly_dishes'])}; pizzas: {len(menu['pizzas'])}")
        except Exception as error:
            failed += 1
            print(f"Error: {error}")
            add_error(channel, source, error, now)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    tree.write(OUTPUT_FILE, encoding="utf-8", xml_declaration=True)
    print(f"\nRSS created: {OUTPUT_FILE}; successful: {successful}; failed: {failed}")


if __name__ == "__main__":
    create_rss()
