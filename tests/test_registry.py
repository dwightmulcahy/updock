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
                    "revision": "a" * 12, "created": "2026-11-01T00:00:00Z"}
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
