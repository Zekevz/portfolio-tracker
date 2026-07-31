#!/usr/bin/env python3
"""
Daily portfolio brief generator.

Pulls live EOD prices from yfinance, calculates real P/L from stored
entry positions, optionally syncs live holdings from EasyEquities
(no public API - authenticates via its own web login flow), fetches
news headlines from RSS and yfinance, and writes a Markdown + HTML
portfolio snapshot.

Run manually or on a daily schedule (cron / Windows Task Scheduler).

Dependencies:
    pip install -r requirements.txt
"""

import json
import re
import sys
import webbrowser
from datetime import datetime, date
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    sys.exit("Missing dependency: pip install yfinance")

try:
    import feedparser
except ImportError:
    sys.exit("Missing dependency: pip install feedparser")


# ── Output ───────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path(__file__).parent / "output"
TODAY = date.today()
NOTE_PATH = OUTPUT_DIR / f"{TODAY}.md"
HTML_PATH = OUTPUT_DIR / f"{TODAY}.html"

# ── Portfolio positions ──────────────────────────────────────────────────────
# Example holdings only. Real position data lives in a private, gitignored
# copy - see README for how EasyEquities sync overrides these at runtime.
# Formula: shares = current_value / current_price
# Entry price = purchase_value / shares (implicit - not stored separately)

USD_HOLDINGS = {
    "AAPL": {"name": "Apple",                "purchase_usd": 100.00, "shares": 0.55},
    "VOO":  {"name": "Vanguard S&P 500 ETF", "purchase_usd": 150.00, "shares": 0.32},
}

ZAR_DIY = {
    "SHP.JO": {"name": "Shoprite", "purchase_zar": 500.00, "shares": 2.10},
}

ZAR_TFSA = {
    "STXDIV.JO": {"name": "Satrix DIVI Plus", "purchase_zar": 300.00, "shares": 95.0},
}

# Bond / fixed-income position with no live price feed - update manually
# after each coupon payment.
BOND = {
    "name":         "Example Government Bond 10.5%",
    "purchase_zar": 3000.00,
    "current_zar":  3050.00,   # update after each coupon payment
    "price":        1.0100,
    "units":        3000.00,
    "next_coupon":  "semi-annual",
    "coupon_est":   "~R150",
    "maturity":     "example date",
}

# ── EasyEquities live sync ───────────────────────────────────────────────────
# Credentials live in ee_credentials.json next to this script (gitignored -
# see .gitignore and ee_credentials.example.json). If the file is missing,
# empty, or login fails, the hardcoded example positions above are used and
# only yfinance prices apply.
EE_CREDENTIALS = Path(__file__).parent / "ee_credentials.json"


def parse_money(s: str) -> float:
    """'R3 236.50' / '$19.62' / 'ZAR2,457.73' -> float. Empty/None -> 0.0."""
    cleaned = re.sub(r"[^\d.\-]", "", (s or "").replace(",", ""))
    return float(cleaned) if cleaned else 0.0


BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def _ee_login(client, username: str, password: str) -> None:
    """
    Two-step EasyID login (the library's own login() predates this flow and
    also gets blocked without a browser User-Agent). Raises with EE's own
    validation message on bad credentials.
    """
    from bs4 import BeautifulSoup

    base = "https://platform.easyequities.io"
    client.session.headers["User-Agent"] = BROWSER_UA

    def form_fields(content):
        """Hidden inputs only - posting the visible fields empty makes EE
        treat it as a failed full login attempt."""
        soup = BeautifulSoup(content, "html.parser")
        form = soup.find("form")
        if form is None:
            return None, soup
        fields = {
            inp["name"]: inp.get("value", "")
            for inp in form.find_all("input", {"type": "hidden"})
            if inp.get("name")
        }
        return fields, soup

    # Step 1: EasyID page, submit username only
    r = client.session.get(base + "/Account/SignIn")
    r.raise_for_status()
    fields, _ = form_fields(r.content)
    if fields is None:
        raise Exception("Sign-in page had no form (site layout changed?)")
    fields["Username"] = username
    r2 = client.session.post(base + "/Account/SignIn", data=fields)
    r2.raise_for_status()

    # Step 2: password page, submit username + password
    fields2, _ = form_fields(r2.content)
    if fields2 is None:
        raise Exception("Password page had no form (site layout changed?)")
    fields2["UserIdentifier"] = username
    fields2["Password"] = password
    r3 = client.session.post(
        base + "/Account/SignIn", data=fields2, allow_redirects=False
    )
    r3.raise_for_status()
    if r3.status_code != 302:
        _, soup = form_fields(r3.content)
        err = soup.select_one(
            ".validation-summary-errors, .field-validation-error, .text-danger"
        )
        msg = err.get_text(" ", strip=True) if err else "Login failed"
        raise Exception(msg)

    # Follow the OIDC redirect chain to finish authentication
    location = r3.headers.get("Location", "")
    if location.startswith("/"):
        location = base + location
    if location:
        client.session.get(location)


