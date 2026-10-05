"""Thread fact export: JSON + readable text, reconstructed from persisted facts.

Covers the acceptance cases:
* a circular-reference thread exports all members plus cycle conflict hints;
* attachments whose entity/bytes are missing still export metadata and are
  marked not downloadable;
* two mails sharing a Message-ID remain two separate member records;
* no attachment/EML bytes and no unprocessed HTML leave the export.
"""
from __future__ import annotations

import json

from conftest import SAMPLES


def _post(client, name, data=None, **params):
    if data is None:
        data = (SAMPLES / name).read_bytes()
    return client.post(
        "/ingest",
        files={"file": (name, data, "message/rfc822")},
        params=params,
    )


def _ingest_all(client, names):
    for n in names:
        r = _post(client, n, recompute_threads=False)
        assert r.status_code == 201, r.text
    return client.post("/threads/rebuild").json()


def _cycle_thread_key(client):
    rebuilt = client.post("/threads/rebuild").json()
    assert rebuilt["cycles"]
    pk = client.get("/search", params={"q": "cycle-a"}).json()["results"][0]["id"]
    return client.get(f"/messages/{pk}").json()["thread_key"]


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------

def test_cycle_thread_exports_all_members_and_cycle_hint(client):
    c, _ = client
    _ingest_all(c, ["02_cycle_a.eml", "02_cycle_b.eml"])
    key = _cycle_thread_key(c)

    r = c.get(f"/threads/{key}/export", params={"fmt": "json"})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["export_kind"] == "thread_facts"
    assert doc["thread_key"] == key
    assert len(doc["messages"]) == 2, "every cycle member must be exported"

    # time order: A (08:00) before B (09:00)
    assert [m["message_id"] for m in doc["messages"]] == [
        "cycle-a@example.com",
        "cycle-b@example.com",
    ]
    # top-level conflict hint
    assert doc["conflicts"]["cycles"]
    flat = {x for cyc in doc["conflicts"]["cycles"] for x in cyc}
    assert {"cycle-a@example.com", "cycle-b@example.com"} <= flat
    # per-member cycle hints
    for m in doc["messages"]:
        assert any(h["kind"] == "reference_cycle" for h in m["hints"])
    # dangling: A also references unknown cycle-c@example.com
    danglers = {
        (m["message_id"], h["header"], h["target_message_id"])
        for m in doc["messages"]
        for h in m["hints"]
        if h["kind"] == "dangling_reference"
    }
    assert ("cycle-a@example.com", "references", "cycle-c@example.com") in danglers
    # only A carries the dangling hint; B must not inherit it
    for m in doc["messages"]:
        own = [h for h in m["hints"] if h["kind"] == "dangling_reference"]
        if m["message_id"] == "cycle-b@example.com":
            assert own == []
    assert any(d["message_id"] == "cycle-c@example.com" for d in doc["conflicts"]["dangling_references"])


def test_headers_bodies_and_raw_summary_present_without_html_or_bytes(client):
    c, _ = client
    _post(c, "01_multibyte.eml")
    pk = c.get("/search", params={"q": "multi-01"}).json()["results"][0]["id"]
    key = c.get(f"/messages/{pk}").json()["thread_key"]

    doc = c.get(f"/threads/{key}/export").json()
    m = doc["messages"][0]

    headers = {(h["name"].lower(), h["value"]) for h in m["headers"]}
    assert any(name == "message-id" and "multi-01@example.com" in value for name, value in headers)

    # plain text body content is present for reviewers
    plain = "\n".join(b["plain_text"] or "" for b in m["bodies"])
    assert "UTF-8 body" in plain
    # no html representation ever crosses the export boundary
    serialized = json.dumps(doc, ensure_ascii=False)
    assert "safe_html" not in serialized and "escaped_html" not in serialized
    assert "<script" not in serialized
    # no attachment payload bytes and no raw EML bytes
    assert "JVBERi0xLjQ" not in serialized  # pdf base64 fragment
    assert "GIF89aFAKEGIFDATA" not in serialized

    attachments = m["attachments"]
    pdf = next(a for a in attachments if a["content_type"] == "application/pdf")
    assert pdf["downloadable"] is True
    assert pdf["filename"] == "文本.pdf"
    assert pdf["sha256"] and pdf["storage_path"]
    assert isinstance(pdf["byte_size"], int)

    raw = m["raw_eml"]
    assert raw["raw_sha256"] and raw["raw_size"]
    assert raw["availability"] == "available"
    assert raw["storage_path"]


