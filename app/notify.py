"""Email notifications. Events raised during a scheduled run are batched into one email."""
import logging
import smtplib
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .email_render import html_body, text_body, title

log = logging.getLogger("updock")

ALL_EVENTS = ("update", "rollback", "error", "available", "approval", "deploy", "disk", "delete")
DEFAULT_EVENTS = ("update", "rollback", "error", "available", "approval", "disk")


class EmailNotifier:
    def __init__(self, host="", port=587, user="", password="", sender="", to=(),
                 security="starttls", events=DEFAULT_EVENTS, instance=None, url="",
                 timeout=20, smtp=smtplib.SMTP, smtp_ssl=smtplib.SMTP_SSL, version=None):
        self.host, self.port, self.user, self.password = host, int(port), user, password
        self.to = [t.strip() for t in to if t.strip()]
        self.sender = sender or user or f"updock@{socket.gethostname()}"
        self.security = (security or "starttls").lower()
        self.events = {e.strip() for e in events if e.strip()}
        self.instance = instance or socket.gethostname()
        self.url = url
        self.timeout = timeout
        self.version = version
        self._smtp, self._smtp_ssl = smtp, smtp_ssl
        self._local = threading.local()

    @classmethod
    def from_env(cls, env, version=None):
        split = lambda v: [x for x in (v or "").split(",") if x.strip()]
        return cls(host=env.get("UPDOCK_SMTP_HOST", ""), port=env.get("UPDOCK_SMTP_PORT", "587"),
                   user=env.get("UPDOCK_SMTP_USER", ""), password=env.get("UPDOCK_SMTP_PASSWORD", ""),
                   sender=env.get("UPDOCK_SMTP_FROM", ""), to=split(env.get("UPDOCK_SMTP_TO")),
                   security=env.get("UPDOCK_SMTP_SECURITY", "starttls"),
                   events=split(env.get("UPDOCK_NOTIFY_EVENTS")) or DEFAULT_EVENTS,
                   instance=env.get("UPDOCK_INSTANCE") or None, url=env.get("UPDOCK_URL", ""),
                   version=version)

    @property
    def enabled(self):
        return bool(self.host and self.to)

    def describe(self):
        return {"enabled": self.enabled, "to": self.to, "events": sorted(self.events),
                "server": f"{self.host}:{self.port}" if self.host else None}

    def notify(self, event, container, message, details=None):
        """`details`: optional structured info shown in the email (see email_render.rows)."""
        if not self.enabled or event not in self.events:
            return
        item = {"t": time.time(), "event": event, "container": container, "msg": message,
                "details": details or {}}
        batch = getattr(self._local, "batch", None)
        if batch is not None:
            batch.append(item)
        else:
            self.send([item])

    @contextmanager
    def batch(self):
        """Collect notifications raised in this thread and send them as one email."""
        outer = getattr(self._local, "batch", None)
        if outer is not None:  # nested: the outer batch sends
            yield
            return
        self._local.batch = items = []
        try:
            yield
        finally:
            self._local.batch = None
            if items:
                self.send(items)

    def compose(self, items):
        counts = {}
        for i in items:
            counts[i["event"]] = counts.get(i["event"], 0) + 1
        if len(items) == 1:
            i = items[0]
            subject = f"[updock] {i['container'] or self.instance}: {title(i['event'])}"
            ver = ((i.get("details") or {}).get("to") or {}).get("version")
            if i["event"] in ("update", "available", "approval") and ver:
                subject += f" to {ver}"
        else:
            parts = [f"{n} {title(e)}" for e, n in sorted(counts.items())]
            subject = f"[updock] {self.instance}: " + ", ".join(parts)
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, self.sender, ", ".join(self.to)
        msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid(domain="updock")
        msg.set_content(text_body(items, self.instance, self.url, self.version))
        msg.add_alternative(html_body(items, self.instance, self.url, self.version), subtype="html")
        return msg

    def _deliver(self, msg):
        ctx = ssl.create_default_context()
        if self.security == "ssl":
            server = self._smtp_ssl(self.host, self.port, timeout=self.timeout, context=ctx)
        else:
            server = self._smtp(self.host, self.port, timeout=self.timeout)
        with server:
            if self.security == "starttls":
                server.starttls(context=ctx)
            if self.user:
                server.login(self.user, self.password)
            server.send_message(msg)

    def send(self, items, raise_errors=False):
        msg = self.compose(items)
        try:
            self._deliver(msg)
        except Exception as e:
            log.error("email to %s failed: %s", ", ".join(self.to), e, extra={"event": "notify"})
            if raise_errors:
                raise
            return False
        log.info("emailed %s: %s", ", ".join(self.to), msg["Subject"], extra={"event": "notify"})
        return True

    def test(self):
        if not self.enabled:
            raise RuntimeError("email is not configured (set UPDOCK_SMTP_HOST and UPDOCK_SMTP_TO)")
        self.send([{"t": time.time(), "event": "test", "container": None,
                    "msg": "Test email from Updock. Notifications are working."}],
                  raise_errors=True)