def sync_from_easyequities() -> bool:
    """
    Replace the hardcoded positions with live EasyEquities holdings.
    Stores EE's live price as 'ee_price' on each position so it can
    override the yfinance previous-close price.
    Returns True on success, False to fall back to hardcoded values.
    """
    if not EE_CREDENTIALS.exists():
        print("  No ee_credentials.json - using example positions")
        return False
    try:
        creds = json.loads(EE_CREDENTIALS.read_text(encoding="utf-8"))
        if not creds.get("username") or not creds.get("password"):
            print("  ee_credentials.json not filled in - using example positions")
            return False

        from easy_equities_client.clients import EasyEquitiesClient

        client = EasyEquitiesClient()
        _ee_login(client, creds["username"], creds["password"])

        usd_acc = zar_acc = tfsa_acc = None
        for acc in client.accounts.list():
            n = acc.name.upper()
            if "TFSA" in n:
                tfsa_acc = acc
            elif "USD" in n:
                usd_acc = acc
            elif "ZAR" in n:
                zar_acc = acc

        def to_position(h, purchase_key):
            price = parse_money(h["current_price"])
            value = parse_money(h["current_value"])
            shares = parse_money(h.get("shares", ""))
            if not shares and price:
                shares = value / price
            return {
                "name": h["name"],
                purchase_key: parse_money(h["purchase_value"]),
                "shares": shares,
                "ee_price": price,
            }

        if usd_acc:
            new_usd = {}
            for h in client.accounts.holdings(usd_acc.id, include_shares=True):
                ticker = h["contract_code"].split(".")[-1]
                new_usd[ticker] = to_position(h, "purchase_usd")
            if new_usd:
                USD_HOLDINGS.clear()
                USD_HOLDINGS.update(new_usd)

        if zar_acc:
            new_diy = {}
            for h in client.accounts.holdings(zar_acc.id, include_shares=True):
                name_u = h["name"].upper()
                if "BOND" in name_u:
                    BOND["purchase_zar"] = parse_money(h["purchase_value"])
                    BOND["current_zar"] = parse_money(h["current_value"])
                    price = parse_money(h["current_price"])
                    if price:
                        BOND["price"] = price
                        BOND["units"] = BOND["current_zar"] / price
                else:
                    ticker = h["contract_code"].split(".")[-1] + ".JO"
                    new_diy[ticker] = to_position(h, "purchase_zar")
            if new_diy:
                ZAR_DIY.clear()
                ZAR_DIY.update(new_diy)

        if tfsa_acc:
            new_tfsa = {}
            for h in client.accounts.holdings(tfsa_acc.id, include_shares=True):
                ticker = h["contract_code"].split(".")[-1] + ".JO"
                new_tfsa[ticker] = to_position(h, "purchase_zar")
            if new_tfsa:
                ZAR_TFSA.clear()
                ZAR_TFSA.update(new_tfsa)

        print("  Synced live positions from EasyEquities")
        return True
    except Exception as e:
        print(f"  EE sync failed ({e}) - using example positions")
        return False


def apply_ee_prices(holdings: dict, prices: dict) -> None:
    """Override yfinance previous-close prices with EE's live prices
    so values match the EasyEquities app. Day-change % stays yfinance."""
    for ticker, h in holdings.items():
        ee_price = h.get("ee_price")
        if ee_price:
            prices.setdefault(ticker, {"price": None, "day_change_pct": None})
            prices[ticker]["price"] = ee_price


KEY_DATES = [
    ("example date", f"{BOND['name']} matures - final coupon ({BOND['coupon_est']}) + principal, redeploy capital"),
]

# ── News ─────────────────────────────────────────────────────────────────────
RSS_FEEDS = {
    "Moneyweb":      "https://www.moneyweb.co.za/feed/",
    "Yahoo Finance": "https://finance.yahoo.com/rss/topstories",
    "CNBC":          "https://www.cnbc.com/id/10000664/device/rss/rss.html",
}
RSS_ITEMS = 4       # headlines per feed
STOCK_NEWS = 2      # yfinance headlines per ticker
STOCK_NEWS_TICKERS = ["AAPL", "VOO"]  # top holdings by watch priority