def test_html_only_message_exports_extracted_text_not_markup(client):
    c, _ = client
    _post(c, "09_html_xss.eml")
    pk = c.get("/search", params={"q": "xss-01"}).json()["results"][0]["id"]
    key = c.get(f"/messages/{pk}").json()["thread_key"]

    serialized = c.get(f"/threads/{key}/export").content.decode("utf-8")
    assert "hello" in serialized
    assert "onclick" not in serialized
    assert "javascript:alert" not in serialized
    assert "http://evil" not in serialized
    assert "<script" not in serialized


def test_missing_attachment_bytes_still_exports_metadata_marked_not_downloadable(client):
    c, arch = client
    r = _post(c, "08_traversal.eml")
    pk = r.json()["message_pk"]
    key = c.get(f"/messages/{pk}").json()["thread_key"]

    # delete the persisted bytes from the controlled store; metadata remains
    att_row = next(a for a in arch.repo.attachments if a["message_pk"] == pk)
    stored_path = att_row["storage_path"]
    abs_path = arch.attachment_storage.resolve(stored_path)
    abs_path.unlink()
    assert not arch.attachment_storage.exists(stored_path)

    doc = c.get(f"/threads/{key}/export").json()
    atts = doc["messages"][0]["attachments"]
    assert len(atts) == 1
    att = atts[0]
    assert att["downloadable"] is False
    assert att["not_downloadable_reason"] == "bytes_missing"
    assert att["byte_size"] == len(b"malicious payload bytes")
    assert att["sha256"] and att["content_type"] == "application/octet-stream"
    # metadata still present even though nothing can be downloaded
    assert att["filename"] and att["mime_path"]


def test_attachment_never_stored_marked_not_stored(client):
    c, arch = client
    _post(c, "01_multibyte.eml")
    pk = c.get("/search", params={"q": "multi-01"}).json()["results"][0]["id"]
    # emulate a failed write: entity metadata kept without a storage path
    for row in arch.repo.attachments:
        if row["message_pk"] == pk and row["content_type"] == "application/pdf":
            row["storage_path"] = None
            row["stored"] = False
    key = c.get(f"/messages/{pk}").json()["thread_key"]
    doc = c.get(f"/threads/{key}/export").json()
    att = next(
        a for a in doc["messages"][0]["attachments"] if a["content_type"] == "application/pdf"
    )
    assert att["downloadable"] is False
    assert att["not_downloadable_reason"] == "not_stored"
    assert att["filename"] == "文本.pdf"


def test_duplicate_message_id_remains_two_member_records(client):
    c, _ = client
    _ingest_all(c, ["04_duplicate_id_a.eml", "04_duplicate_id_b.eml"])
    pk_a = c.get("/search", params={"q": "Duplicate ID first"}).json()["results"][0]["id"]
    key = c.get(f"/messages/{pk_a}").json()["thread_key"]

    doc = c.get(f"/threads/{key}/export").json()
    assert len(doc["messages"]) == 2
    assert [m["message_id"] for m in doc["messages"]] == [
        "dup-1@example.com",
        "dup-1@example.com",
    ]
    pks = {m["message_pk"] for m in doc["messages"]}
    assert len(pks) == 2  # two distinct records, not merged into one row
    subjects = {m["subject"] for m in doc["messages"]}
    assert subjects == {"Duplicate ID first", "Duplicate ID second (conflict)"}

    dup = doc["conflicts"]["duplicate_message_ids"]
    assert len(dup) == 1 and dup[0]["message_id"] == "dup-1@example.com"
    assert set(dup[0]["message_pks"]) == pks
    for m in doc["messages"]:
        assert any(h["kind"] == "duplicate_message_id" for h in m["hints"])


