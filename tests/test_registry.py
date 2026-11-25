import base64
import json
from datetime import UTC

import httpx
import pytest

from app import registry as reg
from app.registry import Registry, RegistryAuthError, RegistryError, parse_repo, sort_tags


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
    with pytest.raises(RegistryAuthError, match="reg.local refused access to app"):
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
    with pytest.raises(RegistryAuthError, match="r.io refused access"):
        r.tags("r.io/app")


def test_tags_not_found(mk):
    r, _ = mk(lambda q: httpx.Response(404))
    with pytest.raises(RegistryError, match="404"):
        r.tags("r.io/app")


# ---------- manifests / digests / remote image info ----------

IDX = "application/vnd.oci.image.index.v1+json"
MAN = "application/vnd.oci.image.manifest.v1+json"
CFG = "sha256:" + "c" * 64


def registry_server(index=True, head_digest=True, platforms=None, config=None, blob_status=200):
    platforms = platforms or [{"os": "linux", "architecture": "amd64"},
                              {"os": "linux", "architecture": "arm64", "variant": "v8"},
                              {"os": "linux", "architecture": "arm", "variant": "v7"}]
    config = config if config is not None else {
        "created": "2026-11-01T00:00:00Z",
        "config": {"Labels": {"org.opencontainers.image.version": "2.1.0",
                              "org.opencontainers.image.revision": "a" * 40}}}
    calls = []

    def h(q):
        calls.append((q.method, q.url.path, q.headers.get("accept")))
        if q.url.path.endswith("/manifests/latest"):
            hdr = {"docker-content-digest": "sha256:idx"} if (head_digest or q.method == "GET") else {}
            if q.method == "HEAD":
                return httpx.Response(200, headers=hdr)
            if index:
                return httpx.Response(200, headers=hdr, json={"mediaType": IDX, "manifests": [
                    {"digest": f"sha256:m{i}", "platform": p} for i, p in enumerate(platforms)]})
            return httpx.Response(200, headers=hdr, json={"mediaType": MAN, "config": {"digest": CFG}})
        if "/manifests/sha256:m" in q.url.path:
            return httpx.Response(200, json={"mediaType": MAN, "config": {
                "digest": "sha256:" + q.url.path[-1] * 64}})
        if q.url.path.endswith(f"/blobs/{CFG}") or "/blobs/sha256:" in q.url.path:
            return httpx.Response(blob_status, json=config)
        return httpx.Response(404)
    return h, calls


def test_digest_via_head(mk):
    h, calls = registry_server()
    r, _ = mk(h)
    assert r.digest("ghcr.io/o/app:latest") == "sha256:idx"
    assert calls == [("HEAD", "/v2/o/app/manifests/latest", calls[0][2])]
    assert IDX in calls[0][2] and MAN in calls[0][2]


def test_digest_falls_back_to_get_and_hash(mk):
    r, _ = mk(lambda q: httpx.Response(200, content=b"{}"))
    import hashlib
    assert r.digest("ghcr.io/o/app") == "sha256:" + hashlib.sha256(b"{}").hexdigest()


def test_digest_get_header_used_when_head_lacks_it(mk):
    h, _ = registry_server(head_digest=False)
    r, _ = mk(h)
    assert r.digest("ghcr.io/o/app:latest") == "sha256:idx"


def test_manifest_error(mk):
    r, _ = mk(lambda q: httpx.Response(404))
    with pytest.raises(RegistryError, match="404 for manifest o/app:latest"):
        r.digest("ghcr.io/o/app")


def test_remote_info_index_platform_selection(mk):
    h, calls = registry_server()
    r, _ = mk(h)
    info = r.remote_info("ghcr.io/o/app:latest", {"os": "linux", "architecture": "arm64", "variant": "v8"})
    assert info == {"id": "1" * 12, "digest": "sha256:idx", "tags": [], "version": "2.1.0",
                    "revision": "a" * 12, "created": "2026-11-01T00:00:00Z", "source": None}
    assert ("GET", "/v2/o/app/manifests/sha256:m1", calls[1][2]) in calls


