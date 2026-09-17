import html
import re
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


URL = "https://www.slatinabistro.cz/obedy"
TIME_ZONE = ZoneInfo("Europe/Prague")


def get_page_lines():
    response = requests.get(
        URL,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 MenuRSS/1.0",
            "Accept": "text/html",
        },
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding

    soup = BeautifulSoup(response.text, "html.parser")

    # Odstranění prvků, které nejsou součástí menu
    for element in soup(["script", "style", "noscript", "svg"]):
        element.decompose()

    lines = []

    for line in soup.get_text("\n", strip=True).splitlines():
        line = re.sub(r"\s+", " ", line).strip()

        if line and (not lines or lines[-1] != line):
            lines.append(line)

    return lines


def extract_today_menu(lines, today):
    today_text = f"{today.day}.{today.month}.{today.year}"

    start_index = None

    for index, line in enumerate(lines):
        normalized_line = line.replace(" ", "")

        if today_text in normalized_line:
            start_index = index
            break

    if start_index is None:
        raise ValueError(
            f"Menu pro dnešní datum {today_text} nebylo nalezeno."
        )

    # Hledání data následujícího dne
    date_pattern = re.compile(
        r"^(0?[1-9]|[12][0-9]|3[01])\."
        r"(0?[1-9]|1[0-2])\."
        r"(20[0-9]{2})$"
    )

    end_index = len(lines)

    for index in range(start_index + 1, len(lines)):
        normalized_line = lines[index].replace(" ", "")

        if date_pattern.fullmatch(normalized_line):
            # Zahrnutí názvu následujícího dne není žádoucí
            if index > 0:
                end_index = index - 1
            else:
                end_index = index

            break

    menu_lines = lines[start_index:end_index]

    # Odstranění názvu dne na konci před dalším datem
    day_names = {
        "Pondělí",
        "Úterý",
        "Středa",
        "Čtvrtek",
        "Pátek",
        "Sobota",
        "Neděle",
    }

    while menu_lines and menu_lines[-1] in day_names:
        menu_lines.pop()

    if not menu_lines:
        raise ValueError("Dnešní menu bylo nalezeno, ale je prázdné.")

    return menu_lines


def format_menu_for_teams(menu_lines):
    formatted = []
    index = 0

    while index < len(menu_lines):
        line = menu_lines[index]

        # Nadpis data
        if re.fullmatch(
            r"(0?[1-9]|[12][0-9]|3[01])\."
            r"(0?[1-9]|1[0-2])\."
            r"20[0-9]{2}",
            line.replace(" ", ""),
        ):
            formatted.append(
                f"<h3>🍽️ Denní menu {html.escape(line)}</h3>"
            )

        # Polévky
        elif line.casefold() == "polévka:":
            formatted.append("<h4>🍲 Polévky</h4>")

        # Číslo samostatně na řádku, například 1, 2, 3
        elif line.isdigit():
            dish_number = line

            if index + 1 < len(menu_lines):
                dish_name = menu_lines[index + 1]
                price = ""

                if index + 2 < len(menu_lines):
                    possible_price = menu_lines[index + 2]

                    if re.search(r"\d+\s*,?-?$", possible_price):
                        price = possible_price
                        index += 1

                formatted.append(
                    "<p>"
                    f"<strong>{html.escape(dish_number)}. "
                    f"{html.escape(dish_name)}</strong>"
                    f"{'<br>💰 ' + html.escape(price) if price else ''}"
                    "</p>"
                )

                index += 1

        # Cena se zobrazí pod předchozím řádkem
        elif re.search(r"^\d+\s*,?-$", line):
            formatted.append(f"<strong>💰 {html.escape(line)}</strong><br>")

        # Provozní informace nechceme v Teams zprávě
        elif line in {
            "Každý všední den",
            "11:00 - 15:00",
            ".",
        }:
            pass

        else:
            formatted.append(f"{html.escape(line)}<br>")

        index += 1

    return "\n".join(formatted)


def create_rss():
    now = datetime.now(TIME_ZONE)
    today_text = f"{now.day}.{now.month}.{now.year}"

    lines = get_page_lines()
    menu_lines = extract_today_menu(lines, now)
    formatted_menu = format_menu_for_teams(menu_lines)

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Slatina Bistro</title>
    <link>{URL}</link>
    <description>Denní menu Slatina Bistro</description>
    <language>cs-CZ</language>
    <lastBuildDate>{format_datetime(now)}</lastBuildDate>

    <item>
      <title>Slatina Bistro – menu {today_text}</title>
      <link>{URL}</link>
      <guid isPermaLink="false">slatina-bistro-{now.strftime("%Y-%m-%d")}</guid>
      <pubDate>{format_datetime(now)}</pubDate>
      <description><![CDATA[
{formatted_menu}
      ]]></description>
    </item>
  </channel>
</rss>
"""

    Path("docs").mkdir(exist_ok=True)

    Path("docs/rss.xml").write_text(
        rss,
        encoding="utf-8",
    )

    print(f"RSS vytvořeno pro datum {today_text}")
    print(f"Počet řádků menu: {len(menu_lines)}")


if __name__ == "__main__":
    create_rss()