# ── Helpers ──────────────────────────────────────────────────────────────────
def fmt_pct(pct: float | None, parens: bool = False) -> str:
    if pct is None:
        return "n/a"
    sign = "+" if pct >= 0 else ""
    s = f"{sign}{pct:.2f}%"
    return f"({s})" if parens else s


def fmt_money(val: float, symbol: str = "$") -> str:
    sign = "+" if val >= 0 else "-"
    return f"{sign}{symbol}{abs(val):.2f}"


def today_label() -> str:
    return TODAY.strftime("%A, %d %B %Y")


def fetch_price(ticker: str, jse: bool = False) -> dict:
    """Fetch price for a single ticker via history(). JSE prices are in cents - divide by 100."""
    try:
        h = yf.Ticker(ticker).history(period="5d")["Close"].dropna()
        if len(h) < 1:
            return {"price": None, "day_change_pct": None}
        current = float(h.iloc[-1])
        prev = float(h.iloc[-2]) if len(h) >= 2 else None
        if jse:
            current /= 100
            prev = prev / 100 if prev else None
        day_chg = ((current - prev) / prev * 100) if prev else None
        return {"price": current, "day_change_pct": day_chg}
    except Exception as e:
        print(f"  Price fetch failed for {ticker}: {e}")
        return {"price": None, "day_change_pct": None}


def fetch_prices(tickers: list, jse: bool = False) -> dict:
    return {t: fetch_price(t, jse=jse) for t in tickers}


def fetch_usdzar() -> float:
    try:
        h = yf.Ticker("USDZAR=X").history(period="5d")["Close"].dropna()
        return float(h.iloc[-1])
    except Exception:
        print("  USD/ZAR fetch failed - using fallback R18.50")
        return 18.50


def fetch_stock_headlines(ticker: str, n: int = 2) -> list:
    """Returns list of {"title": str, "link": str}."""
    try:
        news = yf.Ticker(ticker).news or []
        out = []
        for item in news[:n]:
            c = item.get("content", item)  # fall back to flat shape if API reverts
            title = c.get("title")
            if not title:
                continue
            link = (c.get("clickThroughUrl") or c.get("canonicalUrl") or {}).get("url", "")
            out.append({"title": title, "link": link})
        return out
    except Exception:
        return []


def fetch_rss(feeds: dict, n: int = 4) -> list:
    """Returns list of {"source": str, "title": str, "link": str}."""
    items = []
    for source, url in feeds.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:n]:
                items.append({"source": source, "title": entry.title, "link": entry.link})
        except Exception:
            items.append({"source": source, "title": "(feed unavailable)", "link": ""})
    return items


# ── Note builder ─────────────────────────────────────────────────────────────
def build_usd_section(prices: dict) -> tuple[list, float, float]:
    """Returns (lines, total_purchase, total_current)."""
    lines = [
        "## USD Portfolio (EasyEquities USD DIY)",
        "",
        "| Ticker | Name | Entry $ | Price | Day | Value | P/L $ | P/L % |",
        "|--------|------|---------|-------|-----|-------|--------|-------|",
    ]
    total_purchase = 0.0
    total_current = 0.0

    for ticker, h in USD_HOLDINGS.items():
        p = prices.get(ticker, {})
        price = p.get("price")
        day = p.get("day_change_pct")
        shares = h["shares"]
        purchase = h["purchase_usd"]
        entry = purchase / shares

        if price is not None:
            current_val = shares * price
            pl = current_val - purchase
            pl_pct = (pl / purchase) * 100
            total_purchase += purchase
            total_current += current_val
            lines.append(
                f"| {ticker} | {h['name']} | ${entry:.2f} | ${price:.2f} "
                f"| {fmt_pct(day)} | ${current_val:.2f} "
                f"| {fmt_money(pl)} | {fmt_pct(pl_pct)} |"
            )
        else:
            lines.append(
                f"| {ticker} | {h['name']} | ${entry:.2f} | unavailable | - | - | - | - |"
            )

    if total_purchase:
        total_pl = total_current - total_purchase
        total_pl_pct = (total_pl / total_purchase) * 100
        lines.append(
            f"| **TOTAL** | | | | | **${total_current:.2f}** "
            f"| **{fmt_money(total_pl)}** | **{fmt_pct(total_pl_pct)}** |"
        )

    return lines, total_purchase, total_current


