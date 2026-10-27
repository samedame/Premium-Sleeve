"""Discord alerts.

Alert-only by design: Robinhood has no trading API, so every alert is a
manual instruction to the operator, not an order.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

GREEN, RED, AMBER, GREY = 0x2ECC71, 0xE74C3C, 0xF39C12, 0x95A5A6


def _post(webhook: str, payload: dict, dry_run: bool) -> bool:
    if dry_run or not webhook:
        print("[alert]", json.dumps(payload, indent=2)[:2000])
        return True
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except urllib.error.URLError as exc:
        print(f"[alert] delivery failed: {exc}")
        return False


def entry_alert(webhook, cand, verdict, gate, book, dry_run) -> bool:
    sized = "UNSIZED" if verdict.unsized else f"{verdict.contracts} contract(s)"
    fields = [
        {"name": "Structure",
         "value": (f"Bull put spread\nSell {cand.short.strike:g}P / Buy {cand.long.strike:g}P\n"
                   f"Exp {cand.expiry} ({cand.dte} DTE)"),
         "inline": True},
        {"name": "Deltas",
         "value": f"short {cand.short.delta:+.3f}\nlong {cand.long.delta:+.3f}",
         "inline": True},
        {"name": "Size", "value": sized, "inline": True},
        {"name": "Credit / Risk",
         "value": (f"credit ${cand.credit:.2f}/sh\n"
                   f"max loss ${cand.max_loss_per_contract:,.0f}/ct\n"
                   f"width {cand.width:g}"),
         "inline": True},
        {"name": "Exits",
         "value": "PT 50% of credit\nSL 2x credit\nTime exit 21 DTE",
         "inline": True},
        {"name": "Vega",
         "value": f"{cand.net_vega_per_contract:+.1f}/pt per contract",
         "inline": True},
        {"name": "Gate", "value": gate.reason, "inline": False},
        {"name": "Book after fill",
         "value": (
             "existing book only; this fill adds "
             f"{cand.net_vega_per_contract:+.1f} vega and "
             f"${cand.max_loss_per_contract:,.0f} risk per contract\n"
             f"vega {book.vega:+.1f}/pt · delta {book.delta:+.1f} · risk ${book.risk:,.0f}"
             if verdict.unsized else
             f"vega {book.vega:+.1f}/pt · delta {book.delta:+.1f} · risk ${book.risk:,.0f}"
         ),
         "inline": False},
        {"name": "Sizing notes", "value": "\n".join(verdict.reasons) or "-", "inline": False},
    ]
    return _post(webhook, {"embeds": [{
        "title": "Premium sleeve — ENTRY",
        "description": "Manual execution on Robinhood. This is an alert, not an order.",
        "color": AMBER if verdict.unsized else GREEN,
        "fields": fields,
    }]}, dry_run)


def exit_alert(webhook, pos, debit, reason, pnl, dry_run) -> bool:
    n = pos.contracts
    total = f"${pnl * (n or 0):,.0f}" if n else "unsized"
    return _post(webhook, {"embeds": [{
        "title": "Premium sleeve — EXIT",
        "color": GREEN if pnl >= 0 else RED,
        "fields": [
            {"name": "Position",
             "value": (f"{pos.short_strike:g}P / {pos.long_strike:g}P\n"
                       f"Exp {pos.expiry} · id {pos.id}"),
             "inline": True},
            {"name": "Close at", "value": f"debit ${debit:.2f}/sh", "inline": True},
            {"name": "P/L", "value": f"${pnl:,.0f}/ct · total {total}", "inline": True},
            {"name": "Reason", "value": reason, "inline": False},
        ],
    }]}, dry_run)


def status_alert(webhook, title, lines, ok, dry_run) -> bool:
    return _post(webhook, {"embeds": [{
        "title": f"Premium sleeve — {title}",
        "color": GREY if ok else RED,
        "description": "\n".join(lines)[:3900] or "-",
    }]}, dry_run)
