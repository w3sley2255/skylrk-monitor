# SKYLRK Clothing Monitor

Monitors the SKYLRK online store and alerts you when a **clothing** product is
newly launched, becomes available in **XS or S**, is **restocked** in XS/S, or
changes in a meaningful way (price, name, color, image, stock status).

It reads the store's **public Shopify product JSON** (structured data, not HTML
scraping), keeps a small state file so only *changes* alert, and sends a
notification to Telegram (or Discord / email). It runs free on GitHub Actions.

> The change-detection engine is covered by an 8-test suite (`tests/test_diff.py`)
> that passes, plus a live console demo (seed → restock → dedupe). See §H.

---

## A. Assumptions

1. **Platform = Shopify.** Verified from the live pages: `meta-shopify-checkout-api-token`,
   the `/cdn/shop/files/…` image CDN, `?variant=<id>` product URLs, the Shopify
   digital-wallet path (store id `63182143667`), and Shopify Markets currency
   switching. This is a hard fact, not a guess.
2. **Public product JSON is available.** Shopify serves `/products.json` (paginated,
   all *published* products with variants and an `available` flag per variant) by
   default. I could not fetch it directly in my research environment (URL-fetch
   sandboxing), so **Step 0 of setup is a one-line check** that it returns data.
   The code fails gracefully with a clear message + fallback note if it's disabled.
3. **"Clothing" must be filtered.** The catalogue mixes apparel (hoodies, tees,
   shorts, sweats/joggers, beanies) with accessories (sunglasses, "BUMP CASE"
   phone cases, beach slides). The monitor treats a product as clothing if it has
   a Size option with letter sizes (XS–4XL) or its `product_type` is allow-listed
   — both editable in `config.yaml`.
4. **Sizes.** XS/S are the targets. "Extra Small"/"Small" and punctuation/case
   variants are normalized to `XS`/`S` via an editable synonym map.
5. **Currency = USD** (confirmed from `og:price:currency` on a product page). The
   store can *display* other currencies via Shopify Markets, but the JSON base
   price is USD.
6. **Timezone.** The store shows no explicit timezone. Per your spec, every alert
   and log records **both UTC and Asia/Hong_Kong**.
7. **Notification channel = Telegram** (default). Free, instant, supports images,
   works in HK, trivial setup. Discord (webhook) and email (SMTP) adapters are
   included and swappable via one config line.
8. **Cadence = every 15 minutes** via GitHub Actions cron (documented caveats in §G).
9. **Personal, low-volume use.** ~96 requests/day to one public endpoint — far
   below any reasonable rate limit and the same data search engines read.

---

## B. Website monitoring approach

**What I inspected and found (evidence-based):**

| Data you asked for | Where it lives on SKYLRK |
|---|---|
| Product catalogue | Shopify `/products.json?limit=250&page=N` (all published products) |
| Per-size availability | `variant.available` (`true`/`false`) in that JSON |
| Size / Color | `product.options` (Size, Color) mapped to `variant.option1/2/3` |
| Price + currency | `variant.price` (base USD); confirmed via `og:price:*` on product pages |
| Product name | `product.title` |
| Image | `product.images[0].src` on the `/cdn/shop/files/…` CDN |
| Direct link | `https://skylrk.com/products/<handle>` (canonical) |
| Launch status | Presence in `/products.json` = published/live |

**Why JSON, not HTML scraping.** When I rendered a product page, the size buttons
were visible but the *sold-out state of each size was not reliably present* in the
extracted HTML. The Shopify JSON gives an explicit `available` boolean per variant,
which is exactly what "XS/S restock" detection needs. JSON is also stable across
theme/layout redesigns, satisfying your "easy to update if the site changes" goal.

**Endpoints used (all Shopify-standard, public, unauthenticated):**
- `GET /products.json?limit=250&page=N` — primary catalogue sweep.
- `GET /products/<handle>.js` — optional per-product fallback for the freshest
  per-variant availability (not required by default).
- `GET /sitemap.xml` — optional product discovery.
- `GET /robots.txt` — checked before every run (see §J).

No login, no cart, no checkout, no CAPTCHA, no anti-bot bypass — none of that is
touched.

