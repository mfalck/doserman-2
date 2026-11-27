"""
Anchoring the audit chain: proof from outside the chain.

The hash chain reveals altered, inserted or removed entries - except the
newest ones: delete the last entries and the chain still looks fine. An
anchor fixes "entry #seq had hash H" in two independent ways:

1. Signature (Ed25519). `riskmesh audit keygen` creates a key pair; the private
   key signs a small statement about the chain head. Keep the private key away
   from the database (and note the public-key fingerprint somewhere else);
   otherwise someone with full server access could re-sign a shortened chain.

2. RFC 3161 time stamp. The chain-head hash is sent to a time-stamp authority
   (e.g. https://freetsa.org/tsr or a qualified Danish/EU TSA), which signs
   "this hash existed at time T". This needs no secret on the server and
   cannot be back-dated, so it is the stronger proof. Only the hash leaves the
   server - no data. Full verification of the authority's signature:
     openssl ts -verify -digest <head_hash> -in anchor.tsr -CAfile tsa-ca.pem

Both are optional; an anchor needs at least one of them.
"""

import base64
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .errors import ValidationError

STATEMENT_VERSION = 1


# -- statements -------------------------------------------------------------------

def statement(seq: int, head_hash: str, created_at: str) -> str:
    """The exact bytes that are signed (canonical JSON)."""
    return json.dumps({"riskmesh_audit_anchor": STATEMENT_VERSION, "seq": seq, "head_hash": head_hash,
                       "created_at": created_at}, sort_keys=True, separators=(",", ":"))


# -- Ed25519 keys and signatures -----------------------------------------------------

def generate_key(path: Path, overwrite: bool = False) -> Tuple[Path, str]:
    """Writes a new private key (PEM) readable only by the owner; returns (path, key id)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return path, key_id(public_pem(key))


def load_private_key(path: str):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    try:
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    except (OSError, ValueError) as e:
        raise ValidationError(f"Cannot read the audit signing key {path}: {e}") from e
    if not isinstance(key, Ed25519PrivateKey):
        raise ValidationError("The audit signing key must be an Ed25519 key (create one with `riskmesh audit keygen`).")
    return key


def public_pem(private_key) -> str:
    from cryptography.hazmat.primitives import serialization
    return private_key.public_key().public_bytes(serialization.Encoding.PEM,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def key_id(public_key_pem: str) -> str:
    return hashlib.sha256(public_key_pem.encode()).hexdigest()[:16]


def sign(private_key, text: str) -> str:
    return base64.b64encode(private_key.sign(text.encode())).decode()


def verify_signature(public_key_pem: str, text: str, signature_b64: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    try:
        key = serialization.load_pem_public_key(public_key_pem.encode())
        key.verify(base64.b64decode(signature_b64), text.encode())
        return True
    except (InvalidSignature, ValueError):
        return False


# -- RFC 3161 time stamps ---------------------------------------------------------------

def timestamp_request(digest: bytes, nonce: int) -> bytes:
    from asn1crypto import algos, tsp
    return tsp.TimeStampReq({
        "version": 1,
        "message_imprint": tsp.MessageImprint({
            "hash_algorithm": algos.DigestAlgorithm({"algorithm": "sha256"}),
            "hashed_message": digest,
        }),
        "nonce": nonce,
        "cert_req": True,
    }).dump()


def parse_timestamp_token(token_der: bytes, digest: bytes, nonce: Optional[int] = None) -> str:
    """Checks that a time-stamp token covers `digest`; returns the authority's time (ISO)."""
    from asn1crypto import cms
    try:
        info = cms.ContentInfo.load(token_der)["content"]["encap_content_info"]["content"].parsed
    except (ValueError, TypeError, KeyError) as e:
        raise ValidationError(f"Not a valid time-stamp token: {e}") from e
    if info["message_imprint"]["hashed_message"].native != digest:
        raise ValidationError("The time stamp covers a different hash.")
    if nonce is not None and info["nonce"].native != nonce:
        raise ValidationError("The time-stamp reply does not answer our request (nonce).")
    return info["gen_time"].native.isoformat()


def parse_timestamp_reply(body: bytes, digest: bytes, nonce: int) -> Tuple[bytes, str]:
    # Parsed field by field: a refusal has no token, which asn1crypto's TimeStampResp
    # does not allow although RFC 3161 does.
    from asn1crypto import core, tsp
    try:
        outer = core.Sequence.load(body)
        status_info = tsp.PKIStatusInfo.load(outer[0].dump())
        status = status_info["status"].native
    except (ValueError, TypeError, KeyError, IndexError) as e:
        raise ValidationError(f"The time-stamp authority sent an invalid reply: {e}") from e
    if status not in ("granted", "granted_with_mods"):
        text = " ".join(status_info["status_string"].native or []) if status_info["status_string"] else ""
        raise ValidationError(f"The time-stamp authority refused: {status} {text}".strip())
    try:
        token = outer[1].dump()
    except (ValueError, IndexError) as e:
        raise ValidationError("The time-stamp reply has no token.") from e
    return token, parse_timestamp_token(token, digest, nonce)


def request_timestamp(tsa_url: str, digest: bytes, timeout: float = 15) -> Tuple[bytes, str]:
    """Asks the authority to time-stamp `digest` (only the hash is sent). Returns (token DER, time)."""
    import httpx
    nonce = secrets.randbits(63)
    try:
        r = httpx.post(tsa_url, content=timestamp_request(digest, nonce), timeout=timeout,
                       headers={"Content-Type": "application/timestamp-query"})
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise ValidationError(f"Could not reach the time-stamp authority {tsa_url}: {e}") from e
    return parse_timestamp_reply(r.content, digest, nonce)


def describe(anchor: Dict[str, Any]) -> str:
    parts = [f"#{anchor['seq']} {anchor['head_hash'][:16]}…"]
    if anchor.get("key_id"):
        parts.append(f"signed by key {anchor['key_id']}")
    if anchor.get("tsa_time"):
        parts.append(f"time-stamped {anchor['tsa_time'][:19]} by {anchor.get('tsa_url')}")
    return ", ".join(parts)