def test_remote_info_default_platform_and_variant_fallback(mk):
    h, _ = registry_server()
    r, _ = mk(h)
    assert r.remote_info("ghcr.io/o/app:latest")["id"] == "0" * 12
    # variant not listed -> any matching arch
    assert r.remote_info("ghcr.io/o/app:latest",
                         {"os": "linux", "architecture": "arm", "variant": "v6"})["id"] == "2" * 12


def test_remote_info_single_manifest_and_fallback_labels(mk):
    h, _ = registry_server(index=False, config={"config": {"Labels": {"build_version": "b9"}}})
    r, _ = mk(h)
    info = r.remote_info("ghcr.io/o/app:latest")
    assert info["id"] == "c" * 12 and info["version"] == "b9" and info["created"] is None


def test_remote_info_missing_platform(mk):
    h, _ = registry_server(platforms=[{"os": "windows", "architecture": "amd64"}, {}])
    r, _ = mk(h)
    with pytest.raises(RegistryError, match="no linux/amd64 image"):
        r.remote_info("ghcr.io/o/app:latest")


def test_remote_info_blob_error(mk):
    h, _ = registry_server(index=False, blob_status=500)
    r, _ = mk(h)
    with pytest.raises(RegistryError, match="500 fetching image config"):
        r.remote_info("ghcr.io/o/app:latest")


def test_remote_info_empty_config(mk):
    h, _ = registry_server(index=False, config={})
    r, _ = mk(h)
    assert r.remote_info("ghcr.io/o/app:latest")["version"] is None


def test_token_cached_across_requests(mk):
    issued = []

    def h(q):
        if q.url.path == "/token":
            issued.append(1)
            return httpx.Response(200, json={"token": "T"})
        if q.headers.get("authorization") != "Bearer T":
            return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="https://r.io/token"'})
        return httpx.Response(200, headers={"docker-content-digest": "sha256:x"})
    r, reqs = mk(h)
    r.digest("r.io/app")
    r.digest("r.io/app")
    assert len(issued) == 1



# ---------- 429 handling ----------

from app.registry import RateLimited, retry_after_seconds  # noqa: E402


@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None), ("120", 120), ("soon", None),
    ("Wed, 21 Oct 2015 07:28:00 GMT", 60),
    ("Wed, 21 Oct 2015 07:28:00", 60),          # no zone: assume UTC
    ("Wed, 21 Oct 2015 07:20:00 GMT", 0),       # in the past
])
def test_retry_after_seconds(value, expected):
    from datetime import datetime
    now = datetime(2015, 10, 21, 7, 27, tzinfo=UTC).timestamp()
    assert retry_after_seconds(value, now) == expected


def test_retry_after_default_now():
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0


def test_rate_limited_message():
    assert str(RateLimited("ghcr.io", 90)) == "rate-limited by ghcr.io; next try in 1m"
    assert str(RateLimited("ghcr.io", None, waiting=True)) == "backing off from ghcr.io"


def test_429_raises_rate_limited(mk):
    r, _ = mk(lambda q: httpx.Response(429, headers={"retry-after": "30"}))
    with pytest.raises(RateLimited) as e:
        r.digest("ghcr.io/o/app")
    assert e.value.host == "ghcr.io" and e.value.retry_after == 30
    with pytest.raises(RateLimited):
        r.tags("ghcr.io/o/app")


def test_429_from_token_endpoint(mk):
    def h(q):
        if q.url.path == "/token":
            return httpx.Response(429)
        return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="https://auth.r.io/token"'})
    r, _ = mk(h)
    with pytest.raises(RateLimited) as e:
        r.digest("r.io/app")
    assert e.value.host == "r.io" and e.value.retry_after is None



# ---------- auth errors & redirects ----------

from app.registry import auth_help  # noqa: E402