def build_zar_section(diy_prices: dict, tfsa_prices: dict) -> tuple[list, float, float]:
    lines = [
        "## ZAR Portfolio",
        "",
        "### DIY Account",
        "",
        "| Ticker | Name | Entry R | Price | Day | Value | P/L R | P/L % |",
        "|--------|------|---------|-------|-----|-------|--------|-------|",
    ]
    total_purchase = 0.0
    total_current = 0.0

    def append_holding(holdings, prices, lines):
        nonlocal total_purchase, total_current
        for ticker, h in holdings.items():
            p = prices.get(ticker, {})
            price = p.get("price")
            day = p.get("day_change_pct")
            shares = h["shares"]
            purchase = h["purchase_zar"]
            entry = purchase / shares
            display = ticker.replace(".JO", "")

            if price is not None:
                current_val = shares * price
                pl = current_val - purchase
                pl_pct = (pl / purchase) * 100
                total_purchase += purchase
                total_current += current_val
                lines.append(
                    f"| {display} | {h['name']} | R{entry:.2f} | R{price:.2f} "
                    f"| {fmt_pct(day)} | R{current_val:.2f} "
                    f"| {fmt_money(pl, 'R')} | {fmt_pct(pl_pct)} |"
                )
            else:
                lines.append(
                    f"| {display} | {h['name']} | R{entry:.2f} | unavailable | - | - | - | - |"
                )

    append_holding(ZAR_DIY, diy_prices, lines)

    # Bond - static
    b = BOND
    bond_pl = b["current_zar"] - b["purchase_zar"]
    bond_pl_pct = (bond_pl / b["purchase_zar"]) * 100
    total_purchase += b["purchase_zar"]
    total_current += b["current_zar"]
    lines.append(
        f"| BOND | {b['name']} | R{b['purchase_zar']/b['units']:.4f} "
        f"| R{b['price']:.4f} | - | R{b['current_zar']:.2f} "
        f"| {fmt_money(bond_pl, 'R')} | {fmt_pct(bond_pl_pct)} |"
    )
    lines.append(
        f"| | *Coupon: {b['coupon_est']} ({b['next_coupon']}). Matures {b['maturity']}* | | | | | | |"
    )

    lines += [
        "",
        "### TFSA Account",
        "",
        "| Ticker | Name | Entry R | Price | Day | Value | P/L R | P/L % |",
        "|--------|------|---------|-------|-----|-------|--------|-------|",
    ]
    append_holding(ZAR_TFSA, tfsa_prices, lines)

    return lines, total_purchase, total_current


def build_summary(
    usd_purchase, usd_current, zar_purchase, zar_current, usdzar
) -> list:
    usd_pl = usd_current - usd_purchase
    zar_pl = zar_current - zar_purchase
    usd_in_zar_purchase = usd_purchase * usdzar
    usd_in_zar_current = usd_current * usdzar
    grand_purchase = usd_in_zar_purchase + zar_purchase
    grand_current = usd_in_zar_current + zar_current
    grand_pl = grand_current - grand_purchase
    grand_pl_pct = (grand_pl / grand_purchase * 100) if grand_purchase else 0

    return [
        "## Summary",
        "",
        f"| Account | Invested | Current | P/L | P/L % |",
        f"|---------|----------|---------|-----|-------|",
        f"| USD DIY | ${usd_purchase:.2f} | ${usd_current:.2f} "
        f"| {fmt_money(usd_pl)} | {fmt_pct((usd_pl/usd_purchase*100) if usd_purchase else None)} |",
        f"| ZAR (DIY + TFSA + Bond) | R{zar_purchase:.2f} | R{zar_current:.2f} "
        f"| {fmt_money(zar_pl, 'R')} | {fmt_pct((zar_pl/zar_purchase*100) if zar_purchase else None)} |",
        f"| **Grand total (ZAR equiv, R{usdzar:.2f}/USD)** | **R{grand_purchase:.2f}** "
        f"| **R{grand_current:.2f}** | **{fmt_money(grand_pl, 'R')}** | **{fmt_pct(grand_pl_pct)}** |",
    ]


