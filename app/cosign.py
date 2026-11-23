"""Image signature checks with the cosign CLI (labels updock.cosign.*)."""
import base64
import json
import os
import subprocess
import tempfile

KEY_LABEL = "updock.cosign.key"
IDENTITY_LABEL = "updock.cosign.identity"
ISSUER_LABEL = "updock.cosign.issuer"
TIMEOUT = 180


class SignatureError(Exception):
    """The image to deploy has no valid signature (or can't be checked): not deployed."""


def policy_args(labels):
    """cosign verify arguments for a container's labels, or None if it isn't checked."""
    labels = labels or {}
    key = (labels.get(KEY_LABEL) or "").strip()
    ident = (labels.get(IDENTITY_LABEL) or "").strip()
    issuer = (labels.get(ISSUER_LABEL) or "").strip()
    if key:
        return ["--key", key]
    if ident or issuer:
        if not (ident and issuer):
            raise SignatureError(f"keyless signature checks need both {IDENTITY_LABEL} and "
                                 f"{ISSUER_LABEL}")
        return ["--certificate-identity-regexp", ident,
                "--certificate-oidc-issuer-regexp", issuer]
    return None


def run(args, image, credentials=None, data_dir=None, binary=None, runner=subprocess.run):
    """Run `cosign verify ARGS IMAGE`. Raises SignatureError unless it succeeds.

    `credentials` (user, password, host) are handed to cosign in a throwaway docker config
    so private registries work with logins saved in Updock. The sigstore trust root is
    cached in `data_dir` (the root filesystem is read-only)."""
    binary = binary or os.getenv("UPDOCK_COSIGN", "cosign")
    with tempfile.TemporaryDirectory(prefix="updock-cosign-") as tmp:
        env = {"PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": tmp,
               "DOCKER_CONFIG": tmp,
               "TUF_ROOT": os.path.join(data_dir, "sigstore") if data_dir else
               os.path.join(tmp, "sigstore")}
        for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy",
                  "no_proxy", "SSL_CERT_FILE"):
            if os.getenv(k):
                env[k] = os.environ[k]
        if credentials:
            user, password, host = credentials
            auth = base64.b64encode(f"{user}:{password}".encode()).decode()
            with open(os.path.join(tmp, "config.json"), "w") as f:
                json.dump({"auths": {host: {"auth": auth}}}, f)
        try:
            r = runner([binary, "verify", *args, image], capture_output=True, text=True,
                       timeout=TIMEOUT, env=env)
        except FileNotFoundError:
            raise SignatureError(f"cosign is not installed ({binary}); can't check the "
                                 "signature") from None
        except subprocess.TimeoutExpired:
            raise SignatureError(f"cosign took over {TIMEOUT}s checking {image}") from None
    if r.returncode != 0:
        lines = [ln for ln in (r.stderr or r.stdout or "").strip().splitlines() if ln.strip()]
        detail = lines[-1].removeprefix("Error: ") if lines else f"exit code {r.returncode}"
        raise SignatureError(f"signature check failed for {image}: {detail[:300]}")
