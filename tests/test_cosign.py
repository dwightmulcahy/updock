import json
import os
import subprocess

import pytest

from app import cosign
from app.cosign import SignatureError, policy_args
from app.updater import Updater
from conftest import IMG_CFG, NEW_IMG, OLD_IMG, make_ctr


def test_policy_args():
    assert policy_args(None) is None and policy_args({"x": "1"}) is None
    assert policy_args({"updock.cosign.key": " /keys/a.pub "}) == ["--key", "/keys/a.pub"]
    assert policy_args({"updock.cosign.identity": "^https://github.com/me/",
                        "updock.cosign.issuer": "https://token.actions.githubusercontent.com"}) == [
        "--certificate-identity-regexp", "^https://github.com/me/",
        "--certificate-oidc-issuer-regexp", "https://token.actions.githubusercontent.com"]
    with pytest.raises(SignatureError, match="both"):
        policy_args({"updock.cosign.identity": "x"})


class Runner:
    def __init__(self, code=0, stderr="", exc=None):
        self.code, self.stderr, self.exc, self.calls = code, stderr, exc, []

    def __call__(self, argv, **kw):
        cfg = os.path.join(kw["env"]["DOCKER_CONFIG"], "config.json")
        self.calls.append({"argv": argv, "env": kw["env"], "timeout": kw["timeout"],
                           "config": json.load(open(cfg)) if os.path.exists(cfg) else None})
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.code, "", self.stderr)


def test_run_success_with_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    r = Runner()
    cosign.run(["--key", "k.pub"], "ghcr.io/me/app@sha256:abc", ("me", "tok", "ghcr.io"),
               data_dir=str(tmp_path), binary="/bin/cosign", runner=r)
    call = r.calls[0]
    assert call["argv"] == ["/bin/cosign", "verify", "--key", "k.pub", "ghcr.io/me/app@sha256:abc"]
    assert call["config"] == {"auths": {"ghcr.io": {"auth": "bWU6dG9r"}}}
    assert call["env"]["TUF_ROOT"] == str(tmp_path / "sigstore")
    assert call["env"]["HTTPS_PROXY"] == "http://proxy:3128" and call["timeout"] == cosign.TIMEOUT


def test_run_failure_reports_last_line(monkeypatch):
    monkeypatch.delenv("UPDOCK_COSIGN", raising=False)
    r = Runner(1, "Verification for x --\nError: no matching signatures: invalid signature\n")
    with pytest.raises(SignatureError, match="x@sha256:1: no matching signatures"):
        cosign.run([], "x@sha256:1", runner=r)
    assert r.calls[0]["argv"][0] == "cosign" and r.calls[0]["config"] is None
    assert r.calls[0]["env"]["TUF_ROOT"].endswith("sigstore")
    with pytest.raises(SignatureError, match="exit code 2"):
        cosign.run([], "x@sha256:1", runner=Runner(2, ""))


def test_run_missing_binary_and_timeout():
    with pytest.raises(SignatureError, match="not installed"):
        cosign.run([], "x", runner=Runner(exc=FileNotFoundError()))
    with pytest.raises(SignatureError, match="took over"):
        cosign.run([], "x", runner=Runner(exc=subprocess.TimeoutExpired("cosign", 1)))


# ---------- in the update flow ----------

def signed_ctr(**labels):
    c = make_ctr()
    c["Config"]["Labels"] = {**c["Config"]["Labels"], **(labels or {"updock.cosign.key": "/k.pub"})}
    return c


@pytest.fixture
def sig(fake, monkeypatch):
    fake.images[NEW_IMG] = {"Id": NEW_IMG, "Config": IMG_CFG,
                            "RepoDigests": ["other/app@sha256:zzz", "test/app@sha256:newd"]}
    fake.tags["test/app:latest"] = NEW_IMG
    seen = {"fail": None, "calls": []}

    def run(args, image, credentials=None, data_dir=None):
        seen["calls"].append((args, image, credentials, data_dir))
        if seen["fail"]:
            raise SignatureError(seen["fail"])
    monkeypatch.setattr(cosign, "run", run)
    monkeypatch.setattr("app.updater.registry_credentials", lambda ref: ("u", "p", "docker.io"))
    return seen


def test_update_verifies_pulled_digest(fake, sig):
    fake.add(signed_ctr())
    u = Updater(fake, cosign_data_dir="/data")
    u.update("web")
    assert sig["calls"] == [(["--key", "/k.pub"], "test/app@sha256:newd", ("u", "p", "docker.io"),
                             "/data")]
    assert u.state["web"]["job"] == "updated" and u.state["web"]["unverified_digest"] is None
    assert any("signature verified" in e["msg"] for e in u.logs)


def test_bad_signature_blocks_update(fake, sig):
    fake.add(signed_ctr())
    sig["fail"] = "signature check failed for test/app@sha256:newd: no signatures"
    u = Updater(fake)
    u.update("web")
    st = u.state["web"]
    assert st["job"] == "unverified" and "no signatures" in st["message"]
    assert st["unverified_digest"] == "sha256:newd" and fake.ctrs["web"]["Image"] == OLD_IMG
    assert "create" not in fake.calls and u.counters["result_unverified"] == 1


def test_scheduled_run_skips_known_bad_digest(fake, sig):
    fake.add(signed_ctr())
    u = Updater(fake)
    u.state["web"].update(unverified_digest="sha256:newd")

    def check(name):
        u._set(name, latest={"digest": "sha256:newd", "id": "new"})
        return fake.inspect(name), True
    u.check = check
    u.update("web", scheduled=True)
    assert u.state["web"]["job"] == "unverified" and sig["calls"] == []
    assert any("signature check failed before" in e["msg"] for e in u.logs)


def test_no_matching_digest(fake, sig):
    fake.images[NEW_IMG]["RepoDigests"] = []
    fake.add(signed_ctr())
    u = Updater(fake)
    u.update("web")
    assert u.state["web"]["job"] == "unverified" and "no registry digest" in u.state["web"]["message"]


def test_unlabelled_and_forced_recreate_skip_check(fake, sig):
    fake.add(make_ctr())
    Updater(fake).update("web")
    fake.ctrs.clear()
    fake.add(signed_ctr(**{"updock.cosign.key": "/k.pub"}))
    fake.tags["test/app:latest"] = OLD_IMG
    Updater(fake).update("web", force=True)  # same image: nothing new to verify
    assert sig["calls"] == []


def test_deploy_verifies(fake, sig):
    fake.add(signed_ctr())
    fake.tags["test/app:2.0"] = NEW_IMG
    sig["fail"] = "bad"
    u = Updater(fake)
    u.deploy("web", "2.0")
    assert u.state["web"]["job"] == "unverified" and sig["calls"][0][1] == "test/app@sha256:newd"