def build_note(
    usd_prices, zar_diy_prices, zar_tfsa_prices, usdzar,
    rss_headlines, stock_news,
) -> str:
    usd_lines, usd_purchase, usd_current = build_usd_section(usd_prices)
    zar_lines, zar_purchase, zar_current = build_zar_section(zar_diy_prices, zar_tfsa_prices)
    summary_lines = build_summary(usd_purchase, usd_current, zar_purchase, zar_current, usdzar)

    key_date_rows = "\n".join(f"| {d} | {e} |" for d, e in KEY_DATES)

    def rss_to_md(headlines):
        return [
            f"- **{item['source']}:** [{item['title']}]({item['link']})" if item["link"]
            else f"- **{item['source']}:** {item['title']}"
            for item in headlines
        ]

    rss_md = rss_to_md(rss_headlines)

    stock_news_block = []
    for ticker, headlines in stock_news.items():
        if headlines:
            stock_news_block.append(f"**{ticker}**")
            for h in headlines:
                stock_news_block.append(f"- [{h['title']}]({h['link']})")
            stock_news_block.append("")

    sections = [
        f"# Daily Brief - {today_label()}",
        "",
        f"> Auto-generated by `portfolio_brief.py` at {datetime.now().strftime('%H:%M')}. "
        f"Prices from yfinance (previous close). USD/ZAR: R{usdzar:.2f}.",
        "",
        "---",
        "",
        *usd_lines,
        "",
        "---",
        "",
        *zar_lines,
        "",
        "---",
        "",
        *summary_lines,
        "",
        "---",
        "",
        "## Key Dates",
        "",
        "| Date | Event |",
        "|------|-------|",
        key_date_rows,
        "",
        "---",
        "",
        "## News",
        "",
        "### Market",
        "",
        *rss_md,
        "",
        "### Stock Headlines",
        "",
        *stock_news_block,
        "---",
        "",
        "*Sources: yfinance (prices), Moneyweb/Yahoo Finance/CNBC RSS. "
        "Bond value updated manually. Prices are previous close.*",
    ]

    return "\n".join(sections)


# ── HTML builder ─────────────────────────────────────────────────────────────
def pl_class(val: float) -> str:
    return "pos" if val >= 0 else "neg"


