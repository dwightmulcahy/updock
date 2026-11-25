"""HTML and plain-text bodies for notification emails.

Email clients ignore <style> blocks and most modern CSS, so the HTML uses tables and
inline styles only, and every value is escaped."""
import time
from datetime import UTC, datetime
from html import escape

TITLES = {"update": "updated", "rollback": "rolled back", "error": "failed",
          "available": "update available", "approval": "needs approval", "deploy": "deployed", "delete": "deleted",
          "disk": "disk space", "test": "test email", "unhealthy": "unhealthy",
          "healthy": "healthy again", "self-update": "Updock self-update"}
COLORS = {"update": "#16a34a", "available": "#d97706", "approval": "#ea580c", "error": "#dc2626",
          "rollback": "#dc2626", "deploy": "#7c3aed", "delete": "#6b7280", "disk": "#2563eb",
          "test": "#2563eb", "unhealthy": "#dc2626", "healthy": "#16a34a",
          "self-update": "#7c3aed"}
ICONS = {"update": "✓", "available": "↑", "approval": "⚑", "error": "!", "rollback": "↺", "deploy": "⇄",
         "delete": "✕", "disk": "◔", "test": "✉", "unhealthy": "♥", "healthy": "♥",
         "self-update": "⟳"}
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def title(event):
    return TITLES.get(event, event)


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def _parse(created):
    if not created:
        return None
    try:
        return datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return None


def ago(created, now=None):
    """'2026-11-24 05:12 (2 hours ago)' for an ISO timestamp; '' if unknown."""
    d = _parse(created)
    if d is None:
        return ""
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    secs = (now or time.time()) - d.timestamp()
    if secs < 0:
        rel = "in the future"
    elif secs < 3600:
        rel = f"{max(1, int(secs // 60))} min ago"
    elif secs < 172800:
        rel = f"{int(secs // 3600)} hour{'s' if secs >= 7200 else ''} ago"
    else:
        rel = f"{int(secs // 86400)} days ago"
    return f"{d.astimezone().strftime('%Y-%m-%d %H:%M')} ({rel})"


def duration(secs):
    secs = int(round(secs))
    return f"{secs} s" if secs < 90 else f"{secs // 60} min {secs % 60} s"


def rows(item):
    """[(label, plain value, html value)] describing one notification."""
    d = item.get("details") or {}
    out = []

    def add(label, text, html=None):
        if text:
            out.append((label, text, html if html is not None else escape(text)))
    old, new = d.get("from") or {}, d.get("to") or {}
    add("Image", d.get("image"))
    if d.get("target_ref"):
        add("New tag", d["target_ref"])
    if old.get("version") or new.get("version"):
        a, b = old.get("version") or "?", new.get("version") or "?"
        add("Version", f"{a} → {b}", f"{escape(a)} → <b>{escape(b)}</b>")
    if old.get("id") or new.get("id"):
        a, b = old.get("id") or "?", new.get("id") or "?"
        add("Image ID", f"{a} → {b}",
            f"<code style=\"font-size:12px\">{escape(a)}</code> → "
            f"<code style=\"font-size:12px\">{escape(b)}</code>")
    add("New image built", ago(new.get("created")))
    add("Old image built", ago(old.get("created")))
    add("Compose", d.get("project"))
    if d.get("duration") is not None:
        add("Took", duration(d["duration"]))
    if d.get("cleanup") is not None:
        add("Old image", "removed" if d["cleanup"] else "kept")
    add("Reason", d.get("reason"),
        f"<span style=\"white-space:pre-wrap\">{escape(d['reason'])}</span>" if d.get("reason") else None)
    return out


def summary(items):
    counts = {}
    for i in items:
        counts[i["event"]] = counts.get(i["event"], 0) + 1
    return [(e, n) for e, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]


def text_body(items, instance, url, version=None):
    lines = [f"Updock on {instance}", ""]
    if len(items) > 1:
        lines += [", ".join(f"{n} {title(e)}" for e, n in summary(items)), ""]
    for i in items:
        who = f"{i['container']}: " if i["container"] else ""
        lines.append(f"{_ts(i['t'])}  [{title(i['event'])}]  {who}{i['msg']}")
        for label, text, _ in rows(i):
            if label not in ("Image",):
                lines.append(f"    {label + ':':<16} {text}")
        lines.append("")
    if url:
        lines.append(f"Open Updock: {url}")
    lines.append(f"Sent by Updock{' ' + version if version else ''} on {instance}")
    return "\n".join(lines) + "\n"


