"""Anchoring the audit chain: signatures, RFC 3161 time stamps, truncation detection."""

import base64
from datetime import datetime, timezone

import httpx
import pytest
from asn1crypto import algos, cms, tsp

from riskmesh.domain import anchoring
from riskmesh.domain.errors import ValidationError
from riskmesh.domain.models import AuditAnchor, AuditEntry


def fake_token(digest: bytes, nonce=None) -> bytes:
    """A structurally valid RFC 3161 token (unsigned - enough to test our parsing and checks)."""
    info = tsp.TSTInfo({
        "version": "v1", "policy": "1.2.3.4", "serial_number": 7,
        "message_imprint": tsp.MessageImprint({"hash_algorithm": algos.DigestAlgorithm({"algorithm": "sha256"}),
                                               "hashed_message": digest}),
        "gen_time": datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc), "nonce": nonce,
    })
    signed = cms.SignedData({
        "version": "v3", "digest_algorithms": [algos.DigestAlgorithm({"algorithm": "sha256"})],
        "encap_content_info": cms.EncapsulatedContentInfo({"content_type": "tst_info", "content": info}),
        "signer_infos": [],
    })
    return cms.ContentInfo({"content_type": "signed_data", "content": signed}).dump()


@pytest.fixture
def fake_tsa(monkeypatch):
    calls = []

    def post(url, content, timeout, headers):
        req = tsp.TimeStampReq.load(content)
        calls.append(url)
        digest = req["message_imprint"]["hashed_message"].native
        reply = tsp.TimeStampResp({"status": {"status": "granted"},
                                   "time_stamp_token": cms.ContentInfo.load(fake_token(digest, req["nonce"].native))})
        return httpx.Response(200, content=reply.dump(), request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    return calls


@pytest.fixture
def key(tmp_path):
    path, kid = anchoring.generate_key(tmp_path / "audit-key.pem")
    return str(path), kid


def test_signed_anchor_verifies_and_detects_truncation(svc, key):
    svc.anchor_key_path = key[0]
    for name in ("A", "B", "C"):
        svc.create_person(name)
    anchor = svc.create_anchor("test")
    assert anchor.key_id == key[1] and anchor.seq == 3 and anchor.signature
    result = svc.verify_audit()
    assert result["ok"] and result["anchors"][0]["ok"]
    assert svc.list_audit(limit=1)[0].action == "ANCHOR"

    if type(svc.repo).__name__ == "SqlRepository":
        return  # entries cannot be deleted there at all
    # remove the newest entries: the chain alone would still look fine ...
    for e in sorted(svc.repo.list(AuditEntry), key=lambda e: e.seq)[2:]:
        svc.repo.delete(AuditEntry, e.id)
    result = svc.verify_audit(check_data=False)
    assert not result["chain_errors"]
    # ... but the anchor reveals it
    assert any("removed" in e["error"] for e in result["anchor_errors"]) and not result["ok"]


def test_forged_anchor_is_rejected(svc, key):
    svc.anchor_key_path = key[0]
    svc.create_person("A")
    anchor = svc.create_anchor()
    forged = svc.repo.get(AuditAnchor, anchor.id)
    forged.statement = anchoring.statement(forged.seq, "0" * 64, forged.created_at)
    forged.head_hash = "0" * 64
    svc.repo.save(forged)
    errors = {e["error"] for e in svc.verify_audit()["anchor_errors"]}
    assert any("signature" in e for e in errors) and any("differs" in e for e in errors)


def test_time_stamp_from_authority(svc, fake_tsa):
    svc.tsa_url = "https://tsa.example/tsr"
    svc.create_person("A")
    anchor = svc.create_anchor("quarter close")
    assert fake_tsa == ["https://tsa.example/tsr"] and anchor.tsa_time.startswith("2026-11-27")
    assert svc.verify_audit()["ok"]
    stored = svc.repo.get(AuditAnchor, anchor.id)
    stored.tsa_token = base64.b64encode(fake_token(b"\x00" * 32)).decode()  # token for another hash
    svc.repo.save(stored)
    assert any("time stamp" in e["error"] for e in svc.verify_audit()["anchor_errors"])


def test_refused_or_garbled_replies():
    digest = b"\x01" * 32
    status = tsp.PKIStatusInfo({"status": "rejection"}).dump()
    refused = b"0" + bytes([len(status)]) + status  # SEQUENCE { status } - a refusal carries no token
    with pytest.raises(ValidationError, match="refused"):
        anchoring.parse_timestamp_reply(refused, digest, 1)
    with pytest.raises(ValidationError):
        anchoring.parse_timestamp_reply(b"not asn1", digest, 1)
    ok = tsp.TimeStampResp({"status": {"status": "granted"},
                            "time_stamp_token": cms.ContentInfo.load(fake_token(digest, 99))}).dump()
    with pytest.raises(ValidationError, match="nonce"):
        anchoring.parse_timestamp_reply(ok, digest, 1)


def test_anchor_needs_configuration_and_entries(svc, key):
    with pytest.raises(ValidationError, match="not configured"):
        svc.create_anchor()
    svc.anchor_key_path = key[0]
    with pytest.raises(ValidationError, match="empty"):
        svc.create_anchor()


def test_key_file_is_private_and_not_overwritten(tmp_path):
    path, _ = anchoring.generate_key(tmp_path / "k.pem")
    with pytest.raises(FileExistsError):
        anchoring.generate_key(path)
    assert b"PRIVATE KEY" in path.read_bytes()
