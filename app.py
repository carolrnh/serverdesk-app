"""ServerDesk Stripe webhook host.

POST /webhooks/stripe — verify signature (when secret set) and provision on
checkout.session.completed. GET /health, GET /, GET /thanks for ops + customers.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

app = Flask(__name__)
log = logging.getLogger("serverdesk.webhook")
logging.basicConfig(level=logging.INFO)

LANDING_URL = "https://serverdesk-landing.onrender.com"
WEBHOOK_PUBLIC_URL = "https://serverdesk-webhook.onrender.com"
WEBHOOK_ROOT = Path(__file__).resolve().parent


def _resolve_roots() -> tuple[Path, Path]:
    """Return (serverdesk_root, autonomy_dir) for monorepo or flat deploy zip."""
    colocated = WEBHOOK_ROOT / "autonomy"
    sibling = WEBHOOK_ROOT.parent / "autonomy"
    if (colocated / "provision.py").is_file():
        return WEBHOOK_ROOT, colocated
    if (sibling / "provision.py").is_file():
        return WEBHOOK_ROOT.parent, sibling
    return WEBHOOK_ROOT.parent, sibling


SERVERDESK_ROOT, AUTONOMY_DIR = _resolve_roots()
PROVISION_SCRIPT = AUTONOMY_DIR / "provision.py"


def _dev_mode() -> bool:
    return os.environ.get("DEV_MODE", "").strip() in ("1", "true", "True", "yes")


def _webhook_secret() -> str | None:
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    return secret or None


def _provisions_dir() -> Path:
    override = os.environ.get("PROVISIONS_OUT_DIR", "").strip()
    if override:
        return Path(override)
    return AUTONOMY_DIR / "data" / "provisions"


def _extract_checkout_fields(session: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """email, session_id, optional guild_id from Checkout Session object."""
    email = None
    details = session.get("customer_details") or {}
    if isinstance(details, dict):
        email = details.get("email")
    if not email:
        email = session.get("customer_email")
    session_id = session.get("id")
    meta = session.get("metadata") or {}
    guild_id = meta.get("guild_id") if isinstance(meta, dict) else None
    if guild_id is not None:
        guild_id = str(guild_id).strip() or None
    return (str(email).strip() if email else None, session_id, guild_id)


def _import_provision():
    if not AUTONOMY_DIR.is_dir():
        return None
    if str(AUTONOMY_DIR) not in sys.path:
        sys.path.insert(0, str(AUTONOMY_DIR))
    try:
        import provision as provision_mod  # type: ignore

        return provision_mod
    except Exception as exc:  # noqa: BLE001
        log.warning("import provision failed: %s", exc)
        return None


def run_provision(
    email: str,
    session_id: str | None = None,
    guild_id: str | None = None,
    out_dir: Path | None = None,
) -> Path:
    """Call autonomy provision via import, else subprocess."""
    provision_mod = _import_provision()
    if provision_mod is not None:
        try:
            kwargs: dict[str, Any] = {
                "email": email,
                "guild_id": guild_id,
                "session_id": session_id,
            }
            if out_dir is not None:
                kwargs["out_dir"] = out_dir
            return provision_mod.write_provision_packet(**kwargs)
        except Exception as exc:  # noqa: BLE001 — fall through to CLI
            log.warning("write_provision_packet failed (%s); trying subprocess", exc)

    if not PROVISION_SCRIPT.is_file():
        raise RuntimeError(f"provision script missing: {PROVISION_SCRIPT}")

    cmd = [
        sys.executable,
        str(PROVISION_SCRIPT),
        "--email",
        email,
    ]
    if session_id:
        cmd.extend(["--session", session_id])
    if guild_id:
        cmd.extend(["--guild", guild_id])
    if out_dir is not None:
        cmd.extend(["--out-dir", str(out_dir)])

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"provision.py failed ({proc.returncode}): {proc.stderr or proc.stdout}"
        )
    for line in (proc.stdout or "").splitlines():
        if "→" in line or "->" in line:
            marker = "→" if "→" in line else "->"
            candidate = Path(line.split(marker, 1)[1].strip())
            if candidate.exists():
                return candidate
    return out_dir or _provisions_dir()


def parse_stripe_event(payload: bytes, sig_header: str | None) -> dict[str, Any]:
    """Verify and parse Stripe event, or accept raw JSON in DEV_MODE without secret."""
    secret = _webhook_secret()
    if secret:
        import stripe

        if not sig_header:
            raise ValueError("Missing Stripe-Signature header")
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
        if hasattr(event, "to_dict"):
            return event.to_dict()
        return dict(event)

    if not _dev_mode():
        raise PermissionError(
            "STRIPE_WEBHOOK_SECRET unset; set DEV_MODE=1 to accept unsigned JSON locally"
        )

    data = json.loads(payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else payload)
    if not isinstance(data, dict):
        raise ValueError("Event JSON must be an object")
    return data


def _load_packet_fields(entry: dict[str, Any], out_dir: Path) -> dict[str, str]:
    """Build display fields from index entry + optional JSON/MD sidecar (no secrets)."""
    invite = (entry.get("invite_url") or entry.get("invite_excerpt") or "").strip()
    email = (entry.get("email") or "").strip()
    setup = ""
    faq_tip = ""

    rel = entry.get("packet_md") or ""
    md_path = Path(rel)
    if not md_path.is_absolute():
        md_path = out_dir / rel
    json_path = md_path.with_suffix(".json")

    if json_path.is_file():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                invite = (data.get("invite_url") or invite or "").strip()
                email = (data.get("email") or email or "").strip()
                setup = (data.get("setup_steps_markdown") or "").strip()
                faq_tip = (data.get("faq_tip") or "").strip()
        except (OSError, json.JSONDecodeError):
            pass

    if (not setup or not faq_tip) and md_path.is_file():
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if not invite:
            m = re.search(r"## Invite URL\s*```\s*(https?://[^\s`]+)", text)
            if m:
                invite = m.group(1).strip()
        if not setup:
            m = re.search(r"(## /setup[\s\S]*?)(?=\n## |\n---|\Z)", text)
            if m:
                setup = m.group(1).strip()
        if not faq_tip:
            m = re.search(r"## FAQ tip\s*\n+([\s\S]*?)(?=\n## |\n---|\Z)", text)
            if m:
                faq_tip = m.group(1).strip()

    return {
        "invite_url": invite,
        "email": email,
        "setup_steps_markdown": setup,
        "faq_tip": faq_tip,
        "packet_md": str(md_path) if md_path else "",
    }


def _md_section_to_html(md: str) -> str:
    """Very small markdown→HTML for setup steps (headings, lists, code). No raw secrets."""
    if not md:
        return ""
    lines = md.splitlines()
    out: list[str] = []
    in_code = False
    in_ol = False
    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                if in_ol:
                    out.append("</ol>")
                    in_ol = False
                out.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(html.escape(line))
            continue
        if line.startswith("## "):
            if in_ol:
                out.append("</ol>")
                in_ol = False
            out.append(f"<h2>{html.escape(line[3:].strip())}</h2>")
            continue
        m = re.match(r"^(\d+)\.\s+(.*)$", line)
        if m:
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            safe = html.escape(m.group(2))
            safe = re.sub(r"`([^`]+)`", r"<code>\1</code>", safe)
            safe = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", safe)
            out.append(f"<li>{safe}</li>")
            continue
        if in_ol and not line.strip():
            out.append("</ol>")
            in_ol = False
            continue
        if line.strip():
            if in_ol:
                out.append("</ol>")
                in_ol = False
            safe = html.escape(line)
            safe = re.sub(r"`([^`]+)`", r"<code>\1</code>", safe)
            safe = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", safe)
            out.append(f"<p>{safe}</p>")
    if in_code:
        out.append("</code></pre>")
    if in_ol:
        out.append("</ol>")
    return "\n".join(out)


def _thanks_ready_html(fields: dict[str, str], session_id: str) -> str:
    invite = fields.get("invite_url") or ""
    setup_html = _md_section_to_html(fields.get("setup_steps_markdown") or "")
    faq = fields.get("faq_tip") or ""
    email_disp = fields.get("email") or ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Thanks — ServerDesk</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 40rem;
      margin: 2.5rem auto; padding: 0 1.25rem 3rem; line-height: 1.5; color: #1a1a1a; }}
    @media (prefers-color-scheme: dark) {{
      body {{ color: #f4f4f5; background: #18181b; }}
      a {{ color: #a5b4fc; }}
      code, pre {{ background: #27272a !important; }}
      .card {{ background: #27272a !important; border-color: #3f3f46 !important; }}
    }}
    a {{ color: #5865F2; }}
    h1 {{ font-size: 1.6rem; margin-bottom: 0.35rem; }}
    .muted {{ color: #71717a; font-size: 0.95rem; }}
    .card {{ background: #f4f4f5; border: 1px solid #e4e4e7; border-radius: 10px;
      padding: 1rem 1.1rem; margin: 1.25rem 0; word-break: break-all; }}
    code, pre {{ background: #e4e4e7; padding: 0.1em 0.35em; border-radius: 4px; font-size: 0.9em; }}
    pre {{ padding: 0.75rem 1rem; overflow-x: auto; }}
    pre code {{ padding: 0; background: transparent; }}
    ol {{ padding-left: 1.25rem; }}
    .cta {{ display: inline-block; margin-top: 0.5rem; padding: 0.65rem 1.1rem;
      background: #5865F2; color: #fff !important; text-decoration: none; border-radius: 8px;
      font-weight: 600; }}
    .cta:hover {{ filter: brightness(1.05); }}
  </style>
</head>
<body>
  <h1>You're set — invite ServerDesk</h1>
  <p class="muted">Payment received{(' for ' + html.escape(email_disp)) if email_disp else ''}.
  Use the invite below, then run <code>/setup</code> in Discord.</p>

  <div class="card">
    <strong>Invite URL</strong>
    <p style="margin:0.5rem 0 0"><a href="{html.escape(invite, quote=True)}">{html.escape(invite)}</a></p>
    <p><a class="cta" href="{html.escape(invite, quote=True)}">Add ServerDesk to Discord</a></p>
  </div>

  {setup_html or "<h2>/setup</h2><p>Invite the bot, then run <code>/setup</code> as an admin.</p>"}

  <h2>FAQ tip</h2>
  <p>{html.escape(faq) if faq else "Keep FAQ answers operational — access, schedule, billing. No income promises."}</p>

  <p class="muted">Session <code>{html.escape(session_id)}</code> ·
  <a href="{LANDING_URL}">ServerDesk home</a></p>
</body>
</html>
"""


