#!/usr/bin/env python3
"""
SKYLRK clothing monitor
=======================
Polls the SKYLRK Shopify storefront's PUBLIC product JSON, detects meaningful
changes for CLOTHING products (new launches, XS/S availability/restocks, and
product-page updates), and sends a notification. State is stored on disk so
that only *changes* trigger alerts and duplicates are suppressed.

This single file is intentionally organised into clearly labelled sections so a
non-expert can read, run, and maintain it:

    CONFIG   - load settings (config.yaml) and secrets (environment variables)
    LOGGING  - console + rotating file log, with secret redaction
    FETCH    - HTTP with retries/backoff, pagination, robots.txt courtesy check
    NORMALIZE- turn raw Shopify JSON into a clean model; size + clothing rules
    DIFF     - compare new snapshot vs saved state -> list of events
    NOTIFY   - Telegram / Discord / Email / Console adapters
    STATE    - atomic load/save of state.json + duplicate-suppression ledger
    RUN      - wire everything together (this is main())

Nothing secret is ever written to disk or logs. See README.md for full docs.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import random
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import requests

try:
    import yaml
except ImportError:  # pragma: no cover
    print("Missing dependency PyYAML. Run: pip install -r requirements.txt", file=sys.stderr)
    raise

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads a local .env if present; no-op in CI
except ImportError:  # dotenv is optional
    pass

HKT = ZoneInfo("Asia/Hong_Kong")
SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    "store": {
        "base_url": "https://skylrk.com",
        "products_endpoint": "/products.json",
        "currency": "USD",
        "page_size": 250,
        "max_pages": 20,
    },
    "monitoring": {
        "target_sizes": ["XS", "S"],
        "interval_minutes": 15,
        "alert_on_first_run": False,
        "alert_on_new_launch": True,
        "alert_on_restock": True,
        "alert_on_product_update": True,
        "alert_on_removal": False,
        "cooldown_hours": 6,
    },
    "clothing_filter": {
        "require_size_option": True,
        "size_option_names": ["Size"],
        "color_option_names": ["Color", "Colour"],
        "apparel_letter_sizes": ["XXS", "XS", "S", "M", "L", "XL", "XXL", "2XL", "3XL", "4XL"],
        "product_type_allowlist": [],
        "product_type_blocklist": [
            "Sunglasses", "Eyewear", "Phone Case", "Case", "Slides", "Footwear",
            "Accessory", "Accessories", "Gift Card", "Wallpaper",
        ],
        "handle_blocklist": [],
    },
    "size_synonyms": {
        "extra small": "XS", "x small": "XS", "x-small": "XS", "xsmall": "XS",
        "small": "S",
        "medium": "M",
        "large": "L",
        "extra large": "XL", "x large": "XL", "x-large": "XL", "xlarge": "XL",
        "xx large": "2XL", "xx-large": "2XL", "xxl": "2XL",
        "one size": "OS", "onesize": "OS", "free size": "OS",
    },
    "network": {
        "timeout_seconds": 20,
        "max_retries": 4,
        "backoff_base_seconds": 5,
        "backoff_factor": 2,
        "backoff_jitter_seconds": 3,
        "respect_robots_txt": True,
        "user_agent": "SKYLRK-Personal-Restock-Monitor/1.0 (personal use; 15-min interval; contact: SET_YOUR_EMAIL)",
    },
    "notify": {
        "channel": "console",
        "telegram": {"disable_web_page_preview": False},
        "discord": {"username": "SKYLRK Monitor"},
        "email": {"subject_prefix": "[SKYLRK]"},
    },
    "paths": {
        "state_file": "state/state.json",
        "log_file": "logs/monitor.log",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Optional[str]) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path and Path(path).exists():
        with open(path, "r", encoding="utf-8") as fh:
            user_cfg = yaml.safe_load(fh) or {}
        cfg = _deep_merge(cfg, user_cfg)
    return cfg


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

class _RedactFilter(logging.Filter):
    """Best-effort redaction of anything that looks like a secret from logs."""
    def __init__(self) -> None:
        super().__init__()
        secrets = [
            os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"),
            os.getenv("DISCORD_WEBHOOK_URL"), os.getenv("SMTP_PASS"),
            os.getenv("SMTP_USER"), os.getenv("EMAIL_TO"),
        ]
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self._secrets:
            if s and s in msg:
                msg = msg.replace(s, "***REDACTED***")
        # also mask bot-token patterns and webhook ids that may not be in env
        msg = re.sub(r"(bot)\d{6,}:[A-Za-z0-9_-]{20,}", r"\1***REDACTED***", msg)
        msg = re.sub(r"(discord(?:app)?\.com/api/webhooks/)\d+/[A-Za-z0-9_-]+",
                     r"\1***REDACTED***", msg)
        record.msg = msg
        record.args = ()
        return True


def setup_logging(log_file: str, verbose: bool = False) -> logging.Logger:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("skylrk")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    redact = _RedactFilter()

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.addFilter(redact)
    logger.addHandler(ch)

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(redact)
    logger.addHandler(fh)
    return logger


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stamps(dt: datetime) -> Tuple[str, str]:
    """Return (utc_iso, hkt_iso). Store shows no explicit timezone, so we record
    both UTC and Asia/Hong_Kong per requirements."""
    return dt.isoformat(timespec="seconds"), dt.astimezone(HKT).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# FETCH
# ---------------------------------------------------------------------------

def robots_check(cfg: dict, logger: logging.Logger) -> Tuple[bool, Optional[float]]:
    """Courtesy check of robots.txt. Returns (allowed, crawl_delay_seconds).
    If robots.txt cannot be read, we proceed (return True) and log it."""
    base = cfg["store"]["base_url"].rstrip("/")
    ua = cfg["network"]["user_agent"]
    target = base + cfg["store"]["products_endpoint"]
    rp = RobotFileParser()
    rp.set_url(base + "/robots.txt")
    try:
        rp.read()
    except Exception as exc:  # network/parse issue -> do not hard-block
        logger.warning("Could not read robots.txt (%s); proceeding cautiously.", exc)
        return True, None
    allowed = rp.can_fetch(ua, target)
    try:
        delay = rp.crawl_delay(ua)
    except Exception:
        delay = None
    return allowed, delay


def _get_json_with_retries(session: requests.Session, url: str, cfg: dict,
                           logger: logging.Logger) -> dict:
    net = cfg["network"]
    max_retries = int(net["max_retries"])
    base = float(net["backoff_base_seconds"])
    factor = float(net["backoff_factor"])
    jitter = float(net["backoff_jitter_seconds"])
    timeout = float(net["timeout_seconds"])
    last_exc: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = float(retry_after)
                else:
                    wait = base * (factor ** attempt) + random.uniform(0, jitter)
                logger.warning("HTTP %s on attempt %d; backing off %.1fs",
                               resp.status_code, attempt + 1, wait)
                time.sleep(wait)
                continue
            # Other 4xx: not retryable
            raise RuntimeError(f"Non-retryable HTTP {resp.status_code} for {url}")
        except (requests.ConnectionError, requests.Timeout, json.JSONDecodeError) as exc:
            last_exc = exc
            wait = base * (factor ** attempt) + random.uniform(0, jitter)
            logger.warning("Transient error (%s) attempt %d; backing off %.1fs",
                           type(exc).__name__, attempt + 1, wait)
            time.sleep(wait)
    raise RuntimeError(f"Exhausted retries for {url}: {last_exc}")


def fetch_catalogue(cfg: dict, logger: logging.Logger) -> List[dict]:
    """Fetch all published products via the paginated Shopify JSON endpoint."""
    store = cfg["store"]
    base = store["base_url"].rstrip("/")
    endpoint = store["products_endpoint"]
    page_size = int(store["page_size"])
    max_pages = int(store["max_pages"])

    if cfg["network"].get("respect_robots_txt", True):
        allowed, delay = robots_check(cfg, logger)
        if not allowed:
            raise PermissionError(
                "robots.txt disallows the monitor's user-agent from fetching "
                f"{endpoint}. Stop and use a compliant alternative (see README §J).")
        if delay:
            logger.info("robots.txt advertises Crawl-delay=%.1fs", delay)

    session = requests.Session()
    session.headers.update({
        "User-Agent": cfg["network"]["user_agent"],
        "Accept": "application/json",
    })

    products: List[dict] = []
    for page in range(1, max_pages + 1):
        url = f"{base}{endpoint}?limit={page_size}&page={page}"
        data = _get_json_with_retries(session, url, cfg, logger)
        batch = data.get("products", []) if isinstance(data, dict) else []
        if not batch:
            break
        products.extend(batch)
        logger.debug("Fetched page %d (%d products)", page, len(batch))
        if len(batch) < page_size:
            break
    logger.info("Fetched %d published products total", len(products))
    return products


def load_fixture(path: str) -> List[dict]:
    """Load a local products.json-style fixture (for testing / offline runs)."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("products", []) if isinstance(data, dict) else data


