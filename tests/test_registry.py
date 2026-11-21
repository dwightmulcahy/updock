import base64
import json

import httpx
import pytest

from app import registry as reg
from app.registry import Registry, RegistryError, parse_repo, sort_tags


@pytest.mark.parametrize("ref,expected", [
    ("nginx", ("registry-1.docker.io", "library/nginx")),
    ("nginx:1.27", ("registry-1.docker.io", "library/nginx")),
    ("linuxserver/sonarr:latest", ("registry-1.docker.io", "linuxserver/sonarr")),
    ("docker.io/library/redis:7", ("registry-1.docker.io", "library/redis")),
    ("index.docker.io/redis", ("registry-1.docker.io", "library/redis")),
    ("ghcr.io/o/app:v2", ("ghcr.io", "o/app")),
    ("localhost:5000/app", ("localhost:5000", "app")),
    ("quay.io/o/app@sha256:abc", ("quay.io", "o/app")),
])
def test_parse_repo(ref, expected):
    assert parse_repo(ref) == expected


def test_sort_tags():
    tags = ["latest", "1.2.0", "1.10.0", "1.2.0-alpine", "v2.0", "1.2", "edge", "1.2.0"]
    assert sort_tags(tags) == ["v2.0", "1.10.0", "1.2", "1.2.0", "1.2.0-alpine", "edge", "latest"]


def test_sort_tags_caps(monkeypatch):
    monkeypatch.setattr(reg, "MAX_TAGS", 2)
    assert sort_tags(["1", "2", "3"]) == ["3", "2"]


@pytest.fixture
def mk(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    reqs = []

    def build(handler):
        def h(req):
            reqs.append(req)
            return handler(req)
        return Registry(httpx.Client(transport=httpx.MockTransport(h))), reqs
    return build


def test_default_client():
    assert isinstance(Registry().c, httpx.Client)


def test_tags_anonymous_no_auth(mk):
    r, reqs = mk(lambda q: httpx.Response(200, json={"tags": ["1.0", "2.0"]}))
    assert r.tags("localhost:5000/app:1.0") == ["2.0", "1.0"]
    assert reqs[0].url == "https://localhost:5000/v2/app/tags/list?n=1000"


def test_tags_bearer_flow_and_pagination(mk):
    def h(q):
        if q.url.host == "auth.docker.io":
            assert q.url.params["scope"] == "repository:library/nginx:pull"
            assert q.url.params["service"] == "registry.docker.io"
            assert "authorization" not in q.headers
            return httpx.Response(200, json={"token": "T"})
        if q.headers.get("authorization") != "Bearer T":
            return httpx.Response(401, headers={"www-authenticate":
                'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"'})
        if "last" in q.url.params:
            return httpx.Response(200, json={"tags": ["1.0"]})
        return httpx.Response(200, json={"tags": ["latest", "2.0"]},
                              headers={"link": '</v2/library/nginx/tags/list?last=2.0&n=1000>; rel="next"'})
    r, reqs = mk(h)
    assert r.tags("nginx") == ["2.0", "1.0", "latest"]
    assert reqs[-1].url.params["last"] == "2.0"


def test_tags_absolute_next_link_and_page_cap(mk, monkeypatch):
    monkeypatch.setattr(reg, "MAX_PAGES", 2)
    r, reqs = mk(lambda q: httpx.Response(200, json={"tags": None},
                 headers={"link": '<https://ghcr.io/v2/o/app/tags/list?last=x>; rel="next"'}))
    assert r.tags("ghcr.io/o/app") == []
    assert len(reqs) == 2 and reqs[1].url.params["last"] == "x"


def test_tags_bearer_with_credentials_access_token(mk, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(
        {"auths": {"ghcr.io": {"auth": base64.b64encode(b"me:pw").decode()}}}))

    def h(q):
        if q.url.path == "/token":
            assert q.headers["authorization"] == "Basic " + base64.b64encode(b"me:pw").decode()
            assert "service" not in q.url.params
            return httpx.Response(200, json={"access_token": "A"})
        if q.headers.get("authorization") == "Bearer A":
            return httpx.Response(200, json={"tags": ["1"]})
        return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="https://ghcr.io/token"'})
    r, _ = mk(h)
    assert r.tags("ghcr.io/o/app") == ["1"]


def test_tags_basic_challenge(mk, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(
        {"auths": {"reg.local": {"auth": base64.b64encode(b"u:p").decode()}}}))

    def h(q):
        if q.headers.get("authorization", "").startswith("Basic "):
            return httpx.Response(200, json={"tags": ["a"]})
        return httpx.Response(401, headers={"www-authenticate": 'Basic realm="reg"'})
    r, _ = mk(h)
    assert r.tags("reg.local/app") == ["a"]


def test_tags_basic_challenge_without_credentials(mk):
    r, _ = mk(lambda q: httpx.Response(401, headers={"www-authenticate": 'Basic realm="reg"'}))
    with pytest.raises(RegistryError, match="401"):
        r.tags("reg.local/app")


@pytest.mark.parametrize("header", ["Negotiate", 'Bearer service="x"', ""])
def test_tags_unsupported_challenge(mk, header):
    r, _ = mk(lambda q: httpx.Response(401, headers={"www-authenticate": header}))
    with pytest.raises(RegistryError, match="unsupported"):
        r.tags("reg.local/app")


def test_tags_token_request_fails(mk):
    def h(q):
        if q.url.path == "/token":
            return httpx.Response(403)
        return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="https://r.io/token"'})
    r, _ = mk(h)
    with pytest.raises(RegistryError, match="token request failed: 403"):
        r.tags("r.io/app")


def test_tags_not_found(mk):
    r, _ = mk(lambda q: httpx.Response(404))
    with pytest.raises(RegistryError, match="404"):
        r.tags("r.io/app")
