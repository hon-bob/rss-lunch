import requests
from bs4 import BeautifulSoup
from datetime import datetime
from pathlib import Path

URL = "https://www.slatinabistro.cz/obedy"

html = requests.get(URL, timeout=30).text

soup = BeautifulSoup(html, "html.parser")
text = soup.get_text("\n", strip=True)

today = datetime.now().strftime("%-d.%-m.%Y")

rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Slatina Bistro</title>
<link>{URL}</link>
<description>Denní menu</description>

<item>
<title>Menu {today}</title>
<link>{URL}</link>
<description><![CDATA[
{text[:5000]}
]]></description>
</item>

</channel>
</rss>
"""

Path("docs").mkdir(exist_ok=True)
Path("docs/rss.xml").write_text(rss, encoding="utf-8")
`