def test_missing_id_member_is_flagged(client):
    c, _ = client
    # 01 references parent-01/root-00; ingest a root + the missing-id draft via
    # reference token. Instead, simplest: ingest a mail whose id exists plus a
    # no-id mail that references it.
    root = (
        b"Message-ID: <known-root@example.com>\r\nFrom: r@example.com\r\n"
        b"Subject: Root\r\nDate: Mon, 28 Sep 2026 08:00:00 +0000\r\n"
        b"Content-Type: text/plain\r\n\r\nroot\r\n"
    )
    child = (
        b"From: n@example.com\r\nSubject: Re: Root\r\n"
        b"Date: Mon, 28 Sep 2026 09:00:00 +0000\r\n"
        b"In-Reply-To: <known-root@example.com>\r\n"
        b"Content-Type: text/plain\r\n\r\nno id child\r\n"
    )
    _post(c, "root.eml", root)
    _post(c, "child.eml", child)
    c.post("/threads/rebuild")
    pk_root = c.get("/search", params={"q": "known-root"}).json()["results"][0]["id"]
    key = c.get(f"/messages/{pk_root}").json()["thread_key"]

    doc = c.get(f"/threads/{key}/export").json()
    assert len(doc["messages"]) == 2
    no_id = next(m for m in doc["messages"] if m["message_id"] is None)
    assert no_id["missing_message_id"] is True
    assert any(h["kind"] == "missing_message_id" for h in no_id["hints"])


def test_export_does_not_mutate_thread_assignment(client):
    c, _ = client
    _ingest_all(c, ["02_cycle_a.eml", "02_cycle_b.eml", "04_duplicate_id_a.eml",
                    "04_duplicate_id_b.eml", "05_same_subject_root.eml",
                    "05_same_subject_other.eml"])
    before = {m["id"]: m["thread_key"] for m in c.get("/messages", params={"limit": 200}).json()}
    for key in {v for v in before.values() if v}:
        r = c.get(f"/threads/{key}/export")
        assert r.status_code == 200
    after = {m["id"]: m["thread_key"] for m in c.get("/messages", params={"limit": 200}).json()}
    assert before == after


def test_export_unknown_thread_404(client):
    c, _ = client
    assert c.get("/threads/thread-does-not-exist@nowhere/export").status_code == 404


# ---------------------------------------------------------------------------
# Text export
# ---------------------------------------------------------------------------

def test_text_export_is_readable_and_carries_same_hints(client):
    c, _ = client
    _ingest_all(c, ["02_cycle_a.eml", "02_cycle_b.eml", "04_duplicate_id_a.eml",
                    "04_duplicate_id_b.eml"])
    key = _cycle_thread_key(c)
    r = c.get(f"/threads/{key}/export", params={"fmt": "text"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    text = r.content.decode("utf-8")
    assert "THREAD FACT EXPORT" in text
    assert "cycle-a@example.com" in text and "cycle-b@example.com" in text
    assert "[cycle" in text
    assert "A references B" in text  # body plain text
    assert "Headers" in text
    assert "Original EML summary" in text
    # no bytes
    assert "BEGIN BODY" in text


def test_text_export_flags_not_downloadable_attachment(client):
    c, arch = client
    _post(c, "08_traversal.eml")
    pk = c.get("/search", params={"q": "trav-01"}).json()["results"][0]["id"]
    key = c.get(f"/messages/{pk}").json()["thread_key"]
    row = next(a for a in arch.repo.attachments if a["message_pk"] == pk)
    arch.attachment_storage.resolve(row["storage_path"]).unlink()
    text = c.get(f"/threads/{key}/export", params={"fmt": "text"}).content.decode("utf-8")
    assert "downloadable: NO" in text
    assert "[bytes_missing]" in text
    # metadata survives
    assert "pwned" in text


def test_invalid_format_rejected(client):
    c, _ = client
    _ingest_all(c, ["02_cycle_a.eml", "02_cycle_b.eml"])
    key = _cycle_thread_key(c)
    assert c.get(f"/threads/{key}/export", params={"fmt": "xml"}).status_code == 422