@pytest.mark.parametrize("host,expect", [
    ("ghcr.io", "GitHub token with the read:packages scope"),
    ("registry-1.docker.io", "Docker Hub username and an access token"),
    ("docker.io", "Docker Hub username and an access token"),
    ("quay.io", "a username and password/token"),
])
def test_auth_help(host, expect):
    msg = auth_help(host, "o/app")
    shown = "docker.io" if "docker.io" in host else host
    assert msg.startswith(f"{shown} refused access to o/app") and expect in msg
    assert "under Registries in the Updock header" in msg


@pytest.mark.parametrize("path", ["manifests/latest", "tags/list"])
def test_401_after_token_is_auth_error(mk, path):
    def h(q):
        if q.url.path == "/token":
            return httpx.Response(200, json={"token": "anon"})
        return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="https://ghcr.io/token"'})
    r, _ = mk(h)
    with pytest.raises(RegistryAuthError) as e:
        r.tags("ghcr.io/me/private") if path.startswith("tags") else r.digest("ghcr.io/me/private")
    assert e.value.host == "ghcr.io" and e.value.name == "me/private"


def test_blob_403_is_auth_error(mk):
    h, _ = registry_server(index=False, blob_status=403)
    r, _ = mk(h)
    with pytest.raises(RegistryAuthError):
        r.remote_info("ghcr.io/o/app:latest")


def test_cross_registry_blob_redirect_reauthenticates(mk):
    """lscr.io serves the manifest but redirects blobs to ghcr.io; the redirect must get
    its own token instead of arriving without Authorization (the readarr case)."""
    seen = []

    def h(q):
        seen.append((q.url.host, q.url.path, q.headers.get("authorization")))
        if q.url.host == "lscr.io" and "/manifests/" in q.url.path:
            return httpx.Response(200, headers={"docker-content-digest": "sha256:idx"},
                                  json={"mediaType": MAN, "config": {"digest": CFG}})
        if q.url.host == "lscr.io" and "/blobs/" in q.url.path:
            return httpx.Response(307, headers={"location": f"https://ghcr.io/v2/linuxserver/readarr/blobs/{CFG}"})
        if q.url.host == "ghcr.io" and q.url.path == "/token":
            assert q.url.params["scope"] == "repository:linuxserver/readarr:pull"
            return httpx.Response(200, json={"token": "G"})
        if q.url.host == "ghcr.io" and "/blobs/" in q.url.path:
            if q.headers.get("authorization") != "Bearer G":
                return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'})
            return httpx.Response(307, headers={"location": "https://pkg-containers.githubusercontent.com/blob?sig=x"})
        if q.url.host == "pkg-containers.githubusercontent.com":
            assert q.headers.get("authorization") is None  # never leak tokens to the CDN
            return httpx.Response(200, json={"created": "2026-11-20T00:00:00Z", "config": {
                "Labels": {"org.opencontainers.image.version": "0.4.19.2811-ls157"}}})
        return httpx.Response(404)
    r, _ = mk(h)
    info = r.remote_info("lscr.io/linuxserver/readarr:develop")
    assert info["version"] == "0.4.19.2811-ls157" and info["id"] == "c" * 12
    assert ("ghcr.io", f"/v2/linuxserver/readarr/blobs/{CFG}", "Bearer G") in seen


def test_same_host_redirect_keeps_auth_and_hop_limit(mk):
    calls = []

    def h(q):
        calls.append(q.url.path)
        if q.url.path.endswith("/manifests/latest"):
            return httpx.Response(302, headers={"location": "/v2/o/app/manifests/latest?x=1"})
        return httpx.Response(404)
    r, _ = mk(h)
    with pytest.raises(RegistryError, match="too many redirects for o/app"):
        r.digest("ghcr.io/o/app")
    assert len(calls) == 6  # first try + 5 redirects, then give up



def test_cdn_redirect_drops_auth_and_params(mk):
    def h(q):
        if q.url.host == "ghcr.io":
            assert q.url.params.get("n") == "1000"
            return httpx.Response(307, headers={"location": "https://cdn.example/list"})
        assert "n" not in q.url.params and q.headers.get("authorization") is None
        return httpx.Response(200, json={"tags": ["1.0"]})
    r, _ = mk(h)
    r._auth[("ghcr.io", "o/app")] = {"headers": {"Authorization": "Bearer secret"}}
    assert r.tags("ghcr.io/o/app") == ["1.0"]