---

## C. Options comparison

| Option | Reliability | Ease (non-expert) | Cost | Maintenance | Verdict |
|---|---|---|---|---|---|
| **GitHub Actions (recommended)** | High (managed cron); minor schedule jitter | High — no server, secrets built in | Free tier is ample | Low — edit one YAML/config | ✅ Best balance |
| Local app (cron / Task Scheduler) | Only as reliable as your always-on machine | Medium | Free (needs a PC/Pi on 24/7) | You patch the OS/runtime | Good if you have a home server |
| Cloud function (Lambda+EventBridge / Cloud Functions+Scheduler) | Very high | Low — IAM, state store setup | ~Free at this volume | Medium | Overkill for one store |
| Power Automate | Medium | Low for this logic | HTTP action needs a **paid** plan | Medium | Poor fit for variant diffing |
| No-code watchers (Visualping/Distill/Wachete) | Medium | Very high | Paid once you need many URLs + intervals | Low | Weak at per-size/restock logic |

**Recommendation: GitHub Actions.** No machine to keep on, encrypted secret store
built in, free scheduled runs, and the state file is versioned in the repo (a free
audit log of every change). The *same* `skylrk_monitor.py` also runs locally, so
you're never locked in.

---

## D. Recommended architecture

```
                        ┌──────────────────────────────────────────┐
   every 15 min (cron)  │            skylrk_monitor.py             │
   ───────────────────► │                                          │
                        │  FETCH ─► NORMALIZE ─► DIFF ─► NOTIFY     │
                        │    │          │          │        │      │
                        └────┼──────────┼──────────┼────────┼──────┘
                             │          │          │        │
          GET /products.json │   clothing filter   │        └─► Telegram / Discord / Email
          (+ robots check,   │   + size normalize  │
           retries/backoff)  │                      └─► compare vs state/state.json
                                                        (new / restock / update) + cooldown
                                                              │
                                                              ▼
                                                   state/state.json  (committed back)
                                                   logs (Actions run log + local file)
```

Single flow, run to completion each cycle: fetch the catalogue → keep clothing
only and normalize sizes → diff against the last saved snapshot → send alerts for
real changes → save the new snapshot. State on disk is what makes it stateless
between runs and duplicate-free.

---

## E. Setup and configuration

### Required software & accounts
- **Python 3.9+** (repo tested on 3.12). Dependencies: `requests`, `PyYAML`,
  `python-dotenv` (see `requirements.txt`).
- A **GitHub account** (free) for the recommended deployment.
- A **Telegram account** (free) for the default channel.

### Step 0 — Verify the store actually exposes JSON (do this first)
On any machine with `curl`:
```bash
curl -sA "SKYLRK-Personal-Restock-Monitor/1.0 (contact: you@example.com)" \
  "https://skylrk.com/products.json?limit=1" | head -c 400
```
- You should see JSON starting with `{"products":[{...`. ✅ Proceed.
- If you get HTML, a `404`, or an error, the endpoint is disabled — see §J for the
  compliant fallback (native "notify me" / sitemap-based page checks).

### Step 1 — Get the code
```bash
git clone <your-repo-url> skylrk-monitor    # or copy this folder
cd skylrk-monitor
pip install -r requirements.txt
```

### Step 2 — Create your notification channel

**Telegram (recommended):**
1. In Telegram, message **@BotFather** → `/newbot` → follow prompts → copy the
   **bot token**.
