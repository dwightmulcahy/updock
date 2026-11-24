import logging
import smtplib

import pytest

from app.notify import DEFAULT_EVENTS, EmailNotifier


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls, self.sent = [], []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.calls.append("quit")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, u, p):
        self.calls.append(("login", u, p))

    def send_message(self, msg):
        if getattr(FakeSMTP, "fail", None):
            raise FakeSMTP.fail
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def reset():
    FakeSMTP.instances, FakeSMTP.fail = [], None


def mk(**kw):
    base = dict(host="smtp.example.com", to=["a@x.io", " b@x.io ", ""], user="u", password="p",
                instance="nas", smtp=FakeSMTP, smtp_ssl=FakeSMTP)
    return EmailNotifier(**{**base, **kw})


def test_disabled_without_host_or_recipients():
    n = EmailNotifier(smtp=FakeSMTP)
    assert not n.enabled and n.describe() == {"enabled": False, "to": [], "events": sorted(DEFAULT_EVENTS),
                                              "server": None}
    n.notify("update", "web", "x")
    with n.batch():
        n.notify("update", "web", "x")
    assert FakeSMTP.instances == []
    with pytest.raises(RuntimeError, match="not configured"):
        n.test()


def test_single_event_email_starttls_login():
    n = mk(url="http://nas:8080")
    n.notify("update", "web", "test/app 1 -> 2")
    s = FakeSMTP.instances[0]
    assert (s.host, s.port, s.timeout) == ("smtp.example.com", 587, 20)
    assert s.calls == ["starttls", ("login", "u", "p"), "quit"]
    msg = s.sent[0]
    assert msg["Subject"] == "[updock] web: updated"
    assert msg["To"] == "a@x.io, b@x.io" and msg["From"] == "u"
    body = msg.get_body(("plain",)).get_content()
    assert "Updock on nas" in body and "[updated]  web: test/app 1 -> 2" in body
    assert "Open Updock: http://nas:8080" in body


def test_event_filter():
    n = mk(events=["rollback"])
    n.notify("update", "web", "x")
    assert FakeSMTP.instances == []
    n.notify("rollback", "web", "x")
    assert len(FakeSMTP.instances) == 1


def test_ssl_and_no_auth():
    n = mk(security="SSL", user="", port="465", sender="ops@x.io")
    n.notify("error", "web", "boom")
    s = FakeSMTP.instances[0]
    assert s.port == 465 and s.context is not None and s.calls == ["quit"]
    assert s.sent[0]["From"] == "ops@x.io"


def test_plain_smtp_no_tls():
    n = mk(security="none", user="")
    n.notify("error", None, "boom")
    assert FakeSMTP.instances[0].calls == ["quit"]
    assert FakeSMTP.instances[0].sent[0]["Subject"] == "[updock] nas: failed"


def test_batch_combines_and_nesting():
    n = mk()
    with n.batch():
        n.notify("update", "web", "a")
        with n.batch():
            n.notify("update", "db", "b")
        n.notify("rollback", "app", "c")
        assert FakeSMTP.instances == []
    assert len(FakeSMTP.instances) == 1
    msg = FakeSMTP.instances[0].sent[0]
    assert msg["Subject"] == "[updock] nas: 1 rolled back, 2 updated"
    body = msg.get_body(("plain",)).get_content()
    assert body.index("web: a") < body.index("db: b") < body.index("app: c")


def test_empty_batch_sends_nothing():
    n = mk()
    with n.batch():
        pass
    assert FakeSMTP.instances == []


def test_send_failure_logged_not_raised(caplog):
    FakeSMTP.fail = smtplib.SMTPAuthenticationError(535, b"bad creds")
    n = mk()
    with caplog.at_level(logging.ERROR, logger="updock"):
        n.notify("update", "web", "x")
    rec = caplog.records[-1]
    assert "email to a@x.io, b@x.io failed" in rec.getMessage() and rec.event == "notify"


def test_success_logged(caplog):
    with caplog.at_level(logging.INFO, logger="updock"):
        mk().notify("update", "web", "x")
    assert caplog.records[-1].getMessage() == "emailed a@x.io, b@x.io: [updock] web: updated"


def test_test_email_raises_on_failure():
    FakeSMTP.fail = OSError("connection refused")
    with pytest.raises(OSError):
        mk().test()
    FakeSMTP.fail = None
    mk().test()
    m = FakeSMTP.instances[-1].sent[0]
    assert "Test email from Updock" in m.get_body(("plain",)).get_content() and m["Subject"] == "[updock] nas: test email"
    assert m["Date"] and m["Message-ID"].endswith("@updock>")


def test_from_env_defaults_and_values():
    n = EmailNotifier.from_env({})
    assert not n.enabled and n.port == 587 and n.security == "starttls"
    assert n.events == set(DEFAULT_EVENTS) and n.sender.startswith("updock@")
    n = EmailNotifier.from_env({"UPDOCK_SMTP_HOST": "h", "UPDOCK_SMTP_PORT": "2525",
                                "UPDOCK_SMTP_TO": "a@x, b@x", "UPDOCK_NOTIFY_EVENTS": "update,deploy",
                                "UPDOCK_SMTP_SECURITY": "ssl", "UPDOCK_INSTANCE": "box",
                                "UPDOCK_URL": "http://box"})
    assert n.enabled and n.to == ["a@x", "b@x"] and n.events == {"update", "deploy"}
    assert n.describe() == {"enabled": True, "to": ["a@x", "b@x"], "events": ["deploy", "update"],
                            "server": "h:2525"}
    assert n.instance == "box" and n.url == "http://box"


def test_default_factories_are_smtplib():
    n = EmailNotifier()
    assert n._smtp is smtplib.SMTP and n._smtp_ssl is smtplib.SMTP_SSL