def test_token_server_error_is_plain_registry_error(mk):
    def h(q):
        if q.url.path == "/token":
            return httpx.Response(502)
        return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="https://r.io/token"'})
    r, _ = mk(h)
    with pytest.raises(RegistryError, match="token request failed: 502") as e:
        r.digest("r.io/app")
    assert not isinstance(e.value, RegistryAuthError)



# ---------- login verification ----------

def _verifier(mk, v2_status=401, challenge='Bearer realm="https://auth.r.io/token",service="r.io"',
              token_status=200, basic_status=200):
    def h(q):
        if q.url.path == "/v2/" and "authorization" not in q.headers:
            return httpx.Response(v2_status, headers={"www-authenticate": challenge})
        if q.url.path == "/v2/":
            return httpx.Response(basic_status)
        assert q.headers["authorization"].startswith("Basic ")
        return httpx.Response(token_status, json={"token": "t"})
    return mk(h)


def test_verify_login_bearer_ok_and_rejected(mk):
    r, reqs = _verifier(mk)
    r.verify_login("r.io", "u", "p")
    assert reqs[-1].url.params["service"] == "r.io"
    r, _ = _verifier(mk, token_status=401)
    with pytest.raises(RegistryAuthError):
        r.verify_login("r.io", "u", "bad")
    r, _ = _verifier(mk, token_status=500)
    with pytest.raises(RegistryError, match="login check failed with status 500"):
        r.verify_login("r.io", "u", "p")


def test_verify_login_bearer_without_service_and_hub_host(mk):
    r, reqs = _verifier(mk, challenge='Bearer realm="https://auth.r.io/token"')
    r.verify_login("https://index.docker.io/v1/", "u", "p")
    assert reqs[0].url.host == "registry-1.docker.io" and "service" not in reqs[-1].url.params


def test_verify_login_basic_and_open_and_odd(mk):
    r, _ = _verifier(mk, challenge='Basic realm="x"', basic_status=403)
    with pytest.raises(RegistryAuthError):
        r.verify_login("reg.local", "u", "bad")
    r, _ = _verifier(mk, challenge='Basic realm="x"')
    r.verify_login("reg.local", "u", "p")
    r, _ = _verifier(mk, v2_status=200)
    r.verify_login("open.local", "u", "p")
    r, _ = _verifier(mk, v2_status=503)
    with pytest.raises(RegistryError, match="unexpected status 503"):
        r.verify_login("down.local", "u", "p")


def test_forget_drops_cached_tokens():
    r = Registry(httpx.Client())
    r._auth = {("ghcr.io", "a"): {}, ("registry-1.docker.io", "b"): {}, ("quay.io", "c"): {}}
    r.forget("ghcr.io")
    r.forget("https://index.docker.io/v1/")
    assert list(r._auth) == [("quay.io", "c")]


# ---- insecure registries, custom CA, identitytoken, Docker Hub tag ordering ----

def test_normalize_hosts():
    assert reg.normalize_hosts([" Registry.LAN:5000 ", "http://10.0.0.5:5000/v2", "", "docker.io"]) \
        == {"registry.lan:5000", "10.0.0.5:5000", "registry-1.docker.io"}
    assert reg.normalize_hosts(None) == set()