2. Message your new bot once (say "hi") so it's allowed to message you.
3. Get your **chat id**: message **@userinfobot**, or open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` after messaging your bot and
   read `chat.id`.

**Discord (alternative):** Server Settings → Integrations → Webhooks → New Webhook
→ copy the URL. Set `notify.channel: discord` and provide `DISCORD_WEBHOOK_URL`.

**Email (alternative):** provide SMTP settings (for Gmail use an **App Password**,
not your login). Set `notify.channel: email`.

### Step 3 — Configure secrets (never hard-coded)

**Local:** copy `.env.example` → `.env` and fill in. `.env` is gitignored.
```bash
cp .env.example .env
# edit .env: TELEGRAM_BOT_TOKEN=... and TELEGRAM_CHAT_ID=...
```

**GitHub Actions:** repo → **Settings → Secrets and variables → Actions → New
repository secret**. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (or your
chosen channel's secrets). The workflow reads them as encrypted env vars.

### Step 4 — Set your contact UA and confirm settings
Edit `config.yaml`: put a real email in `network.user_agent`, confirm
`monitoring.target_sizes: ["XS","S"]` and `notify.channel`.

---

## F. Implementation

### Component / file structure
```
skylrk-monitor/
├── skylrk_monitor.py            # the whole monitor, in labelled sections:
│                                #   CONFIG · LOGGING · FETCH · NORMALIZE
│                                #   · DIFF · NOTIFY · STATE · RUN(main)
├── config.yaml                  # editable settings (sizes, filter, channel) — your main knob
├── .env.example                 # secret NAMES only (copy to .env locally)
├── requirements.txt
├── state/
│   └── state.json               # persisted snapshot + dedupe ledger (committed by CI)
├── logs/
│   └── monitor.log              # local rotating log (Actions also keeps run logs)
├── tests/
│   ├── fixtures/                # sample products.json states (baseline/restock/new/price)
│   └── test_diff.py             # 8 passing tests for the detection logic
└── .github/workflows/monitor.yml# 15-min cron + secrets + state commit-back
```
(Kept as one module on purpose so a non-expert can deploy a single file. The
section headers map 1:1 to the components above; split into a package later if you
want — the README's structure guides that.)

### Persistent state (products, variants, prior availability)
`state/state.json`:
```jsonc
{
  "schema_version": 1,
  "last_check": { "utc": "...", "hkt": "...", "status": "ok", "products_tracked": 12 },
  "products": {
    "<product_id>": {
      "handle": "reverse-hoodie-thistle",
      "title": "REVERSE HOODIE",
      "price": "140.00", "currency": "USD",
      "colors": ["THISTLE"],
      "image": "https://…/FRONT.png",           // query-string stripped (see below)
      "overall_available": true,
      "first_seen_utc": "…",
      "variants": {
        "<variant_id>": { "size": "XS", "size_raw": "XS", "color": "THISTLE",
                          "price": "140.00", "available": false }
      }
    }
  },
  "ledger": { "restock:<pid>:THISTLE:XS": "<iso timestamp>" }   // for cooldown
}
```
Writes are **atomic** (temp file + `os.replace`) so a crash mid-write can't corrupt
state. `first_seen_utc` is preserved across runs.

### Change-detection logic
Each run builds a fresh snapshot and compares it to `products` in state:
- **NEW_LAUNCH** — a product id not seen before (clothing only). First-ever run
  seeds silently (no blast of "new" alerts) unless `alert_on_first_run: true`.
- **XS_S_RESTOCK** — a target-size variant goes **unavailable/absent → available**.
  Grouped per color so the message names the right color and sizes.
- **PRODUCT_UPDATE** — any tracked field changes: `title`, representative `price`,
  `colors`, primary `image`, or overall `stock status`. A message lists exactly
  what changed (old → new).

### Duplicate prevention (two layers)
1. **State transitions.** An event only exists when the *stored* value differs from
   *now*. After a successful cycle the snapshot is saved, so the same state won't
   re-fire. Image URLs are compared with the `?v=` cache-buster **stripped**, so
   Shopify re-publishing the same asset doesn't cause false "image changed" spam.
2. **Cooldown ledger.** Every sent alert records a signature + timestamp. An
   identical alert is suppressed for `cooldown_hours` (default 6) — this absorbs a
   variant that flaps in/out of stock.

### Reliability details
- **Retries/backoff:** transient errors and `429/5xx` retry with exponential
  backoff + jitter (~5s, 10s, 20s, 40s), honoring `Retry-After`. A hard failure is
  logged, recorded in `last_check.status=error`, and **does not wipe state**.
- **At-least-once delivery:** if a send fails, that product keeps its *old* state so
  the change re-triggers next run (verified by a test). Trade-off chosen: never
  miss a restock, at the cost of a rare duplicate.
- **Pagination:** loops pages until a short/empty page.
- **robots.txt courtesy check** before fetching (see §J).

---

## G. Deployment and scheduling

### Recommended: GitHub Actions
1. Push this folder to a GitHub repo (private is fine).
2. Add the secrets (Step 3 above).
3. **Seed once** so you don't get a wall of "new" alerts on first real run. Either
   run locally: `python skylrk_monitor.py --seed` and commit `state/state.json`, or
   just let the first scheduled run seed silently (default `alert_on_first_run:false`).
4. The workflow (`.github/workflows/monitor.yml`) runs every 15 min, sends alerts,
   and commits the updated state back. Trigger a manual run any time from the
   **Actions** tab (**Run workflow**).

**Cron caveats (be aware):** GitHub may delay scheduled jobs by a few minutes under
load and occasionally skip one; and it disables schedules after 60 days of repo
inactivity — the per-run **state commit keeps the repo active**, so this won't bite
you. For sub-minute precision you'd need a cloud scheduler, which is overkill here.

### Alternative: run locally on a schedule
- **Linux/macOS cron** (every 15 min):
  ```cron
  */15 * * * * cd /path/to/skylrk-monitor && /usr/bin/python3 skylrk_monitor.py --config config.yaml >> logs/cron.out 2>&1
  ```
- **Windows Task Scheduler:** create a task → trigger every 15 min → action:
  `python C:\path\skylrk-monitor\skylrk_monitor.py`.
Keep the machine on for checks to happen.

---

## H. Testing checklist

Everything here is runnable offline with the bundled sample states — no live site
needed.

- [ ] **Unit tests pass:** `python tests/test_diff.py` → `8/8 tests passed`.
  Covers: clothing filter excludes accessories; first run seeds silently; identical
  snapshot = no alerts; XS restock fires exactly once with correct size/color; new
  product fires once; price change fires one update (not a restock); cooldown
  suppresses then re-allows; failed send retries next run.
- [ ] **Dry-run the detector** against a sample state (prints, sends/saves nothing):
  ```bash
  python skylrk_monitor.py --fixture tests/fixtures/run2_restock.json --dry-run
  ```
- [ ] **End-to-end console demo** (no secrets needed):
  ```bash
  rm -f state/state.json
  python skylrk_monitor.py --fixture tests/fixtures/run1_baseline.json --seed --channel console
  python skylrk_monitor.py --fixture tests/fixtures/run2_restock.json --channel console   # ONE XS alert
  python skylrk_monitor.py --fixture tests/fixtures/run2_restock.json --channel console   # dedupe: none
  ```
- [ ] **Live smoke test (seeds real state):**
  ```bash
  python skylrk_monitor.py --seed --verbose        # fetches live, prints clothing count, no alerts
  ```
- [ ] **Channel test:** temporarily set a fixture product to trigger, run with your
  real channel, confirm the message + image arrive, then reset.

Expected alert format (matches your spec):
```
XS/S RESTOCK: REVERSE HOODIE
Available sizes: XS
Color: THISTLE
Price: USD 140.00
Detected: 2026-07-21T18:06:15+08:00 (HKT) / 2026-07-21T10:06:15+00:00 (UTC)
What changed: Now available in XS (THISTLE)
Product: https://skylrk.com/products/reverse-hoodie-thistle
```

---

## I. Troubleshooting and maintenance

### Troubleshooting
| Symptom | Likely cause / fix |
|---|---|
| Step 0 returns HTML/404 | `/products.json` disabled → use §J fallback |
| No alerts ever | Still in cooldown, or nothing changed; run `--dry-run`, check `logs/` and Actions run log |
| "Missing required environment variable" | Secret not set (local `.env` or GitHub secret) for the selected channel |
| Accessories alerting as clothing | Add the `product_type` to `clothing_filter.product_type_blocklist` or a handle to `handle_blocklist` |
| A real clothing item ignored | Add its `product_type` to `product_type_allowlist`, or its odd size label to `size_synonyms` |
| Duplicate-ish alerts | Increase `monitoring.cooldown_hours` |
| `429`/timeouts in logs | Backoff already handles it; if persistent, raise `interval_minutes` / lengthen the cron |
| robots stop ("compliance stop") | The store disallowed the endpoint for bots → stop and use §J fallback |
| Actions schedule not firing | Ensure the repo is active (state commits keep it alive); trigger once manually |

### Pause / uninstall / modify
- **Pause:** Actions tab → select the workflow → **Disable workflow**. (Local:
  remove the cron line / disable the scheduled task.)
- **Resume:** **Enable workflow** (or re-add cron).
- **Uninstall:** delete the repo (or the local folder). Revoke the Telegram bot via
  BotFather `/deletebot` and delete any secrets. No third-party data persists
  outside your own repo/machine.
- **Modify:** almost everything lives in `config.yaml` — target sizes, clothing
  rules, size synonyms, cooldown, channel. Change the schedule in the workflow's
  `cron` (or your crontab).

### Maintenance plan (if the site structure changes)
Because this uses Shopify's JSON API, cosmetic theme/HTML redesigns **won't** break
it. Watch for these instead:
1. **`/products.json` disabled or moved** — Step 0 check fails; the run logs a clear
   error. Fallback: switch to per-product `/products/<handle>.js` (discover handles
   via `/sitemap.xml`) or the §J compliant alternative.
2. **New size labels** (e.g. "SM", "US S") — add to `size_synonyms`.
3. **New non-clothing categories** — add their `product_type` to the blocklist.
4. **Shopify JSON schema tweak** — extraction is isolated in the `NORMALIZE`
   section; only that needs touching. Re-run `python tests/test_diff.py` after any
   edit to confirm nothing regressed.
Recommended cadence: a 5-minute sanity check (`--seed --verbose` + the test suite)
whenever alerts look off, and after any SKYLRK site relaunch.

---

## J. Compliance, limitations, and costs

### Compliance
- **robots.txt:** the monitor reads `https://skylrk.com/robots.txt` before each run
  and **won't fetch a disallowed path** (`network.respect_robots_txt: true`).
  Shopify's default robots.txt does not disallow `/products.json` and blocks only
  cart/checkout/admin/account — but **verify the live file yourself**, since store
  owners can customize it.