def build_html(
    usd_prices, zar_diy_prices, zar_tfsa_prices, usdzar,
    rss_headlines, stock_news,
    usd_purchase, usd_current, zar_purchase, zar_current,
) -> str:
    ts = datetime.now().strftime("%H:%M")
    day_label = TODAY.strftime("%A, %d %B %Y")

    usd_rows = ""
    for ticker, h in USD_HOLDINGS.items():
        p = usd_prices.get(ticker, {})
        price = p.get("price")
        day = p.get("day_change_pct")
        shares = h["shares"]
        purchase = h["purchase_usd"]
        entry = purchase / shares
        if price is not None:
            current_val = shares * price
            pl = current_val - purchase
            pl_pct = (pl / purchase) * 100
            usd_rows += f"""
            <tr>
              <td class="ticker">{ticker}</td>
              <td>{h['name']}</td>
              <td>${entry:.2f}</td>
              <td>${price:.2f}</td>
              <td class="{pl_class(day or 0)}">{fmt_pct(day)}</td>
              <td>${current_val:.2f}</td>
              <td class="{pl_class(pl)}">{fmt_money(pl)}</td>
              <td class="{pl_class(pl_pct)}">{fmt_pct(pl_pct)}</td>
            </tr>"""
        else:
            usd_rows += f"""
            <tr>
              <td class="ticker">{ticker}</td>
              <td>{h['name']}</td>
              <td>${entry:.2f}</td>
              <td colspan="5" class="unavail">price unavailable</td>
            </tr>"""

    usd_pl = usd_current - usd_purchase
    usd_pl_pct = (usd_pl / usd_purchase * 100) if usd_purchase else 0
    usd_rows += f"""
            <tr class="total-row">
              <td colspan="5">TOTAL</td>
              <td>${usd_current:.2f}</td>
              <td class="{pl_class(usd_pl)}">{fmt_money(usd_pl)}</td>
              <td class="{pl_class(usd_pl_pct)}">{fmt_pct(usd_pl_pct)}</td>
            </tr>"""

    zar_diy_rows = ""
    for ticker, h in ZAR_DIY.items():
        p = zar_diy_prices.get(ticker, {})
        price = p.get("price")
        day = p.get("day_change_pct")
        shares = h["shares"]
        purchase = h["purchase_zar"]
        entry = purchase / shares
        display = ticker.replace(".JO", "")
        if price is not None:
            current_val = shares * price
            pl = current_val - purchase
            pl_pct = (pl / purchase) * 100
            zar_diy_rows += f"""
            <tr>
              <td class="ticker">{display}</td>
              <td>{h['name']}</td>
              <td>R{entry:.2f}</td>
              <td>R{price:.2f}</td>
              <td class="{pl_class(day or 0)}">{fmt_pct(day)}</td>
              <td>R{current_val:.2f}</td>
              <td class="{pl_class(pl)}">{fmt_money(pl, 'R')}</td>
              <td class="{pl_class(pl_pct)}">{fmt_pct(pl_pct)}</td>
            </tr>"""
        else:
            zar_diy_rows += f"""
            <tr>
              <td class="ticker">{display}</td>
              <td>{h['name']}</td>
              <td>R{entry:.2f}</td>
              <td colspan="5" class="unavail">price unavailable</td>
            </tr>"""

    b = BOND
    bond_pl = b["current_zar"] - b["purchase_zar"]
    bond_pl_pct = (bond_pl / b["purchase_zar"]) * 100
    zar_diy_rows += f"""
            <tr>
              <td class="ticker">BOND</td>
              <td>{b['name']}</td>
              <td>R{b['purchase_zar']/b['units']:.4f}</td>
              <td>R{b['price']:.4f}</td>
              <td>-</td>
              <td>R{b['current_zar']:.2f}</td>
              <td class="{pl_class(bond_pl)}">{fmt_money(bond_pl, 'R')}</td>
              <td class="{pl_class(bond_pl_pct)}">{fmt_pct(bond_pl_pct)}</td>
            </tr>"""

    tfsa_rows = ""
    for ticker, h in ZAR_TFSA.items():
        p = zar_tfsa_prices.get(ticker, {})
        price = p.get("price")
        day = p.get("day_change_pct")
        shares = h["shares"]
        purchase = h["purchase_zar"]
        entry = purchase / shares
        display = ticker.replace(".JO", "")
        if price is not None:
            current_val = shares * price
            pl = current_val - purchase
            pl_pct = (pl / purchase) * 100
            tfsa_rows += f"""
            <tr>
              <td class="ticker">{display}</td>
              <td>{h['name']}</td>
              <td>R{entry:.2f}</td>
              <td>R{price:.2f}</td>
              <td class="{pl_class(day or 0)}">{fmt_pct(day)}</td>
              <td>R{current_val:.2f}</td>
              <td class="{pl_class(pl)}">{fmt_money(pl, 'R')}</td>
              <td class="{pl_class(pl_pct)}">{fmt_pct(pl_pct)}</td>
            </tr>"""
        else:
            tfsa_rows += f"""
            <tr>
              <td class="ticker">{display}</td>
              <td>{h['name']}</td>
              <td>R{entry:.2f}</td>
              <td colspan="5" class="unavail">price unavailable</td>
            </tr>"""

    usd_in_zar_current = usd_current * usdzar
    usd_in_zar_purchase = usd_purchase * usdzar
    grand_purchase = usd_in_zar_purchase + zar_purchase
    grand_current = usd_in_zar_current + zar_current
    grand_pl = grand_current - grand_purchase
    grand_pl_pct = (grand_pl / grand_purchase * 100) if grand_purchase else 0
    zar_pl = zar_current - zar_purchase
    zar_pl_pct = (zar_pl / zar_purchase * 100) if zar_purchase else 0

    dates_rows = "".join(f"<tr><td>{d}</td><td>{e}</td></tr>" for d, e in KEY_DATES)

    def news_card_grid(headlines):
        cards = [
            f'<a class="news-card" href="{h["link"]}" target="_blank">'
            f'<span class="news-source">{h.get("source", "")}</span>'
            f'<div class="news-title">{h["title"]}</div></a>'
            for h in headlines if h.get("link")
        ]
        if not cards:
            return '<p style="color:var(--muted);font-size:12px;">No headlines fetched.</p>'
        return f'<div class="news-grid">{"".join(cards)}</div>'

    rss_items = news_card_grid(rss_headlines)

    stock_news_html = ""
    for ticker, headlines in stock_news.items():
        if headlines:
            tagged = [{**h, "source": ticker} for h in headlines]
            stock_news_html += (
                f'<div class="news-ticker-block">'
                f'<span class="ticker-label">{ticker}</span>'
                f'{news_card_grid(tagged)}'
                f'</div>'
            )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Portfolio Brief - {day_label}</title>
