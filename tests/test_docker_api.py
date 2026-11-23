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


# ---------- pull progress ----------

def _stream(*msgs):
    return httpx.Response(200, content=("\n".join(json.dumps(m) for m in msgs) + "\n").encode())


def test_pull_reports_progress(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: _stream(
        {"status": "Pulling from library/x", "id": "latest"},
        {"status": "Pulling fs layer", "id": "a"},
        {"status": "Downloading", "id": "a", "progressDetail": {"current": 25, "total": 100}},
        {"status": "Downloading", "id": "b", "progressDetail": {"current": 0, "total": 100}},
        {"status": "Downloading", "id": "a", "progressDetail": {"current": 25, "total": 100}},
        {"status": "Download complete", "id": "a"},
        {"status": "Downloading", "id": "b", "progressDetail": {"current": 50}},  # no total
        {"status": "Pull complete", "id": "b"},
        {"status": "Already exists", "id": "zzz"},
        {"status": "Digest: sha256:abc"},
    )
    seen = []
    d.pull("x", progress=seen.append)
    assert seen == [25, 12, 50, 100]


def test_pull_progress_zero_total(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: _stream(
        {"status": "Downloading", "id": "a", "progressDetail": {"current": 0, "total": 0}},
        {"status": "Downloading", "id": "a", "progressDetail": {"current": 0, "total": 10}})
    seen = []
    d.pull("x", progress=seen.append)
    assert seen == [0]


# ---------- credential helpers ----------

def _helper(tmp_path, monkeypatch, name, script):
    exe = tmp_path / f"docker-credential-{name}"
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")


def test_cred_helpers_per_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    _write_cfg(tmp_path, {})
    cfg = json.loads((tmp_path / "config.json").read_text())
    cfg["credHelpers"] = {"ghcr.io": "pass"}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    _helper(tmp_path, monkeypatch, "pass",
            'read s; echo "{\\"ServerURL\\":\\"$s\\",\\"Username\\":\\"bot\\",\\"Secret\\":\\"tok\\"}"\n')
    assert docker_api.registry_credentials("ghcr.io/o/app") == ("bot", "tok", "ghcr.io")
    assert docker_api.registry_credentials("quay.io/o/app") is None


def test_creds_store_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"credsStore": "desktop"}))
    _helper(tmp_path, monkeypatch, "desktop", 'echo \'{"Username":"u","Secret":"s"}\'\n')
    assert docker_api.registry_credentials("nginx") == ("u", "s", "https://index.docker.io/v1/")
    assert _decode(registry_auth("nginx"))["password"] == "s"


@pytest.mark.parametrize("script", ["exit 1\n", "echo not-json\n", "echo '{\"Username\":\"u\"}'\n"])
def test_cred_helper_failures(tmp_path, monkeypatch, caplog, script):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"credsStore": "bad"}))
    _helper(tmp_path, monkeypatch, "bad", script)
    assert docker_api.registry_credentials("nginx") is None
    assert "credential helper bad failed" in caplog.records[-1].getMessage()


def test_cred_helper_missing(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"credsStore": "nothere"}))
    assert docker_api.registry_credentials("nginx") is None
    assert "not found on PATH" in caplog.records[-1].getMessage()


# ---------- exec / demux ----------

from app.docker_api import demux  # noqa: E402


def frame(stream, data):
    return bytes([stream, 0, 0, 0]) + len(data).to_bytes(4, "big") + data


def test_demux_frames_and_raw():
    assert demux(frame(1, b"out\n") + frame(2, b"err\n")) == "out\nerr\n"
    assert demux(b"plain tty output") == "plain tty output"
    assert demux(frame(1, b"ok") + b"\x01\x00") == "ok\x01\x00"  # truncated tail kept
    assert demux(b"") == ""


def _exec_handler(start_resp, code=0):
    def h(r):
        if r.url.path.endswith("/exec"):
            assert json.loads(r.content)["Cmd"] == ["sh", "-c", "true"]
            return httpx.Response(201, json={"Id": "e1"})
        if r.url.path == "/exec/e1/start":
            return start_resp(r)
        if r.url.path == "/exec/e1/json":
            return httpx.Response(200, json={"ExitCode": code})
        return httpx.Response(404)
    return h


def test_exec_run(mock_docker):
    d, s = mock_docker
    s["handler"] = _exec_handler(lambda r: httpx.Response(200, content=frame(1, b"hi\n")), 3)
    assert d.exec_run("c1", ["sh", "-c", "true"]) == (3, "hi\n")


def test_exec_run_truncates_output(mock_docker):
    d, s = mock_docker
    s["handler"] = _exec_handler(lambda r: httpx.Response(200, content=iter([b"x" * 12, b"y" * 5])))
    assert d.exec_run("c1", ["sh", "-c", "true"], max_output=10) == (0, "x" * 10)


def test_exec_run_start_error(mock_docker):
    d, s = mock_docker
    s["handler"] = _exec_handler(lambda r: httpx.Response(409, text="not running"))
    with pytest.raises(DockerError, match="not running"):
        d.exec_run("c1", ["sh", "-c", "true"])