- **Terms of Service:** read `https://skylrk.com/pages/terms-of-service` and
  `/pages/policies`. If they prohibit automated access/scraping, **stop** and use a
  compliant alternative (below) or ask the store for permission.
- **Good citizen:** one public endpoint, ~15-min cadence, a descriptive User-Agent
  with your contact, conservative backoff, and honoring `Retry-After`. No auth,
  CAPTCHA, or anti-bot circumvention. No cart/checkout automation. This is
  read-only public catalogue data.
- **Privacy / data minimization:** collects only product data. No customer or
  personal data. Secrets are never hard-coded or logged (logs auto-redact tokens,
  webhooks, and SMTP creds); `.env` is gitignored; GitHub secrets are encrypted.

### Compliant fallback (if automated polling isn't permitted or JSON is off)
- Use SKYLRK's **native "notify me / back in stock"** on a sold-out XS/S variant if
  offered (most compliant for restocks).
- Or a **no-code page watcher** (Visualping/Distill) pointed at specific product
  URLs, respecting their ToS — weaker on per-size logic but zero scraping code.
- Or subscribe to the brand's newsletter/Instagram for launches.

### Costs
- **GitHub Actions:** free tier covers this comfortably (each run is seconds; ~96
  runs/day). Private-repo minutes are generous; public repos are unlimited. No cost
  expected at this volume.
- **Telegram / Discord webhooks:** free.
- **Email:** free with your existing provider (App Password).
- **Local option:** free apart from keeping a machine powered on.
- **No third-party paid services required.** (Only Power Automate's HTTP action or
  scaled no-code watchers would introduce fees — hence not recommended.)

---

### Quick command reference
```bash
python tests/test_diff.py                                   # run tests
python skylrk_monitor.py --seed                             # seed state, no alerts
python skylrk_monitor.py --fixture <file> --dry-run         # detect only, print
python skylrk_monitor.py --fixture <file> --channel console # offline demo
python skylrk_monitor.py --config config.yaml               # a normal run
```