# ---------------------------------------------------------------------------
# NORMALIZE
# ---------------------------------------------------------------------------

BASE_SIZE_MAP = {
    "xxs": "XXS", "xs": "XS", "s": "S", "m": "M", "l": "L",
    "xl": "XL", "xxl": "2XL", "2xl": "2XL", "xxxl": "3XL", "3xl": "3XL", "4xl": "4XL",
    "os": "OS", "onesize": "OS", "freesize": "OS",
}


def _depunct(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.strip().lower())


def normalize_size(raw: Optional[str], cfg: dict) -> Optional[str]:
    if raw is None:
        return None
    merged = {_depunct(k): v for k, v in cfg.get("size_synonyms", {}).items()}
    merged = {**BASE_SIZE_MAP, **merged}
    key = _depunct(raw)
    if key in merged:
        return merged[key]
    return raw.strip().upper() or None


def _option_index(options: List[dict], names: List[str]) -> Optional[int]:
    wanted = {n.strip().lower() for n in names}
    for opt in options:
        if str(opt.get("name", "")).strip().lower() in wanted:
            return int(opt.get("position", 0))
    return None


def _variant_option(variant: dict, index: Optional[int]) -> Optional[str]:
    if not index:
        return None
    return variant.get(f"option{index}")


def _strip_query(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return url.split("?", 1)[0]


def _tags_to_list(tags: Any) -> List[str]:
    if isinstance(tags, list):
        return [str(t).strip() for t in tags]
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    return []


def is_clothing(product: dict, cfg: dict) -> bool:
    f = cfg["clothing_filter"]
    handle = product["handle"]
    ptype = (product.get("product_type") or "").strip().lower()

    if handle in {h.lower() for h in f.get("handle_blocklist", [])}:
        return False
    if ptype and ptype in {b.lower() for b in f.get("product_type_blocklist", [])}:
        return False
    allow = {a.lower() for a in f.get("product_type_allowlist", [])}
    if allow and ptype in allow:
        return True
    if f.get("require_size_option", True):
        allowed_sizes = {normalize_size(x, cfg) for x in f.get("apparel_letter_sizes", [])}
        present = {v["size"] for v in product["variants"].values() if v["size"]}
        return bool(present & allowed_sizes)
    return True


def normalize_product(raw: dict, cfg: dict) -> Optional[dict]:
    """Convert one raw Shopify product to our clean model. Returns None if it is
    not a clothing product per the configured rules."""
    options = raw.get("options", []) or []
    size_idx = _option_index(options, cfg["clothing_filter"]["size_option_names"])
    color_idx = _option_index(options, cfg["clothing_filter"]["color_option_names"])

    variants: Dict[str, dict] = {}
    prices: List[float] = []
    for v in raw.get("variants", []) or []:
        vid = str(v["id"])
        size_raw = _variant_option(v, size_idx)
        color = _variant_option(v, color_idx)
        try:
            price_f = float(v.get("price"))
        except (TypeError, ValueError):
            price_f = 0.0
        prices.append(price_f)
        variants[vid] = {
            "id": vid,
            "size_raw": size_raw,
            "size": normalize_size(size_raw, cfg) if size_raw else None,
            "color": (color.strip() if isinstance(color, str) else color),
            "price": f"{price_f:.2f}",
            "available": bool(v.get("available")),
        }

    images = raw.get("images", []) or []
    primary_image = _strip_query(images[0].get("src")) if images else None
    colors = sorted({v["color"] for v in variants.values() if v["color"]})
    price = f"{min(prices):.2f}" if prices else "0.00"

    product = {
        "id": str(raw["id"]),
        "handle": raw["handle"],
        "title": (raw.get("title") or "").strip(),
        "product_type": (raw.get("product_type") or "").strip(),
        "tags": _tags_to_list(raw.get("tags")),
        "url": f"{cfg['store']['base_url'].rstrip('/')}/products/{raw['handle']}",
        "image": primary_image,
        "price": price,
        "currency": cfg["store"]["currency"],
        "colors": colors,
        "overall_available": any(v["available"] for v in variants.values()),
        "variants": variants,
    }
    return product if is_clothing(product, cfg) else None


def build_snapshot(raw_products: List[dict], cfg: dict) -> Dict[str, dict]:
    snap: Dict[str, dict] = {}
    for raw in raw_products:
        prod = normalize_product(raw, cfg)
        if prod is not None:
            snap[prod["id"]] = prod
    return snap


# ---------------------------------------------------------------------------
# DIFF
# ---------------------------------------------------------------------------

def _short_sig(parts: List[str]) -> str:
    import hashlib
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _make_event(kind: str, product: dict, changed: str, dt: datetime,
                sizes: Optional[List[str]] = None, color: Optional[str] = None,
                signature: str = "") -> dict:
    utc, hkt = stamps(dt)
    return {
        "type": kind,
        "product_id": product["id"],
        "title": product["title"],
        "url": product["url"],
        "image": product.get("image"),
        "price": product.get("price"),
        "currency": product.get("currency"),
        "sizes": sizes or [],
        "color": color,
        "changed": changed,
        "detected_utc": utc,
        "detected_hkt": hkt,
        "signature": signature,
    }


TRACKED_FIELDS = ["title", "price", "colors", "image", "overall_available"]


def _diff_product_fields(old: dict, new: dict) -> Dict[str, Tuple[Any, Any]]:
    changes: Dict[str, Tuple[Any, Any]] = {}
    for field in TRACKED_FIELDS:
        ov, nv = old.get(field), new.get(field)
        if ov != nv:
            changes[field] = (ov, nv)
    return changes


def _describe_changes(changes: Dict[str, Tuple[Any, Any]], currency: str) -> str:
    labels = {
        "title": "Name", "price": "Price", "colors": "Colors",
        "image": "Image", "overall_available": "Stock status",
    }
    lines = []
    for field, (ov, nv) in changes.items():
        if field == "price":
            lines.append(f"{labels[field]}: {currency} {ov} -> {currency} {nv}")
        elif field == "overall_available":
            lines.append(f"{labels[field]}: "
                         f"{'In stock' if ov else 'Sold out'} -> "
                         f"{'In stock' if nv else 'Sold out'}")
        elif field == "colors":
            lines.append(f"{labels[field]}: {', '.join(ov) or '-'} -> {', '.join(nv) or '-'}")
        elif field == "image":
            lines.append("Image updated")
        else:
            lines.append(f"{labels[field]}: {ov!r} -> {nv!r}")
    return "; ".join(lines)


def detect_events(prev: dict, snap: Dict[str, dict], cfg: dict,
                  dt: datetime, logger: logging.Logger) -> List[dict]:
    mon = cfg["monitoring"]
    target = {normalize_size(s, cfg) for s in mon["target_sizes"]}
    prev_products: Dict[str, dict] = prev.get("products", {})
    first_run = len(prev_products) == 0
    events: List[dict] = []

    for pid, prod in snap.items():
        if pid not in prev_products:
            if not first_run and mon["alert_on_new_launch"]:
                avail_target = sorted({v["size"] for v in prod["variants"].values()
                                       if v["size"] in target and v["available"]})
                events.append(_make_event(
                    "NEW_LAUNCH", prod,
                    "New product launched" + (f"; available in {', '.join(avail_target)}"
                                              if avail_target else ""),
                    dt, sizes=avail_target, signature=f"new:{pid}"))
            continue

        old = prev_products[pid]

        # --- XS/S availability / restock, grouped per color ---
        restock_colors_hit = False
        if mon["alert_on_restock"]:
            by_color: Dict[str, set] = {}
            for vid, v in prod["variants"].items():
                if v["size"] in target and v["available"]:
                    oldv = old.get("variants", {}).get(vid)
                    was_unavailable = (oldv is None) or (not oldv.get("available"))
                    if was_unavailable:
                        by_color.setdefault(v["color"] or "", set()).add(v["size"])
            for color, sizes in by_color.items():
                restock_colors_hit = True
                size_list = sorted(sizes)
                sig = f"restock:{pid}:{color}:{'+'.join(size_list)}"
                events.append(_make_event(
                    "XS_S_RESTOCK", prod,
                    f"Now available in {', '.join(size_list)}"
                    + (f" ({color})" if color else ""),
                    dt, sizes=size_list, color=(color or None), signature=sig))

        # --- generic product update ---
        if mon["alert_on_product_update"]:
            changes = _diff_product_fields(old, prod)
            # avoid double-announcing stock flip that a restock already covered
            if restock_colors_hit and "overall_available" in changes:
                ov, nv = changes["overall_available"]
                if (not ov) and nv:  # sold_out -> in_stock, explained by restock
                    del changes["overall_available"]
            if changes:
                sig = "update:" + pid + ":" + _short_sig(
                    [f"{k}={changes[k][1]}" for k in sorted(changes)])
                events.append(_make_event(
                    "PRODUCT_UPDATE", prod,
                    _describe_changes(changes, prod["currency"]),
                    dt, signature=sig))

    # --- removals (optional) ---
    if mon["alert_on_removal"]:
        for pid, old in prev_products.items():
            if pid not in snap:
                events.append(_make_event(
                    "REMOVED", old, "Product no longer listed (unpublished/removed)",
                    dt, signature=f"removal:{pid}"))

    # --- cooldown / duplicate suppression via ledger ---
    ledger: Dict[str, str] = prev.get("ledger", {})
    cooldown = timedelta(hours=float(mon["cooldown_hours"]))
    filtered: List[dict] = []
    for e in events:
        prev_ts = ledger.get(e["signature"])
        if prev_ts:
            try:
                if dt - datetime.fromisoformat(prev_ts) < cooldown:
                    logger.info("Suppressing duplicate within cooldown: %s", e["signature"])
                    continue
            except ValueError:
                pass
        filtered.append(e)

    if first_run:
        logger.info("First run: seeding state with %d clothing products (no alerts).",
                    len(snap))
    return filtered


def update_state(prev: dict, snap: Dict[str, dict], sent_events: List[dict],
                 failed_pids: set, dt: datetime) -> dict:
    """Produce the new state. For products whose alert FAILED to send we keep the
    *old* record so the transition re-triggers next run (at-least-once delivery).
    All other products take the fresh snapshot. First-seen time is preserved."""
    prev_products: Dict[str, dict] = prev.get("products", {})
    ledger: Dict[str, str] = dict(prev.get("ledger", {}))

    for e in sent_events:
        ledger[e["signature"]] = dt.isoformat(timespec="seconds")

    # prune ledger entries older than 30 days
    cutoff = dt - timedelta(days=30)
    for sig in list(ledger.keys()):
        try:
            if datetime.fromisoformat(ledger[sig]) < cutoff:
                del ledger[sig]
        except ValueError:
            del ledger[sig]

    new_products: Dict[str, dict] = {}
    for pid, prod in snap.items():
        if pid in failed_pids and pid in prev_products:
            new_products[pid] = prev_products[pid]  # keep old -> retry next run
            continue
        prod = dict(prod)
        prod["first_seen_utc"] = prev_products.get(pid, {}).get(
            "first_seen_utc", dt.isoformat(timespec="seconds"))
        new_products[pid] = prod

    # keep failed removals so they retry
    for pid in failed_pids:
        if pid not in snap and pid in prev_products:
            new_products[pid] = prev_products[pid]

    utc, hkt = stamps(dt)
    return {
        "schema_version": SCHEMA_VERSION,
        "last_check": {"utc": utc, "hkt": hkt,
                       "status": "error" if failed_pids else "ok",
                       "products_tracked": len(new_products)},
        "products": new_products,
        "ledger": ledger,
    }


# ---------------------------------------------------------------------------
# NOTIFY
# ---------------------------------------------------------------------------

TYPE_LABEL = {
    "NEW_LAUNCH": "NEW LAUNCH",
    "XS_S_RESTOCK": "XS/S RESTOCK",
    "PRODUCT_UPDATE": "PRODUCT UPDATE",
    "REMOVED": "REMOVED",
}


def format_message(e: dict) -> str:
    lines = [f"{TYPE_LABEL.get(e['type'], e['type'])}: {e['title']}"]
    if e.get("sizes"):
        lines.append(f"Available sizes: {', '.join(e['sizes'])}")
    if e.get("color"):
        lines.append(f"Color: {e['color']}")
    if e.get("price"):
        lines.append(f"Price: {e.get('currency','')} {e['price']}")
    lines.append(f"Detected: {e['detected_hkt']} (HKT) / {e['detected_utc']} (UTC)")
    lines.append(f"What changed: {e['changed']}")
    lines.append(f"Product: {e['url']}")
    return "\n".join(lines)


def _need(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def send_console(e: dict, cfg: dict, logger: logging.Logger) -> bool:
    print("\n" + "=" * 60)
    print(format_message(e))
    if e.get("image"):
        print(f"Image: {e['image']}")
    print("=" * 60)
    return True


def _telegram_send_text(token: str, chat_id: str, text: str, cfg: dict,
                        logger: logging.Logger) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text,
               "disable_web_page_preview":
                   cfg["notify"]["telegram"].get("disable_web_page_preview", False)}
    r = requests.post(url, json=payload, timeout=20)
    if r.status_code == 200:
        return True
    logger.error("Telegram sendMessage failed: HTTP %s %s", r.status_code, r.text[:200])
    return False


def send_telegram(e: dict, cfg: dict, logger: logging.Logger) -> bool:
    """Send one alert. If the product photo cannot be delivered we still send the
    text, because a missed restock is far worse than a missing picture: Telegram
    fetches the image URL itself and rejects anything it cannot read as an image
    (dead CDN link, redirect to HTML, oversized file)."""
    token = _need("TELEGRAM_BOT_TOKEN")
    chat_id = _need("TELEGRAM_CHAT_ID")
    text = format_message(e)
    try:
        if e.get("image"):
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            payload = {"chat_id": chat_id, "photo": e["image"], "caption": text[:1024]}
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 200:
                return True
            logger.warning("Telegram sendPhoto failed (HTTP %s %s); "
                           "retrying as text-only so the alert still arrives.",
                           r.status_code, r.text[:200])
            return _telegram_send_text(token, chat_id, text, cfg, logger)
        return _telegram_send_text(token, chat_id, text, cfg, logger)
    except requests.RequestException as exc:
        logger.error("Telegram send error: %s", exc)
        return False


def send_discord(e: dict, cfg: dict, logger: logging.Logger) -> bool:
    webhook = _need("DISCORD_WEBHOOK_URL")
    embed = {
        "title": f"{TYPE_LABEL.get(e['type'], e['type'])}: {e['title']}",
        "url": e["url"],
        "description": e["changed"],
        "fields": [],
        "timestamp": e["detected_utc"],
    }
    if e.get("sizes"):
        embed["fields"].append({"name": "Sizes", "value": ", ".join(e["sizes"]), "inline": True})
    if e.get("color"):
        embed["fields"].append({"name": "Color", "value": e["color"], "inline": True})
    if e.get("price"):
        embed["fields"].append({"name": "Price",
                                "value": f"{e.get('currency','')} {e['price']}", "inline": True})
    if e.get("image"):
        embed["image"] = {"url": e["image"]}
    payload = {"username": cfg["notify"]["discord"].get("username", "SKYLRK Monitor"),
               "embeds": [embed]}
    try:
        r = requests.post(webhook, json=payload, timeout=20)
        if r.status_code in (200, 204):
            return True
        logger.error("Discord send failed: HTTP %s %s", r.status_code, r.text[:200])
        return False
    except requests.RequestException as exc:
        logger.error("Discord send error: %s", exc)
        return False


def send_email(e: dict, cfg: dict, logger: logging.Logger) -> bool:
    host = _need("SMTP_HOST"); port = int(os.getenv("SMTP_PORT", "587"))
    user = _need("SMTP_USER"); password = _need("SMTP_PASS")
    sender = os.getenv("EMAIL_FROM", user); recipient = _need("EMAIL_TO")
    prefix = cfg["notify"]["email"].get("subject_prefix", "[SKYLRK]")
    subject = f"{prefix} {TYPE_LABEL.get(e['type'], e['type'])}: {e['title']}"
    text = format_message(e)
    html = "<pre style='font-family:monospace'>" + text.replace("<", "&lt;") + "</pre>"
    if e.get("image"):
        html += f"<br><img src='{e['image']}' width='320'>"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject; msg["From"] = sender; msg["To"] = recipient
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(sender, [recipient], msg.as_string())
        return True
    except Exception as exc:
        logger.error("Email send error: %s", exc)
        return False


CHANNELS = {
    "console": send_console,
    "telegram": send_telegram,
    "discord": send_discord,
    "email": send_email,
}


def notify_all(events: List[dict], cfg: dict, logger: logging.Logger,
               channel_override: Optional[str] = None) -> set:
    """Send every event. Returns the set of product_ids whose send FAILED."""
    channel = channel_override or cfg["notify"]["channel"]
    sender = CHANNELS.get(channel)
    if sender is None:
        raise ValueError(f"Unknown notify channel: {channel}")
    failed: set = set()
    sent: List[dict] = []
    for e in events:
        ok = sender(e, cfg, logger)
        if ok:
            sent.append(e)
            logger.info("Sent %s alert for %s", e["type"], e["title"])
        else:
            failed.add(e["product_id"])
            logger.error("FAILED to send %s alert for %s", e["type"], e["title"])
    # attach the successfully-sent list to the events for the caller
    for e in events:
        e["_sent"] = e not in [ev for ev in events if ev["product_id"] in failed]
    return failed


# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------

def load_state(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"schema_version": SCHEMA_VERSION, "last_check": {},
                "products": {}, "ledger": {}}
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(path: str, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, p)  # atomic on the same filesystem


# ---------------------------------------------------------------------------
# RUN
# ---------------------------------------------------------------------------

def run_once(cfg: dict, logger: logging.Logger, fixture: Optional[str] = None,
             channel_override: Optional[str] = None, dry_run: bool = False,
             seed_only: bool = False) -> int:
    dt = now_utc()
    utc, hkt = stamps(dt)
    logger.info("=== Check start: %s UTC / %s HKT ===", utc, hkt)

    state_path = cfg["paths"]["state_file"]
    prev = load_state(state_path)

    try:
        raw = load_fixture(fixture) if fixture else fetch_catalogue(cfg, logger)
    except Exception as exc:
        logger.error("Fetch failed: %s", exc)
        # record the failed check without touching product state
        prev.setdefault("last_check", {})
        prev["last_check"].update({"utc": utc, "hkt": hkt, "status": "error",
                                   "error": str(exc)[:300]})
        if not dry_run:
            save_state(state_path, prev)
        return 2

    snap = build_snapshot(raw, cfg)
    logger.info("Clothing products in snapshot: %d", len(snap))

    if seed_only:
        state = update_state(prev, snap, [], set(), dt)
        if not dry_run:
            save_state(state_path, state)
        logger.info("Seeded state with %d products (no alerts).", len(snap))
        return 0

    events = detect_events(prev, snap, cfg, dt, logger)
    logger.info("Events to notify: %d", len(events))

    failed_pids: set = set()
    sent_events: List[dict] = []
    if events and not dry_run:
        failed_pids = notify_all(events, cfg, logger, channel_override)
        sent_events = [e for e in events if e["product_id"] not in failed_pids]
    elif events and dry_run:
        for e in events:
            print(format_message(e)); print("-" * 40)
        sent_events = []  # dry run does not update ledger

    state = update_state(prev, snap, sent_events, failed_pids, dt)
    if not dry_run:
        save_state(state_path, state)
    logger.info("=== Check done. status=%s tracked=%d sent=%d failed=%d ===",
                state["last_check"]["status"], len(state["products"]),
                len(sent_events), len(failed_pids))
    return 0 if not failed_pids else 3


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Monitor SKYLRK clothing for launches and XS/S restocks.")
    ap.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    ap.add_argument("--fixture", help="Use a local products.json file instead of the network")
    ap.add_argument("--channel", help="Override notify channel (console/telegram/discord/email)")
    ap.add_argument("--seed", action="store_true", help="Seed state from current catalogue, send no alerts")
    ap.add_argument("--dry-run", action="store_true", help="Detect and print events but do not send or save")
    ap.add_argument("--verbose", action="store_true", help="Debug logging")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = load_config(args.config)
    logger = setup_logging(cfg["paths"]["log_file"], verbose=args.verbose)
    try:
        return run_once(cfg, logger, fixture=args.fixture,
                        channel_override=args.channel, dry_run=args.dry_run,
                        seed_only=args.seed)
    except PermissionError as exc:
        logger.error("Compliance stop: %s", exc)
        return 4
    except Exception as exc:  # last-resort guard so the scheduler sees a clean exit code
        logger.exception("Unexpected error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