<style>
  :root {{
    --bg: #0f0f13; --surface: #1a1a24; --border: #2a2a38;
    --accent: #7c5cbf; --accent2: #9b7de0; --text: #e0dff0;
    --muted: #7a7a9a; --pos: #4caf7d; --neg: #e05c6a; --ticker: #a89de0;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; line-height: 1.5; padding: 24px; }}
  header {{ display: flex; justify-content: space-between; align-items: flex-end; border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px; }}
  header h1 {{ font-size: 22px; font-weight: 600; color: var(--accent2); }}
  header .meta {{ color: var(--muted); font-size: 12px; text-align: right; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }}
  .grid.thirds {{ grid-template-columns: 1fr 1fr 1fr; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px 20px; }}
  .card.full {{ grid-column: 1 / -1; }}
  .card h2 {{ font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin-bottom: 14px; }}
  .stat {{ font-size: 28px; font-weight: 700; }}
  .stat.pos {{ color: var(--pos); }}
  .stat.neg {{ color: var(--neg); }}
  .stat-label {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ color: var(--muted); font-weight: 500; text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }}
  td {{ padding: 8px 8px; border-bottom: 1px solid #1f1f2c; white-space: nowrap; }}
  tr:last-child td {{ border-bottom: none; }}
  .ticker {{ color: var(--ticker); font-weight: 600; font-family: monospace; font-size: 13px; }}
  .pos {{ color: var(--pos); }}
  .neg {{ color: var(--neg); }}
  .unavail {{ color: var(--muted); font-style: italic; }}
  .total-row td {{ border-top: 1px solid var(--border); font-weight: 600; }}
  .news-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }}
  .news-card {{ display: flex; flex-direction: column; gap: 4px; background: #14141e; border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; text-decoration: none; color: var(--text); }}
  .news-source {{ font-size: 10px; text-transform: uppercase; letter-spacing: .08em; color: var(--accent2); font-weight: 600; }}
  .news-title {{ font-size: 12.5px; line-height: 1.35; }}
  .news-ticker-block {{ margin-bottom: 14px; }}
  .news-ticker-block .ticker-label {{ font-size: 12px; color: var(--ticker); font-weight: 600; font-family: monospace; display: block; margin-bottom: 6px; }}
  footer {{ margin-top: 24px; color: var(--muted); font-size: 11px; border-top: 1px solid var(--border); padding-top: 12px; }}
</style>
</head>
<body>

<header>
  <div>
    <h1>Portfolio Brief</h1>
    <div style="color:var(--muted);font-size:13px;margin-top:4px;">{day_label}</div>
  </div>
  <div class="meta">Generated {ts}<br>USD/ZAR R{usdzar:.2f}</div>
</header>

<div class="grid thirds">
  <div class="card">
    <h2>USD Portfolio</h2>
    <div class="stat {pl_class(usd_pl)}">{fmt_money(usd_pl)}</div>
    <div class="stat-label">{fmt_pct(usd_pl_pct)} on ${usd_purchase:.2f} invested</div>
  </div>
  <div class="card">
    <h2>ZAR Portfolio</h2>
    <div class="stat {pl_class(zar_pl)}">{fmt_money(zar_pl, 'R')}</div>
    <div class="stat-label">{fmt_pct(zar_pl_pct)} on R{zar_purchase:.2f} invested</div>
  </div>
  <div class="card">
    <h2>Grand Total (ZAR equiv)</h2>
    <div class="stat {pl_class(grand_pl)}">{fmt_money(grand_pl, 'R')}</div>
    <div class="stat-label">{fmt_pct(grand_pl_pct)} &nbsp;|&nbsp; R{grand_current:.0f} of R{grand_purchase:.0f}</div>
  </div>
</div>

<div class="card" style="margin-bottom:16px;">
  <h2>EasyEquities USD - DIY</h2>
  <table>
    <thead><tr><th>Ticker</th><th>Name</th><th>Entry</th><th>Price</th><th>Day</th><th>Value</th><th>P/L $</th><th>P/L %</th></tr></thead>
    <tbody>{usd_rows}</tbody>
  </table>
</div>

<div class="card" style="margin-bottom:16px;">
  <h2>EasyEquities ZAR - DIY</h2>
  <table>
    <thead><tr><th>Ticker</th><th>Name</th><th>Entry</th><th>Price</th><th>Day</th><th>Value</th><th>P/L R</th><th>P/L %</th></tr></thead>
    <tbody>{zar_diy_rows}</tbody>
  </table>
</div>

<div class="card" style="margin-bottom:16px;">
  <h2>TFSA</h2>
  <table>
    <thead><tr><th>Ticker</th><th>Name</th><th>Entry</th><th>Price</th><th>Day</th><th>Value</th><th>P/L R</th><th>P/L %</th></tr></thead>
    <tbody>{tfsa_rows}</tbody>
  </table>
</div>

<div class="card" style="margin-bottom:16px;">
  <h2>Key Dates</h2>
  <table><tbody>{dates_rows}</tbody></table>
</div>

<div class="card full" style="margin-bottom:16px;">
  <h2>Market News</h2>
  {rss_items}
</div>

<div class="card full" style="margin-bottom:16px;">
  <h2>Stock Headlines</h2>
  {stock_news_html if stock_news_html else '<p style="color:var(--muted);font-size:12px;">No headlines fetched.</p>'}
</div>

<footer>
  Prices: yfinance (previous close). Bond value static - update manually after coupon.
  News: Moneyweb / Yahoo Finance / CNBC RSS.
</footer>

</body>
</html>"""


# ── Main ─────────────────────────────────────────────────────────────────────
def already_ran_today() -> bool:
    """Skip if today's HTML already exists and was written in the last 20 hours."""
    if HTML_PATH.exists():
        age_hours = (datetime.now().timestamp() - HTML_PATH.stat().st_mtime) / 3600
        if age_hours < 20:
            print(f"Already ran today ({HTML_PATH.name} is {age_hours:.1f}h old). Skipping.")
            return True
    return False


def main():
    if already_ran_today():
        return

    print(f"portfolio_brief.py - {TODAY}")
    print("-" * 40)

    print("Syncing positions from EasyEquities...")
    sync_from_easyequities()

    all_usd = list(USD_HOLDINGS.keys())
    all_zar_diy = list(ZAR_DIY.keys())
    all_zar_tfsa = list(ZAR_TFSA.keys())

    print("Fetching USD prices...")
    usd_prices = fetch_prices(all_usd)

    print("Fetching ZAR prices...")
    zar_diy_prices = fetch_prices(all_zar_diy, jse=True)
    zar_tfsa_prices = fetch_prices(all_zar_tfsa, jse=True)

    apply_ee_prices(USD_HOLDINGS, usd_prices)
    apply_ee_prices(ZAR_DIY, zar_diy_prices)
    apply_ee_prices(ZAR_TFSA, zar_tfsa_prices)

    print("Fetching USD/ZAR rate...")
    usdzar = fetch_usdzar()
    print(f"  R{usdzar:.2f}")

    print("Fetching RSS headlines...")
    rss = fetch_rss(RSS_FEEDS, n=RSS_ITEMS)

    print("Fetching stock news...")
    stock_news = {t: fetch_stock_headlines(t, n=STOCK_NEWS) for t in STOCK_NEWS_TICKERS}

    usd_purchase = sum(h["purchase_usd"] for h in USD_HOLDINGS.values())
    usd_current = sum(
        h["shares"] * (usd_prices.get(t, {}).get("price") or h["purchase_usd"] / h["shares"])
        for t, h in USD_HOLDINGS.items()
    )
    zar_purchase = (
        sum(h["purchase_zar"] for h in ZAR_DIY.values())
        + BOND["purchase_zar"]
        + sum(h["purchase_zar"] for h in ZAR_TFSA.values())
    )
    zar_current = (
        sum(
            h["shares"] * (zar_diy_prices.get(t, {}).get("price") or h["purchase_zar"] / h["shares"])
            for t, h in ZAR_DIY.items()
        )
        + BOND["current_zar"]
        + sum(
            h["shares"] * (zar_tfsa_prices.get(t, {}).get("price") or h["purchase_zar"] / h["shares"])
            for t, h in ZAR_TFSA.items()
        )
    )

    note = build_note(usd_prices, zar_diy_prices, zar_tfsa_prices, usdzar, rss, stock_news)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    NOTE_PATH.write_text(note, encoding="utf-8")
    print(f"Markdown: {NOTE_PATH}")

    html = build_html(
        usd_prices, zar_diy_prices, zar_tfsa_prices, usdzar,
        rss, stock_news, usd_purchase, usd_current, zar_purchase, zar_current,
    )
    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"HTML:     {HTML_PATH}")
    webbrowser.open(HTML_PATH.as_uri())


if __name__ == "__main__":
    main()
