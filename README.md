# Bee Buddy

A self-hosted, ad-free, mobile-friendly view of your NYT Spelling Bee progress: the grid of remaining words, the two-letter list, your pangrams, and how you compare with other Bee Buddy visitors. It updates as you play.

It runs on your own machine (a Raspberry Pi works well) using **your own NYT subscription**. It isn't affiliated with or endorsed by The New York Times.

```
            phone / browser
                  │  :8090 (public)
         ┌────────▼────────┐  static page, /buddy.json,
         │  web (nginx)    │  POST /api/sync, GET /api/status
         └────────┬────────┘  (rate limited; everything else is a 404)
   backend network│  shared volume: buddy.json (counts only, no answers)
         ┌────────▼────────┐
         │  collector      │  headless Chromium (Playwright) + FastAPI,
         │                 │  your NYT session, admin page
         └────────┬────────┘
                  │  127.0.0.1:8091 only
          http://localhost:8091  (admin)
```

## How it works

1. When the page opens, it shows the last saved copy straight away and asks the collector to sync. While the page is open it syncs again every 60 seconds, and whenever you switch back to the tab.
2. The collector keeps one headless Chromium running with your NYT session cookie, with a tab parked on nytimes.com. A sync fetches the same two small JSON files the game itself uses, today's puzzle and your game state, so it usually takes well under a second. If that fails, it falls back to loading the full Spelling Bee page.
3. It works out the grid, two-letter list, points, rank and pangrams, then writes `buddy.json` for the public page.
4. When your found words change, it reads Bee Buddy's **You vs. other Bee Buddy visitors** stats in the background, loading Bee Buddy's page without ads, images or fonts. That's slower than the rest, so the section can lag a few seconds behind. To bridge the gap, a newly found word moves into the stats straight away when it's the only unfound word with its first letter and length, because then its percentage is already known.

**The answer list never leaves the collector.** The public file contains counts only. Like Bee Buddy, the visitor stats name only the words you've found; the rest appear as first letter and length.

However many people open the page, NYT is fetched at most once per `MIN_SYNC_INTERVAL` (5 s by default), and only one sync runs at a time. Bee Buddy's page is loaded at most once every 30 seconds.

## Requirements

- Docker with the Compose plugin
- An NYT subscription that includes Games
- On a Raspberry Pi: a **64-bit** OS (`uname -m` shows `aarch64`) and 2 GB+ RAM

## Quick start

```bash
git clone https://github.com/MrKieren/spelling-bee-buddy.git
cd spelling-bee-buddy
cp .env.example .env                              # optional: change ports and timings
mkdir -p debug && sudo chown 1000:1000 debug      # Linux / Pi only
docker compose up -d --build                      # first build takes a while on a Pi
```

Then:

1. Open the **admin page** at `http://localhost:8091` on the machine running Docker.
2. Paste in your NYT session cookie (see below) and click **Save and sync**.
3. Open the **public page** at `http://<host>:8090`.

To try it without NYT first, set `FAKE_NYT=1` in `.env`. Each sync then "finds" one more word in a built-in demo puzzle.

## Your NYT session cookie

The collector never logs in itself. NYT's bot protection blocks automated logins, so it reuses the session from your own browser:

1. Log in to nytimes.com in desktop Firefox.
2. Press **F12** and go to **Storage → Cookies → https://www.nytimes.com**.
3. Copy the value of **`NYT-S`** and paste it into the admin page.

When the session expires, the public page says "NYT login needed". Paste a fresh value the same way.

Alternatively, you can put the cookie in `secrets/cookies.txt`, in Netscape cookies.txt format:

```
.nytimes.com	TRUE	/	TRUE	2000000000	NYT-S	<value>
```

The collector uses whichever of the two was updated most recently, and reads them on every sync, so no restart is needed.

## Admin page

`http://localhost:8091` shows whether you're logged in to NYT, when it last synced and your progress. It has a **Sync now** button and the cookie form.

It's published on `127.0.0.1` only, so it works from the machine running Docker and nowhere else, not even from other devices on your home network. That's deliberate: the page has no password, so anyone who could reach it could change your NYT cookie.

### Reaching it from another computer (e.g. a headless Pi)

Open an SSH tunnel **from the computer you want to browse on**, not from the Pi:

```bash
ssh -L 8091:127.0.0.1:8091 pi@<pi-address>
```

Leave that session open, then browse to **`http://localhost:8091`** on the same computer. The tunnel carries the request to the Pi, where the admin page sees it as local.

