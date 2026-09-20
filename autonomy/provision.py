#!/usr/bin/env python3
"""Post-payment provision packets for ServerDesk customers.

Given a paid email (+ optional guild id), writes a packet under data/provisions/
with invite URL, /setup steps, and an FAQ tip. Never invites itself to guilds.

Also indexes by Stripe session_id in data/provisions/index.json so the
webhook /thanks page can look up invite + setup without email.

Live Stripe webhook: ../webhook_host/ (Render service serverdesk-webhook).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from paths import (  # noqa: E402
    INVITE_URL_FILE,
    LANDING_URL_FILE,
    PROVISIONS_DIR,
    STRIPE_LINK_FILE,
    ensure_data_dirs,
)

SETUP_STEPS = """
## /setup (admin in Discord)

1. Invite the bot with the URL in this packet (bot + applications.commands).
2. Enable **Server Members Intent** on the Discord Developer Portal if welcome-on-join is needed (already set for ServerDesk#6194 hosted).
3. In your server, run:
   ```
   /setup welcome_channel:#welcome staff_channel:#staff-help staff_role:@Staff
   ```
4. Try `/faq welcome` and `/ask How do I access Week 1?`.
5. Edit FAQ content (hosted: ask Raven; self-host: `config/faq.yaml`).
""".strip()

FAQ_TIP = (
    "Tip: keep `/faq` answers operational (access, schedule, billing email) — "
    "no income or ranking promises. Prefer `/ask` for personal account questions "
    "so staff get a private thread instead of DMs."
)

INDEX_FILENAME = "index.json"

WEBHOOK_STUB_NOTES = """
# Stripe webhook — checkout.session.completed

**Live host:** `../webhook_host/` (Flask + gunicorn). Deploy as Render free web
service `serverdesk-webhook` — see `../webhook_host/README.md` and `render.yaml`.

Autonomy still does **not** run HTTP itself. The webhook service:

1. Exposes `POST /webhooks/stripe`, `GET /health`, `GET /`, and `GET /thanks`.
2. Verifies `Stripe-Signature` with `STRIPE_WEBHOOK_SECRET` via
   `stripe.Webhook.construct_event`. If secret unset, accepts JSON only when
   `DEV_MODE=1` (local/tests).
3. On `checkout.session.completed`:
   - Reads `session.customer_details.email` (or `session.customer_email`).
   - Optional metadata: `guild_id`.
   - Calls the same logic as:
     `python /workspace/serverdesk/autonomy/provision.py --email "$EMAIL" [--guild GUILD_ID] [--session SESSION_ID]`
   - Indexes `session.id` → packet path + email + invite excerpt in
     `data/provisions/index.json` for the customer `/thanks` page.
4. Does **not** auto-DM Discord users. Packet lands on disk for Chief.
5. Optional email: if `RESEND_API_KEY` and `FULFILLMENT_FROM` are set, the
   webhook may email the packet; unset = skip silently (success path never
   requires email).
6. Idempotency: key packets by `session.id` so retries do not duplicate noise.

**Stripe Payment Link Success URL:**

```
https://serverdesk-webhook.onrender.com/thanks?session_id={CHECKOUT_SESSION_ID}
```

Manual / fallback:

```bash
python provision.py --email customer@example.com --session cs_xxx [--guild GUILD_ID]
```
""".strip()


def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return default


def _slug_email(email: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9@.+-]+", "_", email.strip().lower())
    return safe[:80] or "unknown"


def _invite_excerpt(invite: str, max_len: int = 96) -> str:
    invite = (invite or "").strip()
    if len(invite) <= max_len:
        return invite
    return invite[: max_len - 1] + "…"


def index_path_for(out_dir: Path) -> Path:
    return out_dir / INDEX_FILENAME


def load_provision_index(out_dir: Path = PROVISIONS_DIR) -> dict[str, Any]:
    path = index_path_for(out_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def lookup_provision_by_session(
    session_id: str,
    out_dir: Path = PROVISIONS_DIR,
) -> dict[str, Any] | None:
    """Return index entry for session_id, or None."""
    if not session_id:
        return None
    idx = load_provision_index(out_dir)
    entry = idx.get(session_id)
    return entry if isinstance(entry, dict) else None


def update_provision_index(
    session_id: str,
    *,
    packet_md: Path,
    email: str,
    invite_url: str,
    out_dir: Path = PROVISIONS_DIR,
) -> Path:
    """Map session_id → packet md path + email + invite excerpt. Returns index path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = index_path_for(out_dir)
    idx = load_provision_index(out_dir)
    try:
        rel_md = str(packet_md.resolve().relative_to(out_dir.resolve()))
    except ValueError:
        rel_md = str(packet_md)
    idx[session_id] = {
        "packet_md": rel_md,
        "email": email.strip(),
        "invite_url": invite_url.strip(),
        "invite_excerpt": _invite_excerpt(invite_url),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")
    return path


def write_provision_packet(
    email: str,
    guild_id: str | None = None,
    session_id: str | None = None,
    out_dir: Path = PROVISIONS_DIR,
) -> Path:
    if not email or "@" not in email:
        raise ValueError("A valid paid email is required")
    ensure_data_dirs()
    out_dir.mkdir(parents=True, exist_ok=True)

    invite = _read(
        INVITE_URL_FILE,
        "https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot%20applications.commands",
    )
    landing = _read(LANDING_URL_FILE, "")
    stripe = _read(STRIPE_LINK_FILE, "")
    ts = datetime.now(timezone.utc)
    stamp = ts.strftime("%Y%m%dT%H%M%SZ")
    slug = _slug_email(email)
    base = f"{stamp}_{slug}"
    if session_id:
        base = f"{base}_{session_id[:16]}"

    packet: dict[str, Any] = {
        "email": email.strip(),
        "guild_id": guild_id,
        "session_id": session_id,
        "created_at": ts.isoformat(),
        "invite_url": invite,
        "landing_url": landing,
        "stripe_payment_link": stripe,
        "bot": "ServerDesk#6194",
        "setup_steps_markdown": SETUP_STEPS,
        "faq_tip": FAQ_TIP,
        "notes": "Packet for Chief/Raven to deliver manually. No auto-DM.",
    }

    md_path = out_dir / f"{base}.md"
    json_path = out_dir / f"{base}.json"

    md = f"""# ServerDesk provision packet

- **Customer email:** {packet['email']}
- **Guild ID:** {guild_id or '(not provided — ask customer)'}
- **Stripe session:** {session_id or '(manual / not from webhook)'}
- **Created:** {packet['created_at']}
- **Bot:** {packet['bot']}

## Invite URL

```
{invite}
```

{SETUP_STEPS}

## FAQ tip

{FAQ_TIP}

## Links

- Landing: {landing or '(see LANDING_URL.txt)'}
- Billing reference: {stripe or '(see STRIPE_PAYMENT_LINK.txt)'}

---
*Generated by autonomy/provision.py — deliver manually; do not mass-message.*
"""
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")

    if session_id:
        update_provision_index(
            session_id,
            packet_md=md_path,
            email=email,
            invite_url=invite,
            out_dir=out_dir,
        )

    # Keep stub notes adjacent for operators wiring Stripe
    stub_path = out_dir / "STRIPE_WEBHOOK_STUB.md"
    if not stub_path.exists():
        stub_path.write_text(WEBHOOK_STUB_NOTES + "\n", encoding="utf-8")

    return md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write ServerDesk provision packet")
    parser.add_argument("--email", default=None, help="Paid customer email")
    parser.add_argument("--guild", default=None, help="Optional Discord guild/server ID")
    parser.add_argument("--session", default=None, help="Optional Stripe Checkout session id")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=PROVISIONS_DIR,
        help="Output directory for packets",
    )
    parser.add_argument(
        "--write-webhook-stub-only",
        action="store_true",
        help="Only ensure STRIPE_WEBHOOK_STUB.md exists",
    )
    args = parser.parse_args(argv)

    ensure_data_dirs()
    if args.write_webhook_stub_only:
        stub = args.out_dir / "STRIPE_WEBHOOK_STUB.md"
        args.out_dir.mkdir(parents=True, exist_ok=True)
        stub.write_text(WEBHOOK_STUB_NOTES + "\n", encoding="utf-8")
        print(f"Wrote {stub}")
        return 0

    if not args.email:
        parser.error("--email is required unless --write-webhook-stub-only")

    path = write_provision_packet(
        email=args.email,
        guild_id=args.guild,
        session_id=args.session,
        out_dir=args.out_dir,
    )
    print(f"Provision packet → {path}")
    print(f"JSON sidecar → {path.with_suffix('.json')}")
    if args.session:
        print(f"Index → {index_path_for(args.out_dir)} ({args.session})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
