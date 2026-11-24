import time
from email import message_from_bytes

from app import email_render as er
from app.notify import EmailNotifier

NOW = time.mktime((2026, 11, 24, 8, 0, 0, 0, 0, -1))


def item(event="update", container="swafarewatcher", msg="x", **details):
    return {"t": NOW, "event": event, "container": container, "msg": msg, "details": details}


def test_ago_and_duration():
    iso = lambda secs: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - secs))
    assert er.ago(iso(30), NOW).endswith("(1 min ago)")
    assert er.ago(iso(3 * 3600), NOW).endswith("(3 hours ago)")
    assert er.ago(iso(3600), NOW).endswith("(1 hour ago)")
    assert er.ago(iso(5 * 86400), NOW).endswith("(5 days ago)")
    assert er.ago(iso(-60), NOW).endswith("(in the future)")
    assert er.ago("2026-11-24T05:00:00", NOW)  # no zone: treated as UTC
    assert er.ago(None) == "" and er.ago("garbage") == ""
    assert er.duration(42.4) == "42 s" and er.duration(125) == "2 min 5 s"


def test_rows_for_an_update():
    r = er.rows(item(image="ghcr.io/me/swa:latest", target_ref="ghcr.io/me/swa:0.20.0",
                     **{"from": {"version": "0.19.0", "id": "c8d45fa4ac3a"},
                        "to": {"version": "0.20.0", "id": "6b4352f3b535",
                               "created": "2026-11-24T12:00:00Z"}},
                     project="swa / web", duration=38, cleanup=True, reason="a\nb"))
    labels = {k: v for k, v, _ in r}
    assert labels["Version"] == "0.19.0 → 0.20.0" and labels["Image ID"] == "c8d45fa4ac3a → 6b4352f3b535"
    assert labels["New tag"] == "ghcr.io/me/swa:0.20.0" and labels["Took"] == "38 s"
    assert labels["Old image"] == "removed" and labels["Compose"] == "swa / web"
    assert "New image built" in labels and "Old image built" not in labels
    html = dict((k, h) for k, _, h in r)
    assert html["Version"] == "0.19.0 → <b>0.20.0</b>" and "pre-wrap" in html["Reason"]
    assert er.rows({**item(), "details": None}) == []
    assert {k for k, _, _ in er.rows(item(cleanup=False, **{"to": {"id": "n"}}))} == {"Image ID", "Old image"}


def test_html_and_text_bodies_escape_and_link():
    items = [item(msg="<b>boom</b>", image="a<b>", notes="https://github.com/me/r/releases",
                  **{"from": {"version": "1"}, "to": {"version": "2"}}),
             item("error", "db", "Docker said <no>", reason="<script>x</script>"),
             item("disk", None, "disk at 91%"),
             item("rollback", "web", "rolled back", notes="javascript:alert(1)", reason="unhealthy")]
    html = er.html_body(items, "qnap & co", "http://nas:18888", "v0.7.0")
    assert "<script>" not in html and "&lt;script&gt;" in html and "a&lt;b&gt;" in html
    assert "qnap &amp; co" in html and "Updock v0.7.0" in html
    assert 'href="https://github.com/me/r/releases"' in html and "javascript:" not in html
    assert 'href="http://nas:18888"' in html and "Open Updock" in html
    assert "1 update" in html and "1 failed" in html  # summary chips for batches
    assert "Docker said &lt;no&gt;" in html  # message shown when there are only few details
    text = er.text_body(items, "qnap", "", None)
    assert "1 disk space" in text and "    Version:         1 → 2" in text
    assert "Open Updock" not in text and text.rstrip().endswith("Sent by Updock on qnap")
    solo = er.html_body([item("test", None, "hello")], "nas", "ftp://x")
    assert "Open Updock" not in solo and "Test email" in solo


def test_email_is_multipart_with_version_in_subject():
    sent = []

    class SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self, **k): pass
        def send_message(self, m): sent.append(m)
    n = EmailNotifier(host="smtp", to=["a@x.io"], smtp=SMTP, instance="qnap", version="v1")
    n.notify("update", "swa", "swa 0.19 -> 0.20", {"to": {"version": "0.20.0"}})
    m = message_from_bytes(sent[0].as_bytes())
    assert m["Subject"] == "[updock] swa: updated to 0.20.0"
    assert m.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in m.get_payload()] == ["text/plain", "text/html"]
    n.notify("rollback", "swa", "rolled back")
    assert sent[1]["Subject"] == "[updock] swa: rolled back"
    assert EmailNotifier.from_env({}, version="v9").version == "v9"