def _thanks_pending_html(session_id: str) -> str:
    safe = html.escape(session_id or "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <meta http-equiv="refresh" content="3"/>
  <title>Preparing — ServerDesk</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 36rem; margin: 3rem auto;
      padding: 0 1.25rem; line-height: 1.5; color: #1a1a1a; }}
    .muted {{ color: #71717a; }}
    code {{ background: #f4f4f5; padding: 0.1em 0.35em; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>Still preparing your invite…</h1>
  <p>Your payment went through. We're writing your ServerDesk setup packet now.</p>
  <p class="muted">This page refreshes automatically in a few seconds.
  Session: <code>{safe or "(missing)"}</code></p>
</body>
</html>
"""


def maybe_send_fulfillment_email(to_email: str, packet_md_path: Path) -> bool:
    """If RESEND_API_KEY and FULFILLMENT_FROM are set, email packet as simple HTML.

    Returns True if a send was attempted successfully. Never raises to callers
    for missing config — skip silently. Success path must not require email.
    """
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    from_addr = os.environ.get("FULFILLMENT_FROM", "").strip()
    if not api_key or not from_addr:
        return False
    if not to_email or "@" not in to_email:
        log.warning("fulfillment email skipped: invalid recipient")
        return False

    try:
        md = packet_md_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("fulfillment email skipped: cannot read packet (%s)", exc)
        return False

    # Simple HTML: escaped preformatted markdown body (no secrets beyond invite).
    body_html = (
        "<p>Thanks for purchasing ServerDesk. Your invite and setup steps are below.</p>"
        f"<pre style='white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:13px'>"
        f"{html.escape(md)}</pre>"
        f"<p><a href='{WEBHOOK_PUBLIC_URL}/thanks'>Open thank-you page</a> "
        f"(use your Checkout session id) · <a href='{LANDING_URL}'>Landing</a></p>"
    )
    payload = json.dumps(
        {
            "from": from_addr,
            "to": [to_email],
            "subject": "Your ServerDesk invite + setup",
            "html": body_html,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            log.info("fulfillment email sent status=%s to=%s", resp.status, to_email)
            return True
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:500]
        log.warning("fulfillment email HTTP %s: %s", exc.code, err_body)
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("fulfillment email failed: %s", exc)
        return False


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/")
def index():
    html_page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>ServerDesk webhook</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem; color: #1a1a1a; }}
    a {{ color: #5865F2; }}
    code {{ background: #f4f4f5; padding: 0.1em 0.35em; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>ServerDesk webhook</h1>
  <p>Stripe <code>checkout.session.completed</code> → provision packet. Ops only.</p>
  <p>Customer landing: <a href="{LANDING_URL}">{LANDING_URL}</a></p>
  <p>After pay: <code>GET /thanks?session_id={{CHECKOUT_SESSION_ID}}</code></p>
  <p>Health: <a href="/health">/health</a> · Endpoint: <code>POST /webhooks/stripe</code></p>
</body>
</html>
"""
    return html_page, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.get("/thanks")
def thanks():
    """Customer thank-you page keyed by Stripe Checkout Session id."""
    session_id = (request.args.get("session_id") or "").strip()
    out_dir = _provisions_dir()

    entry = None
    provision_mod = _import_provision()
    if provision_mod is not None and session_id:
        try:
            entry = provision_mod.lookup_provision_by_session(session_id, out_dir=out_dir)
        except Exception as exc:  # noqa: BLE001
            log.warning("index lookup failed: %s", exc)
            entry = None
    if entry is None and session_id:
        # Fallback: read index.json directly
        idx_path = out_dir / "index.json"
        if idx_path.is_file():
            try:
                idx = json.loads(idx_path.read_text(encoding="utf-8"))
                if isinstance(idx, dict):
                    raw = idx.get(session_id)
                    if isinstance(raw, dict):
                        entry = raw
            except (OSError, json.JSONDecodeError):
                pass

    if not session_id or not entry:
        page = _thanks_pending_html(session_id)
        return page, 200, {"Content-Type": "text/html; charset=utf-8"}

    fields = _load_packet_fields(entry, out_dir)
    page = _thanks_ready_html(fields, session_id)
    return page, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.post("/webhooks/stripe")
def stripe_webhook():
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature")
    try:
        event = parse_stripe_event(payload, sig)
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # stripe SignatureVerificationError etc.
        log.warning("stripe verify failed: %s", exc)
        return jsonify({"error": "invalid signature or payload"}), 400

    etype = event.get("type")
    if etype != "checkout.session.completed":
        return jsonify({"ok": True, "ignored": etype}), 200

    obj = (event.get("data") or {}).get("object") or {}
    email, session_id, guild_id = _extract_checkout_fields(obj)
    if not email:
        return jsonify({"error": "checkout session missing customer email"}), 400

    out_dir = _provisions_dir()
    try:
        path = run_provision(
            email=email,
            session_id=session_id,
            guild_id=guild_id,
            out_dir=out_dir,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("provision failed")
        return jsonify({"error": f"provision failed: {exc}"}), 500

    emailed = False
    if isinstance(path, Path) and path.suffix == ".md" and path.is_file():
        emailed = maybe_send_fulfillment_email(email, path)

    return jsonify(
        {
            "ok": True,
            "provisioned": True,
            "email": email,
            "session_id": session_id,
            "guild_id": guild_id,
            "packet": str(path),
            "emailed": emailed,
            "thanks": f"/thanks?session_id={session_id}" if session_id else None,
        }
    ), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8790"))
    app.run(host="0.0.0.0", port=port, debug=_dev_mode())
