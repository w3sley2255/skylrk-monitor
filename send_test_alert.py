#!/usr/bin/env python3
"""Send one sample alert through the configured channel, then exit.

Verifies the notification path (token, chat id, image upload, formatting)
without waiting for a real restock and without touching state/state.json.

    python send_test_alert.py                # uses notify.channel from config.yaml
    python send_test_alert.py --channel console

Exit codes: 0 sent, 1 send failed, 2 misconfigured (e.g. missing secret).
"""
from __future__ import annotations

import argparse
import json
import sys

from skylrk_monitor import (
    CHANNELS,
    load_config,
    now_utc,
    setup_logging,
    stamps,
)

def build_sample_event(cfg: dict) -> dict:
    """An event shaped exactly like a real XS_S_RESTOCK, so the message you receive
    is byte-for-byte what a genuine alert looks like.

    Details are borrowed from a real product in the seeded state, so the image URL
    is one the store actually serves. A made-up URL is not a valid test: Telegram
    fetches the image itself and rejects anything that is not really an image.
    """
    utc, hkt = stamps(now_utc())
    event = {
        "type": "XS_S_RESTOCK",
        "product_id": "test",
        "title": "[TEST ALERT - not a real restock]",
        "url": "https://skylrk.com",
        "image": None,
        "price": "0.00",
        "currency": cfg["store"]["currency"],
        "sizes": ["XS"],
        "color": None,
        "changed": "This is a test of the notification channel",
        "detected_utc": utc,
        "detected_hkt": hkt,
        "signature": "test",
    }

    try:
        with open(cfg["paths"]["state_file"], "r", encoding="utf-8") as fh:
            products = json.load(fh).get("products", {})
    except (OSError, ValueError):
        return event  # unseeded state is fine - send the text-only version

    sample = next((p for p in products.values() if p.get("image")), None)
    if sample:
        event.update({
            "title": f"{sample['title']}  [TEST ALERT - not a real restock]",
            "url": sample["url"],
            "image": sample["image"],
            "price": sample["price"],
            "currency": sample["currency"],
            "color": (sample.get("colors") or [None])[0],
            "changed": "Test alert - no actual stock change",
        })
    return event


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--channel", help="Override notify channel for this test")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    logger = setup_logging(cfg["paths"]["log_file"])
    channel = args.channel or cfg["notify"]["channel"]

    sender = CHANNELS.get(channel)
    if sender is None:
        print(f"Unknown channel {channel!r}. Choose one of: {', '.join(CHANNELS)}",
              file=sys.stderr)
        return 2

    print(f"Sending a test alert via '{channel}' ...")
    try:
        ok = sender(build_sample_event(cfg), cfg, logger)
    except RuntimeError as exc:
        # _need() raises this when a secret is absent - the most common setup slip.
        print(f"\nNOT SENT: {exc}\n"
              f"Fill in the blank value in .env (see SETUP-TELEGRAM.md step 4).",
              file=sys.stderr)
        return 2

    if ok:
        print(f"\nSent. Check {channel} - the message should already be there.\n"
              "State was not modified; this was a standalone test.")
        return 0
    print("\nSend FAILED. The error above (and logs/monitor.log) has the reason; "
          "SETUP-TELEGRAM.md step 5 lists the common causes.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