def _button(href, label, color="#2563eb"):
    return (f'<a href="{escape(href)}" style="display:inline-block;background:{color};color:#ffffff;'
            f'text-decoration:none;font-weight:600;font-size:13px;padding:8px 14px;border-radius:6px">'
            f'{escape(label)}</a>')


def _safe_url(u):
    return u if isinstance(u, str) and u.lower().startswith(("http://", "https://")) else None


def _card(item):
    ev, color = item["event"], COLORS.get(item["event"], "#6b7280")
    d = item.get("details") or {}
    name = item["container"]
    head = (f'<b style="font-size:16px;color:#111827">{escape(name)}</b> '
            f'<span style="color:{color};font-weight:600">{escape(title(ev))}</span>') if name else \
        f'<b style="font-size:16px;color:{color}">{escape(title(ev).capitalize())}</b>'
    details = rows(item)
    first = item["msg"].split("\n", 1)[0]
    # with structured details the one-line message only repeats them
    msg = "" if details else (f'<div style="color:#374151;font-size:14px;line-height:1.5;'
                              f'white-space:pre-wrap;margin-top:6px">{escape(item["msg"])}</div>')
    if details and ev not in ("update", "available", "deploy"):
        msg = f'<div style="color:#374151;font-size:14px;margin-top:6px">{escape(first)}</div>'
    table = "".join(
        f'<tr><td style="padding:3px 14px 3px 0;color:#6b7280;font-size:13px;white-space:nowrap;'
        f'vertical-align:top">{escape(label)}</td>'
        f'<td style="padding:3px 0;color:#111827;font-size:13px;word-break:break-all">{html}</td></tr>'
        for label, _, html in details)
    notes = _safe_url(d.get("notes"))
    return f'''<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 14px;background:#ffffff;border:1px solid #e5e7eb;border-left:4px solid {color};border-radius:8px">
<tr><td style="padding:14px 16px">
<table role="presentation" cellpadding="0" cellspacing="0"><tr>
<td style="vertical-align:middle;padding-right:10px"><div style="width:26px;height:26px;line-height:26px;border-radius:13px;background:{color};color:#ffffff;text-align:center;font-weight:700;font-size:14px">{ICONS.get(ev, "•")}</div></td>
<td style="vertical-align:middle">{head}<div style="color:#9ca3af;font-size:12px">{escape(_ts(item["t"]))}</div></td>
</tr></table>
{msg}
{f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:10px">{table}</table>' if table else ""}
{f'<div style="margin-top:12px">{_button(notes, "Release notes", "#374151")}</div>' if notes else ""}
</td></tr></table>'''


def html_body(items, instance, url, version=None):
    chips = ""
    if len(items) > 1:
        chips = "".join(
            f'<span style="display:inline-block;margin:0 6px 6px 0;padding:3px 10px;border-radius:12px;'
            f'background:{COLORS.get(e, "#6b7280")};color:#ffffff;font-size:12px;font-weight:600">'
            f'{n} {escape(title(e))}</span>' for e, n in summary(items))
        chips = f'<div style="margin:0 0 12px">{chips}</div>'
    cards = "".join(_card(i) for i in items)
    link = _safe_url(url)
    open_btn = f'<div style="margin:6px 0 18px">{_button(link, "Open Updock")}</div>' if link else ""
    return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:{FONT}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6"><tr><td align="center" style="padding:20px 10px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:620px">
<tr><td style="background:#111827;border-radius:8px 8px 0 0;padding:14px 18px">
<span style="color:#ffffff;font-size:18px;font-weight:700">Updock</span>
<span style="color:#9ca3af;font-size:13px">&nbsp;on {escape(instance)}</span></td></tr>
<tr><td style="background:#f9fafb;border:1px solid #e5e7eb;border-top:0;border-radius:0 0 8px 8px;padding:16px">
{chips}{cards}{open_btn}
<div style="color:#9ca3af;font-size:11px;line-height:1.5">Sent by Updock{escape(' ' + version) if version else ''} on {escape(instance)}. Choose which events are emailed with <code>UPDOCK_NOTIFY_EVENTS</code>.</div>
</td></tr></table></td></tr></table></body></html>
'''
