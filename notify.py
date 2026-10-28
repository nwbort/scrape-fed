#!/usr/bin/env python3
"""
Work out what changed in a file between two scrapes and push it to ntfy.sh.

Messages go to the topic in the NTFY_TOPIC environment variable (on the
server in NTFY_SERVER, default https://ntfy.sh). Without NTFY_TOPIC the
messages are only printed, which is handy when running locally.

Rows have no ids of their own on the portal, so each table is keyed by the
fields that identify a row (a listing's event, date, time and reason, say);
the remaining fields are what can change about it (a listing's outcome, or
the orders made at it).
"""

import json
import os
import urllib.request

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
# ntfy truncates message bodies at 4096 bytes; leave room for the "...".
MAX_MESSAGE_BYTES = 3900

FILE_FIELDS = ("title", "filing_date", "finalised_date")
APPLICATION_FIELDS = ("title", "type", "status", "finalised")


def _event_key(e):
    return (e.get("event_id"), e["date"], e["time"], e["reason"])


def _event_value(e):
    return (
        e.get("presiding"),
        e.get("location"),
        e.get("outcome"),
        tuple(o["url"] for o in e.get("orders", [])),
        tuple(j["citation"] for j in e.get("judgments", [])),
    )


def _document_key(d):
    return (d["date"], d["time"], d["title"], d["filed_by"])


def _party_key(p):
    return (p["role"], p["name"])


def _party_value(p):
    return (p.get("end_date"),)


def describe_event(e: dict) -> str:
    text = f"{e['date']} {e['time']} {e['reason']}"
    if e.get("presiding"):
        text += f" ({e['presiding']})"
    if e.get("outcome"):
        text += f": {e['outcome']}"
    extras = []
    if e.get("orders"):
        extras.append("orders")
    extras += [j["citation"] for j in e.get("judgments", [])]
    if extras:
        text += " [" + ", ".join(extras) + "]"
    return text


def describe_document(d: dict) -> str:
    return f"{d['date']} {d['title']} ({d['filed_by']})"


def describe_party(p: dict) -> str:
    text = f"{p['role']}: {p['name']}"
    if p.get("end_date"):
        text += f" (ended {p['end_date']})"
    return text


def _diff_rows(old, new, key, value=None):
    old_by = {key(r): r for r in old}
    new_by = {key(r): r for r in new}
    added = [r for k, r in new_by.items() if k not in old_by]
    removed = [r for k, r in old_by.items() if k not in new_by]
    changed = (
        [(old_by[k], r) for k, r in new_by.items() if k in old_by and value(old_by[k]) != value(r)]
        if value
        else []
    )
    return added, changed, removed


def diff_file(old: dict, new: dict) -> list[tuple[str, str]]:
    """What changed, as (kind, line) pairs. kind names what the line is
    about ('event', 'document', ...) so the title can count them."""
    out = []
    for f in FILE_FIELDS:
        if old["file"].get(f) != new["file"].get(f):
            out.append(("file", f"~ {f.replace('_', ' ')}: {new['file'].get(f) or '-'} (was {old['file'].get(f) or '-'})"))

    old_apps = {a["id"]: a for a in old["applications"]}
    for app in new["applications"]:
        prev = old_apps.get(app["id"])
        label = f"{app['title']} ({app['type']})" if len(new["applications"]) > 1 else ""
        prefix = f"[{label}] " if label else ""
        if prev is None:
            out.append(("application", f"+ application {app['title']} ({app['type']}), filed {app['filed']}"))
            prev = {"events": [], "documents": [], "parties": []}
        else:
            for f in APPLICATION_FIELDS:
                if prev.get(f) != app.get(f):
                    out.append(("application", f"~ {prefix}{f}: {app.get(f) or '-'} (was {prev.get(f) or '-'})"))

        added, changed, removed = _diff_rows(prev["events"], app["events"], _event_key, _event_value)
        out += [("event", f"+ {prefix}{describe_event(e)}") for e in added]
        out += [("event", f"~ {prefix}{describe_event(e)}") for _, e in changed]
        out += [("event", f"- {prefix}{describe_event(e)}") for e in removed]

        added, _, removed = _diff_rows(prev["documents"], app["documents"], _document_key)
        out += [("document", f"+ {prefix}{describe_document(d)}") for d in added]
        out += [("document", f"- {prefix}{describe_document(d)}") for d in removed]

        added, changed, removed = _diff_rows(prev["parties"], app["parties"], _party_key, _party_value)
        out += [("party", f"+ {prefix}{describe_party(p)}") for p in added]
        out += [("party", f"~ {prefix}{describe_party(p)}") for _, p in changed]
        out += [("party", f"- {prefix}{describe_party(p)}") for p in removed]

    new_ids = {a["id"] for a in new["applications"]}
    out += [
        ("application", f"- application {a['title']} ({a['type']})")
        for a in old["applications"]
        if a["id"] not in new_ids
    ]
    return out


_KIND_WORDS = {
    "file": ("file detail", "file details"),
    "application": ("application change", "application changes"),
    "event": ("listing change", "listing changes"),
    "document": ("document", "documents"),
    "party": ("party change", "party changes"),
}


def title_for(number: str, changes: list[tuple[str, str]]) -> str:
    counts = {}
    for kind, _ in changes:
        counts[kind] = counts.get(kind, 0) + 1
    parts = [
        f"{n} {_KIND_WORDS[k][0 if n == 1 else 1]}"
        for k in _KIND_WORDS
        if (n := counts.get(k))
    ]
    return f"{number}: " + ", ".join(parts)


def truncate(text: str) -> str:
    data = text.encode("utf-8")
    if len(data) <= MAX_MESSAGE_BYTES:
        return text
    return data[:MAX_MESSAGE_BYTES].decode("utf-8", "ignore").rstrip() + "\n..."


def send(title: str, message: str, click: str | None = None, tags=(), priority=3) -> None:
    message = truncate(message)
    print(f"NOTIFY: {title}\n{message}", flush=True)
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("  (NTFY_TOPIC not set, not sending)", flush=True)
        return
    # Publish as JSON rather than with headers so non-ASCII text survives.
    payload = {"topic": topic, "title": title, "message": message, "priority": priority, "tags": list(tags)}
    if click:
        payload["click"] = click
    req = urllib.request.Request(
        NTFY_SERVER,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "scrape-fed (+https://github.com/nwbort/scrape-fed)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except Exception as e:
        print(f"  FAILED to send notification: {e}", flush=True)