def test_insecure_registry_uses_http(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    seen = []

    def h(q):
        seen.append(str(q.url))
        if "/tags/list" in q.url.path:
            return httpx.Response(200, json={"tags": ["1"]},
                                  headers={"link": "</v2/app/tags/list?last=1>; rel=next"}
                                  if "last" not in q.url.params else {})
        if q.url.path == "/v2/":
            return httpx.Response(200)
        return httpx.Response(200, headers={"docker-content-digest": "sha256:d"})
    r = Registry(httpx.Client(transport=httpx.MockTransport(h)), insecure=["registry.lan:5000"])
    assert r.tags("registry.lan:5000/app:1") == ["1"]
    assert r.digest("registry.lan:5000/app:1") == "sha256:d"
    r.verify_login("registry.lan:5000", "u", "p")
    assert all(u.startswith("http://registry.lan:5000/") for u in seen)
    assert seen[1].startswith("http://registry.lan:5000/v2/app/tags/list?last=1")
    assert r.base("ghcr.io") == "https://ghcr.io"


def test_tls_context_loads_ca(tmp_path):
    import ssl
    assert isinstance(reg.tls_context(), ssl.SSLContext)
    with pytest.raises((OSError, ssl.SSLError)):
        reg.tls_context(str(tmp_path / "missing.pem"))
    assert isinstance(Registry(ca_file=None).c, httpx.Client)


def test_identitytoken_exchanged_at_realm(mk, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(
        {"auths": {"me.azurecr.io": {"auth": base64.b64encode(b"00000000:").decode(),
                                     "identitytoken": "REFRESH"}}}))

    def h(q):
        if q.url.path == "/oauth2/token":
            assert q.method == "POST"
            form = dict(x.split("=", 1) for x in q.content.decode().split("&"))
            assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "REFRESH"
            assert form["client_id"] == "docker" and form["service"] == "me.azurecr.io"
            assert form["scope"] == "repository%3Aapp%3Apull"
            assert "authorization" not in q.headers
            return httpx.Response(200, json={"access_token": "ACC"})
        if q.headers.get("authorization") == "Bearer ACC":
            return httpx.Response(200, json={"tags": ["2"]})
        return httpx.Response(401, headers={"www-authenticate":
            'Bearer realm="https://me.azurecr.io/oauth2/token",service="me.azurecr.io"'})
    r, _ = mk(h)
    assert r.tags("me.azurecr.io/app:1") == ["2"]


def test_hub_tags_by_last_push(mk):
    def h(q):
        assert q.url.host == "hub.docker.com"
        if q.url.params.get("page") == "2":
            return httpx.Response(200, json={"results": [{"name": "1.0"}, {}], "next": None})
        assert q.url.params["ordering"] == "last_updated" and q.url.params["page_size"] == "100"
        return httpx.Response(200, json={
            "results": [{"name": "2.0"}, {"name": "latest"}],
            "next": "https://hub.docker.com/v2/repositories/library/nginx/tags?page=2"})
    r, reqs = mk(h)
    assert r.tags("nginx:1.0") == ["2.0", "1.0", "latest"]
    assert reqs[0].url.path == "/v2/repositories/library/nginx/tags" and len(reqs) == 2


def test_hub_tags_foreign_next_link_not_followed(mk):
    r, reqs = mk(lambda q: httpx.Response(200, json={"results": [{"name": "1"}],
                                                     "next": "https://evil.example/x"}))
    assert r.tags("me/app") == ["1"] and len(reqs) == 1


@pytest.mark.parametrize("hub", [httpx.Response(404), httpx.Response(200, json={"results": []}),
                                 httpx.Response(200, text="nope"),
                                 httpx.Response(429, headers={"retry-after": "5"})])
def test_hub_tags_fall_back_to_registry(mk, hub, caplog):
    def h(q):
        if q.url.host == "hub.docker.com":
            return hub
        return httpx.Response(200, json={"tags": ["3"]})
    r, reqs = mk(h)
    assert r.tags("me/private:1") == ["3"]
    assert reqs[-1].url.host == "registry-1.docker.io"
    assert "Docker Hub tag API for me/private failed" in caplog.text


def test_hub_tags_page_cap(mk, monkeypatch):
    monkeypatch.setattr(reg, "MAX_PAGES", 1)
    r, reqs = mk(lambda q: httpx.Response(200, json={
        "results": [{"name": "1"}], "next": "https://hub.docker.com/v2/repositories/a/b/tags?page=2"}))
    assert r.tags("a/b") == ["1"] and len(reqs) == 1
