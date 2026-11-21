import base64
import json

import httpx
import pytest

from app import docker_api
from app.docker_api import Docker, DockerError, registry_auth, registry_of, split_ref


@pytest.mark.parametrize("ref,expected", [
    ("nginx", ("nginx", "latest")),
    ("nginx:1.27", ("nginx", "1.27")),
    ("ghcr.io/o/app:v2", ("ghcr.io/o/app", "v2")),
    ("localhost:5000/app", ("localhost:5000/app", "latest")),
    ("localhost:5000/app:dev", ("localhost:5000/app", "dev")),
    ("nginx@sha256:abc", ("nginx@sha256:abc", None)),
    ("sha256:abc", ("sha256:abc", None)),
])
def test_split_ref(ref, expected):
    assert split_ref(ref) == expected


@pytest.mark.parametrize("repo,expected", [
    ("nginx", "https://index.docker.io/v1/"),
    ("library/nginx", "https://index.docker.io/v1/"),
    ("ghcr.io/o/app", "ghcr.io"),
    ("localhost:5000/app", "localhost:5000"),
    ("localhost/app", "localhost"),
])
def test_registry_of(repo, expected):
    assert registry_of(repo) == expected


def _write_cfg(tmp_path, auths):
    (tmp_path / "config.json").write_text(json.dumps({"auths": auths}))


def _decode(h):
    return json.loads(base64.urlsafe_b64decode(h))


def test_registry_auth_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    assert registry_auth("nginx") is None


def test_registry_auth_bad_json(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    (tmp_path / "config.json").write_text("{nope")
    assert registry_auth("nginx") is None


def test_registry_auth_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    _write_cfg(tmp_path, {"https://index.docker.io/v1/": {"auth": base64.b64encode(b"u:p:w").decode()}})
    assert _decode(registry_auth("nginx")) == {
        "username": "u", "password": "p:w", "serveraddress": "https://index.docker.io/v1/"}


def test_registry_auth_custom_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    _write_cfg(tmp_path, {"ghcr.io": {"auth": base64.b64encode(b"a:b").decode()}})
    assert _decode(registry_auth("ghcr.io/o/app"))["username"] == "a"


def test_registry_auth_hub_without_scheme_key(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    _write_cfg(tmp_path, {"index.docker.io/v1": {"auth": base64.b64encode(b"a:b").decode()}})
    assert registry_auth("nginx") is not None


def test_registry_auth_no_entry_or_helper(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    _write_cfg(tmp_path, {"ghcr.io": {}})
    assert registry_auth("ghcr.io/o/app") is None
    assert registry_auth("quay.io/o/app") is None


def test_init_unix_default(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    d = Docker()
    assert str(d.c.base_url) == "http://docker"


def test_init_tcp(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://10.0.0.1:2375")
    assert str(Docker().c.base_url) == "http://10.0.0.1:2375"


@pytest.fixture
def mock_docker(monkeypatch):
    """Docker() whose HTTP client is backed by a programmable handler."""
    state = {"handler": None, "requests": []}

    def handler(req):
        state["requests"].append(req)
        return state["handler"](req)

    d = Docker()
    d.c = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://docker")
    monkeypatch.setattr(docker_api, "registry_auth", lambda repo: None)
    return d, state


def json_resp(data, status=200):
    return httpx.Response(status, json=data)


def test_containers_inspect_image(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: json_resp({"path": r.url.path})
    assert d.containers() == {"path": "/containers/json"}
    assert d.inspect("web") == {"path": "/containers/web/json"}
    d.image("test/app:latest")
    assert s["requests"][-1].url.raw_path == b"/images/test%2Fapp%3Alatest/json"


def test_req_error_raises(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(404, text="no such container\n")
    with pytest.raises(DockerError) as e:
        d.inspect("x")
    assert e.value.status == 404 and "no such container" in str(e.value)


def test_req_non_json_returns_none(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(204)
    assert d.rename("id", "new") is None
    assert s["requests"][-1].url.params["name"] == "new"


def test_create_connect_remove(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: json_resp({"Id": "newid"}, 201) if "create" in r.url.path else httpx.Response(200)
    assert d.create("web", {"Image": "x"}) == "newid"
    req = s["requests"][-1]
    assert req.url.params["name"] == "web" and json.loads(req.content) == {"Image": "x"}
    d.connect("net2", "newid", {"Aliases": ["a"]})
    assert json.loads(s["requests"][-1].content) == {"Container": "newid", "EndpointConfig": {"Aliases": ["a"]}}
    d.remove("newid")
    assert s["requests"][-1].method == "DELETE" and s["requests"][-1].url.params["force"] == "1"
    d.remove_image("sha256:old")
    assert s["requests"][-1].url.path == "/images/sha256:old"


@pytest.mark.parametrize("code", [204, 304])
def test_stop_start_ok(mock_docker, code):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(code)
    d.stop("id", 7)
    assert s["requests"][-1].url.params["t"] == "7"
    d.start("id")


@pytest.mark.parametrize("method", ["stop", "start"])
def test_stop_start_error(mock_docker, method):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(500, text="boom")
    with pytest.raises(DockerError):
        getattr(d, method)(*(("id", 1) if method == "stop" else ("id",)))


def test_pull_ok_with_auth(mock_docker, monkeypatch):
    d, s = mock_docker
    monkeypatch.setattr(docker_api, "registry_auth", lambda repo: "TOKEN")
    s["handler"] = lambda r: httpx.Response(200, content=b'{"status":"Pulling"}\n\n{"status":"Done"}\n')
    d.pull("ghcr.io/o/app:v2")
    req = s["requests"][-1]
    assert req.url.params["fromImage"] == "ghcr.io/o/app" and req.url.params["tag"] == "v2"
    assert req.headers["X-Registry-Auth"] == "TOKEN"


def test_pull_no_auth_header(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(200, content=b"")
    d.pull("nginx")
    assert "X-Registry-Auth" not in s["requests"][-1].headers


def test_pull_http_error(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(404, text="not found")
    with pytest.raises(DockerError, match="not found"):
        d.pull("nginx")


def test_pull_stream_error(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(200, content=b'{"status":"x"}\n{"error":"denied"}\n')
    with pytest.raises(DockerError, match="denied"):
        d.pull("nginx")


def test_restart(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(204)
    d.restart("id", 5)
    assert s["requests"][-1].url.path == "/containers/id/restart"
    assert s["requests"][-1].url.params["t"] == "5"
    s["handler"] = lambda r: httpx.Response(404, text="gone")
    with pytest.raises(DockerError):
        d.restart("id")