def test_exec_run_read_timeout(mock_docker):
    d, s = mock_docker

    def slow(r):
        raise httpx.ReadTimeout("slow", request=r)
    s["handler"] = _exec_handler(slow)
    with pytest.raises(TimeoutError, match="after 5s"):
        d.exec_run("c1", ["sh", "-c", "true"], timeout=5)


def test_exec_run_total_timeout(mock_docker, monkeypatch):
    d, s = mock_docker
    s["handler"] = _exec_handler(lambda r: httpx.Response(200, content=b"tick"))
    ticks = iter([0.0, 100.0])
    monkeypatch.setattr(docker_api.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError, match="after 2s"):
        d.exec_run("c1", ["sh", "-c", "true"], timeout=2)


def test_disk_usage(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(200, json={"LayersSize": 1000, "Images": [
        {"Size": 600, "SharedSize": 100, "Containers": 0},
        {"Size": 300, "SharedSize": -1, "Containers": 1},
        {"Size": 50, "SharedSize": 80, "Containers": -1}]})
    assert d.disk_usage() == {"used": 1000, "reclaimable": 500, "images": 3}
    assert s["requests"][-1].url.params["type"] == "image"
    s["handler"] = lambda r: httpx.Response(200, json={"Images": [{"Size": 5}]})
    assert d.disk_usage()["used"] == 5


def test_ping(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(200, text="OK")
    assert d.ping() is True and s["requests"][-1].url.path == "/_ping"
    s["handler"] = lambda r: httpx.Response(500)
    assert d.ping() is False


# ---------- archives ----------

def test_get_archive(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(200, content=iter([b"ab", b"cd"]))
    assert d.get_archive("c1", "/etc/x", 10) == b"abcd"
    assert s["requests"][-1].url.params["path"] == "/etc/x"
    with pytest.raises(DockerError) as e:
        d.get_archive("c1", "/etc/x", 3)
    assert e.value.status == 413
    s["handler"] = lambda r: httpx.Response(404, text="no such file")
    with pytest.raises(DockerError) as e:
        d.get_archive("c1", "/nope", 10)
    assert e.value.status == 404


def test_put_archive(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(200)
    d.put_archive("c1", "/", b"TAR")
    req = s["requests"][-1]
    assert req.method == "PUT" and req.content == b"TAR"
    assert req.headers["content-type"] == "application/x-tar"
    s["handler"] = lambda r: httpx.Response(500, text="read-only file system")
    with pytest.raises(DockerError, match="read-only"):
        d.put_archive("c1", "/", b"TAR")


# ---------- socket proxy denials ----------

from app.docker_api import proxy_hint  # noqa: E402


@pytest.mark.parametrize("method,path,hint", [
    ("GET", "/containers/json", "enable CONTAINERS=1"),
    ("GET", "/v1.47/images/x/json", "enable IMAGES=1"),
    ("POST", "/containers/abc/exec", "enable CONTAINERS=1, EXEC=1, POST=1"),
    ("POST", "/exec/e1/start", "enable EXEC=1, POST=1"),
    ("POST", "/networks/n/connect", "enable NETWORKS=1, POST=1"),
    ("GET", "/system/df", "enable SYSTEM=1"),
    ("GET", "/_ping", "enable PING=1"),
    ("DELETE", "/volumes/x", "enable POST=1"),
    ("GET", "/info", "blocked by the Docker socket proxy"),
])
def test_proxy_hint(method, path, hint):
    assert proxy_hint(method, path).endswith(hint)


PROXY_403 = "<html><body><h1>403 Forbidden</h1>\nRequest forbidden by administrative rules.\n</body></html>"


def test_proxy_403_explained(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(403, text=PROXY_403)
    with pytest.raises(DockerError) as e:
        d.disk_usage()
    assert e.value.status == 403
    assert str(e.value) == ("403: blocked by the Docker socket proxy; enable SYSTEM=1 "
                            "(GET /system/df)")
    with pytest.raises(DockerError, match="enable CONTAINERS=1, POST=1"):
        d.start("abc")
    with pytest.raises(DockerError, match="enable IMAGES=1, POST=1"):
        d.pull("nginx")


def test_engine_json_403_kept(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(403, text='{"message":"operation not permitted"}')
    with pytest.raises(DockerError, match="operation not permitted"):
        d.inspect("x")


def test_tag_image(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(201)
    d.tag_image("sha256:abc", "ghcr.io/me/app:1.0")
    req = s["requests"][-1]
    assert req.method == "POST" and req.url.raw_path.startswith(b"/images/sha256%3Aabc/tag")
    assert dict(req.url.params) == {"repo": "ghcr.io/me/app", "tag": "1.0"}


def test_version(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(200, json={"Version": "29.0.1"})
    assert d.version() == {"Version": "29.0.1"} and s["requests"][-1].url.path == "/version"


def test_remove_with_volumes(mock_docker):
    d, s = mock_docker
    s["handler"] = lambda r: httpx.Response(204)
    d.remove("abc", volumes=True)
    assert dict(s["requests"][-1].url.params) == {"force": "1", "v": "1"}
    d.remove("abc")
    assert dict(s["requests"][-1].url.params) == {"force": "1", "v": "0"}
