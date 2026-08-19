# Portfolio Tracker

A daily-run script that pulls live prices, computes real profit/loss against
actual entry positions, and renders a Markdown + HTML portfolio brief with
market news alongside it.

I built this to track my own EasyEquities investments daily without opening
the app every morning. This public copy uses example holdings and example
financial figures in place of my real portfolio data - the code and
architecture are unchanged from what I actually run.

## What it does

1. **Syncs live holdings from EasyEquities.** EasyEquities has no public
   API, so `sync_from_easyequities()` drives its own web login flow
   (two-step EasyID form submission, session cookies, redirect chain) via
   `easy_equities_client` + `BeautifulSoup`, then pulls current holdings
   per account (USD DIY, ZAR DIY, TFSA).
2. **Falls back to hardcoded positions** if no credentials file is present
   or the sync fails, so the script still runs standalone.
3. **Fetches live prices** for every ticker via `yfinance`, plus the
   USD/ZAR exchange rate, and computes entry price, current value, and
   P/L in both dollar/rand terms and percentage.
4. **Pulls market news** from RSS feeds (Moneyweb, Yahoo Finance, CNBC)
   and per-ticker headlines from yfinance.
5. **Renders two outputs**: a Markdown note (for pasting into a notes app)
   and a styled standalone HTML dashboard, opened automatically in the
   browser.
6. **Is idempotent per day** - if it already ran in the last 20 hours it
   skips, so it's safe to trigger from a scheduled task without
   duplicating work.

## Running it

```bash
pip install -r requirements.txt
python portfolio_brief.py
```

To sync real holdings instead of the example ones, copy
`ee_credentials.example.json` to `ee_credentials.json` and fill in your
EasyEquities login. That file is gitignored and never gets committed.

Edit `USD_HOLDINGS`, `ZAR_DIY`, `ZAR_TFSA`, and `BOND` near the top of
`portfolio_brief.py` to match your own starting positions (these get
overwritten automatically once EasyEquities sync succeeds).

## Known limitations

- **EasyEquities has no official API.** The sync logs in through the same
  web form a browser would use. If EasyEquities changes their sign-in
  page layout, `_ee_login()` breaks until it's updated to match.
- **Bond / fixed-income positions have no live price feed** and need their
  current value updated manually after each coupon payment.
- **Share counts aren't tracked transactionally.** Buying or selling
  requires updating the position dict by hand (or relying on the
  EasyEquities sync, which reflects current state but not history).

## Roadmap

- Persist positions to a small local file/DB instead of in-script dicts,
  so buys/sells don't require editing code.
- Track transaction history instead of just current snapshot, so realized
  vs unrealized P/L can be split out.
- Add a scheduled-task/cron setup script instead of manual daily runs.
- **Intraday monitor (in development).** Watches only held positions during
  market hours on a 20-minute interval, and stays silent unless a position
  moves past a set threshold or genuinely new news lands on one of them.
  Silence is the default, since a monitor that talks constantly is one you
  stop reading. A working prototype runs today; what it still needs is
  somewhere to run other than an open session on my laptop, and deeper
  per-holding insight rather than price movement alone.

## AI-workflow annexure

See [AI-ANNEXURE.md](AI-ANNEXURE.md) for the full account of how AI was used
here, what it got wrong, what only surfaced against the live service, and what
was verified by hand before being trusted. Roughly 20 hours of work, about 15
of it troubleshooting.
