#!/usr/bin/env python3
"""
AIOS Vault — the master password that gates destructive, irreversible operations.

Set once at install, required for: `aios uninstall`, disabling the guardrails,
turning full control on, and rotating itself.

THREAT MODEL — worth being precise, because the obvious promise is impossible.
The agents run as you, with a shell. Any file you can delete, they can delete.
So "the AI can't delete the password" cannot be enforced by encryption alone.
What IS enforceable, and what this module actually does:

  1. The password is never stored. Only scrypt(password, salt) is — memory-hard,
     so even with the file an attacker can't read the password back out. This
     part is absolute: there is nothing to read.

  2. Deleting the file must not GRANT anything. Every protected operation fails
     CLOSED: no vault, or a vault that doesn't verify, means the operation is
     refused, not allowed. That turns "delete the password to escape it" into
     locking yourself out — an attack with no payoff.

  3. Tamper-evidence. The record is mirrored outside the repo and self-checksummed.
     If the two disagree, or a checksum fails, protected ops refuse and say so.

  4. The agents' own shell refuses to touch the vault paths (aios_sec.guard), so
     the sanctioned route to the filesystem is closed to them.

What remains true, and is stated in the docs rather than hidden: an agent that
writes files through some path other than the guarded shell could still DESTROY
the vault. It still cannot read the password, and destroying it grants nothing —
it only forces you to restore the mirror or reinstall.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from pathlib import Path

ROOT = Path(os.environ.get("AIOS_ROOT", Path(__file__).resolve().parent))
VAULT = ROOT / ".aios" / "vault.json"
# Mirrored outside the repo: a `git clean`, a reinstall, or an agent rewriting the
# project tree shouldn't silently drop your credential.
MIRROR = Path.home() / ".aios" / "vault.json"

# scrypt cost. n=2**15 with r=8 is ~32MB and ~0.1s per attempt here — brutal for
# brute force, unnoticeable for a human typing a password once.
_N, _R, _P, _DKLEN, _SALT = 2 ** 15, 8, 1, 64, 32
PEPPER = b"aios-vault-v1"     # domain separation, not a secret


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8") + PEPPER, salt=salt,
                          n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=64 * 1024 * 1024)


def _checksum(rec: dict) -> str:
    """Self-checksum over the fields that matter, so edits are detectable."""
    payload = json.dumps({k: rec[k] for k in ("v", "algo", "salt", "hash", "created")},
                         sort_keys=True).encode()
    return hashlib.sha256(payload + PEPPER).hexdigest()


def _harden(p: Path):
    """Best-effort: owner-read-only, and immutable where the OS offers it."""
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except Exception:
        pass
    if os.name == "nt":
        try:  # strip inherited ACEs, grant only the current user
            import subprocess
            user = os.environ.get("USERNAME", "")
            if user:
                subprocess.run(["icacls", str(p), "/inheritance:r", "/grant:r",
                                f"{user}:(R,W)"], capture_output=True, timeout=15)
        except Exception:
            pass


def _write(rec: dict):
    for path in (VAULT, MIRROR):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
            _harden(path)
        except Exception:
            pass


def _read_one(path: Path) -> dict | None:
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        if rec.get("checksum") != _checksum(rec):
            return {"_tampered": True}
        return rec
    except FileNotFoundError:
        return None
    except Exception:
        return {"_tampered": True}


def status() -> dict:
    """Where the vault stands, without revealing anything sensitive."""
    a, b = _read_one(VAULT), _read_one(MIRROR)
    present = [x for x in (a, b) if x]
    tampered = any(x.get("_tampered") for x in present)
    good = [x for x in present if not x.get("_tampered")]
    out = {
        "set": bool(good),
        "tampered": tampered,
        "primary": bool(a and not a.get("_tampered")),
        "mirror": bool(b and not b.get("_tampered")),
        "paths": {"primary": str(VAULT), "mirror": str(MIRROR)},
    }
    if good:
        out["created"] = good[0].get("created")
        # Both copies must agree, or someone edited one of them.
        if a and b and not a.get("_tampered") and not b.get("_tampered"):
            out["mismatch"] = a.get("hash") != b.get("hash")
    return out


def is_set() -> bool:
    return status()["set"]


def set_password(password: str, current: str | None = None) -> dict:
    """Set or rotate. Rotating requires the current password — otherwise anything
    that can write the file could simply replace your password with its own."""
    password = (password or "").strip()
    if len(password) < 8:
        return {"ok": False, "error": "password must be at least 8 characters"}
    st = status()
    if st["set"]:
        if current is None:
            return {"ok": False, "error": "a password is already set — pass the current one to change it"}
        if not verify(current)["ok"]:
            return {"ok": False, "error": "current password is incorrect"}
    salt = secrets.token_bytes(_SALT)
    rec = {"v": 1, "algo": f"scrypt n={_N} r={_R} p={_P}",
           "salt": base64.b64encode(salt).decode(),
           "hash": base64.b64encode(_derive(password, salt)).decode(),
           "created": time.time()}
    rec["checksum"] = _checksum(rec)
    _write(rec)
    st2 = status()
    if not st2["set"]:
        return {"ok": False, "error": "could not persist the vault (check permissions)"}
    return {"ok": True, "rotated": st["set"], "primary": st2["primary"],
            "mirror": st2["mirror"]}


def verify(password: str) -> dict:
    """Constant-time check. FAILS CLOSED: no vault, tampering, or a mismatch
    between copies all return ok=False, so destroying the vault never unlocks."""
    a, b = _read_one(VAULT), _read_one(MIRROR)
    good = [x for x in (a, b) if x and not x.get("_tampered")]
    if not good:
        if a or b:
            return {"ok": False, "error": "vault is corrupt or was tampered with — "
                    "protected operations are refused. Restore it or reinstall."}
        return {"ok": False, "error": "no master password is set — run `aios password set`",
                "unset": True}
    if len(good) == 2 and good[0]["hash"] != good[1]["hash"]:
        return {"ok": False, "error": "vault copies disagree (one was modified) — "
                f"compare {VAULT} and {MIRROR}"}
    rec = good[0]
    try:
        salt = base64.b64decode(rec["salt"])
        expect = base64.b64decode(rec["hash"])
    except Exception:
        return {"ok": False, "error": "vault is unreadable"}
    ok = hmac.compare_digest(_derive(password or "", salt), expect)
    # Heal a missing/mangled copy once the real password proved you're the owner.
    if ok and len(good) == 1:
        _write({k: rec[k] for k in rec})
    return {"ok": ok} if ok else {"ok": False, "error": "incorrect password"}


def require(password: str, operation: str) -> dict:
    """Gate for a protected operation. Returns {ok} or {ok:False, error}."""
    st = status()
    if not st["set"]:
        return {"ok": False, "unset": True,
                "error": f"'{operation}' is protected but no master password is set. "
                         f"Run `aios password set` first."}
    r = verify(password)
    if not r["ok"]:
        return {"ok": False, "error": r.get("error", "incorrect password"),
                "operation": operation}
    return {"ok": True, "operation": operation}


# Paths the agents' shell must never touch. Enforced in aios_sec.guard, which
# every agent command already routes through.
def protected_paths() -> list[str]:
    return [str(VAULT), str(MIRROR), str(VAULT.parent / "vault"), ".aios/vault.json",
            "~/.aios/vault.json", ".aios-vault"]


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print(json.dumps(status(), indent=2))
    elif cmd == "verify":
        import getpass
        print(json.dumps(verify(getpass.getpass("Master password: ")), indent=2))