If it doesn't load:

- **`channel N: open failed: connect failed`** in the SSH window means the tunnel reached the Pi but nothing answered on port 8091. On the Pi, check that the containers are running with `docker compose ps` (look for `127.0.0.1:8091->8000`), and test the page locally with `curl -s http://127.0.0.1:8091/ | head -3`.
- **Use `127.0.0.1`, not `localhost`, on the Pi side of the tunnel.** On some systems `localhost` resolves to the IPv6 address `::1` first, and the admin port only listens on IPv4.
- **Port 8091 already in use on your computer** (for example, another copy of Bee Buddy running there): pick a different local port, e.g. `ssh -L 9091:127.0.0.1:8091 pi@<pi-address>`, and open `http://localhost:9091`.

## Using it away from home

Expose **port 8090 only**, never 8091. A tunnel avoids opening ports on your router and gives you HTTPS:

- **Cloudflare Tunnel:** free, but needs a domain whose DNS is on Cloudflare.
- **Tailscale Funnel:** free, and gives you a fixed `*.ts.net` address without buying a domain.

The page sends `noindex` headers and blocks crawlers in `robots.txt`, but anyone with the URL can see your progress.

## Configuration

All settings are optional; put them in `.env`.

| Setting | Default | What it does |
|---|---|---|
| `PUBLIC_PORT` | `8090` | Port for the public page |
| `ADMIN_PORT` | `8091` | Port for the admin page (bound to `127.0.0.1`) |
| `MIN_SYNC_INTERVAL` | `5` | Minimum seconds between NYT fetches |
| `BACKGROUND_SYNC_MINUTES` | `60` | Sync even when nobody has the page open (`0` = only on visits) |
| `VISITOR_STATS` | `1` | Include the "You vs. other visitors" section |
| `VISITOR_STATS_MINUTES` | `15` | Re-read visitor stats at least this often, even without new words |
| `DEBUG_CAPTURE` | `0` | Save requests, the Buddy page and screenshots to `./debug` on each sync |
| `FAKE_NYT` | `0` | Use a built-in demo puzzle instead of NYT |
| `BROWSER_RESTART_HOURS` | `6` | Restart the collector's browser this often, to keep its memory in check |

After changing `.env`, run `docker compose up -d`.

## Updating

```bash
git pull
docker compose up -d --build
```

Your NYT cookie is kept in the `profile` volume, so it survives rebuilds.

## Troubleshooting

- **Logs:** `docker compose logs -f collector`. A healthy sync looks like `Sync ok in 0.4s: 50/70 words (source=api-fast, logged_in=True)`. The first sync after a start or browser restart takes a few seconds longer while the browser starts. If you see `Quick sync failed … loading the full page instead` on every sync, NYT has changed something; syncs still work, just slowly.
- **`logged_in=False`:** the cookie is missing or expired. Paste a fresh one on the admin page.
- **Blocked as a robot:** NYT's bot protection is much stricter for VPN and datacenter addresses. Run the collector from your home connection; if the host routes everything through a VPN, exclude Docker's traffic from it.
- **Wrong counts after an NYT change:** set `DEBUG_CAPTURE=1`, run a sync, and look in `./debug` for `capture-*.json`, `buddy.html` and the screenshots. Then adjust the parsing in `collector/app/nyt.py`. Debug files contain puzzle answers, and `.gitignore` keeps them out of the repo.

## Development

```
collector/            private service (Python, FastAPI, Playwright)
  app/main.py         sync scheduling, public /sync and /status, admin API
  app/nyt.py          headless browser: puzzle, found words, visitor stats
  app/buddy.py        Buddy maths; builds the public data (no answers)
  app/session.py      where the NYT cookie comes from
  app/admin.html      admin page
  app/fake.py         demo puzzle for FAKE_NYT=1
  tests/              unit tests
web/                  public service (nginx)
  nginx.conf          the only routes that exist publicly
  static/             the page (plain HTML, CSS and JS)
docker-compose.yml
```

Run the tests:

```bash
cd collector
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

To work on the page without NYT, run the stack with `FAKE_NYT=1`.

## Security notes

- Only `web` is reachable from outside. The collector is published on `127.0.0.1` for the admin page, and the public server can only reach its `/sync` and `/status`.
- The public data is checked so it can't name a word you haven't found; there's a test for this.
- Your NYT cookie (in the `profile` volume, or `secrets/cookies.txt`) can access your NYT account. Treat it like a password; both locations are kept out of git.
