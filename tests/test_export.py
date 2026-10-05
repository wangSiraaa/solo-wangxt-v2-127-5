"""Thread fact export: JSON/text bundle, conflict hints, attachment verdicts.

Exports are rebuilt from persisted facts; these tests cover the API surface
(memory backend) plus builder edge cases that do not need HTTP.
"""
import json

from conftest import SAMPLES


def _post(c, name, **params):
    return c.post(
        "/ingest",
        files={"file": (name, (SAMPLES / name).read_bytes(), "message/rfc822")},
        params=params,
    )


def _ingest_thread_fixture(c):
    for n in [
        "02_cycle_a.eml",
        "02_cycle_b.eml",
        "04_duplicate_id_a.eml",
        "04_duplicate_id_b.eml",
        "01_multibyte.eml",
        "03_missing_id.eml",
    ]:
        r = _post(c, n, recompute_threads=False)
        assert r.status_code == 201, r.text
    c.post("/threads/rebuild")


def _thread_key_for(c, query):
    pk = c.get("/search", params={"q": query}).json()["results"][0]["id"]
    return c.get(f"/messages/{pk}").json()["thread_key"]


# ---------------------------------------------------------------- cycle ----


def test_cycle_thread_export_lists_all_members_and_conflicts(client):
    c, _ = client
    _ingest_thread_fixture(c)
    key = _thread_key_for(c, "cycle-a")
    r = c.get(f"/threads/{key}/export")
    assert r.status_code == 200, r.text
    bundle = r.json()

    assert bundle["schema_version"] == "thread-fact-export/1"
    assert bundle["thread_key"] == key
    # every member of the cyclic thread is present
    assert bundle["member_count"] == 2
    ids = {m["message_id"] for m in bundle["messages"]}
    assert ids == {"cycle-a@example.com", "cycle-b@example.com"}
    # messages are in time order
    dates = [m["date"] for m in bundle["messages"]]
    assert dates == sorted(dates)

    flat = {x for cyc in bundle["conflicts"]["cycles"] for x in cyc["cycle"]}
    assert {"cycle-a@example.com", "cycle-b@example.com"} <= flat
    cyc_pks = {pk for cyc in bundle["conflicts"]["cycles"] for pk in cyc["message_pks"]}
    assert cyc_pks == set(bundle["message_pks"])
    # dangling reference cycle-a -> cycle-c is surfaced
    dangling = bundle["conflicts"]["dangling_references"]
    assert any(d["message_id"] == "cycle-c@example.com" for d in dangling)
    flagged = {m["id"]: m["flags"] for m in bundle["messages"]}
    assert all("in_reference_cycle" in f for f in flagged.values())
    assert any("has_dangling_reference" in f for f in flagged.values())
    assert all(m["warnings"] for m in bundle["messages"])


def test_export_does_not_modify_thread_assignment(client):
    c, _ = client
    _ingest_thread_fixture(c)
    before = {m["id"]: m["thread_key"] for m in c.get("/messages").json()}
    for key in {v for v in before.values() if v}:
        r = c.get(f"/threads/{key}/export")
        assert r.status_code == 200
    after = {m["id"]: m["thread_key"] for m in c.get("/messages").json()}
    assert before == after


# ------------------------------------------------------------ duplicates ---


def test_duplicate_message_id_exports_two_records(client):
    c, _ = client
    _ingest_thread_fixture(c)
    key = _thread_key_for(c, "dup-1")
    bundle = c.get(f"/threads/{key}/export").json()

    assert bundle["member_count"] == 2
    assert len(bundle["messages"]) == 2
    assert [m["message_id"] for m in bundle["messages"]] == [
        "dup-1@example.com",
        "dup-1@example.com",
    ]
    dups = bundle["conflicts"]["duplicate_message_ids"]
    assert len(dups) == 1
    assert dups[0]["message_id"] == "dup-1@example.com"
    assert set(dups[0]["message_pks"]) == set(bundle["message_pks"])
    assert all("duplicate_message_id" in m["flags"] for m in bundle["messages"])


# ------------------------------------------------------------ attachments --


def test_missing_attachment_entity_keeps_metadata_marked_not_downloadable(client):
    c, arch = client
    _ingest_thread_fixture(c)
    key = _thread_key_for(c, "multi-01")

    # happy path: stored attachments are downloadable and carry metadata
    good = c.get(f"/threads/{key}/export").json()
    atts = [a for m in good["messages"] for a in m["attachments"]]
    assert len(atts) == 2
    assert {a["filename"] for a in atts} == {"banner.gif", "文本.pdf"}
    assert all(a["downloadable"] and a["sha256"] and a["byte_size"] for a in atts)

    # entity exists but was never persisted (storage failure at ingest):
    # metadata must survive and the record must be marked not downloadable.
    for row in arch.repo.attachments:
        row["storage_path"] = None
        row["stored"] = False
    missing = c.get(f"/threads/{key}/export").json()
    matts = [a for m in missing["messages"] for a in m["attachments"]]
    assert len(matts) == 2  # entities retained
    for a in matts:
        assert a["downloadable"] is False
        assert a["not_downloadable_reason"] == "not_stored"
        assert a["sha256"] and a["byte_size"] and a["content_type"]

    # path recorded but bytes gone from disk -> distinct reason
    for row in arch.repo.attachments:
        row["storage_path"] = "aa/deadbeefdoesnotexist_att.bin"
        row["stored"] = True
    gone = c.get(f"/threads/{key}/export").json()
    gatts = [a for m in gone["messages"] for a in m["attachments"]]
    assert all(not a["downloadable"] for a in gatts)
    assert all(a["not_downloadable_reason"] == "bytes_missing" for a in gatts)


def test_export_never_embeds_attachment_bytes_or_html(client):
    c, _ = client
    _post(c, "01_multibyte.eml")
    _post(c, "09_html_xss.eml")
    c.post("/threads/rebuild")
    xkey = _thread_key_for(c, "xss-01")
    raw = c.get(f"/threads/{xkey}/export").content.decode()
    # raw/sanitized/escaped HTML and remote resources never leave the archive
    assert "<script" not in raw
    assert "onerror" not in raw
    assert "safe_html" not in raw and "escaped_html" not in raw
    assert "http://evil" not in raw
    # extracted plain text is present instead (markup/attributes removed)
    plain = json.loads(raw)["messages"][0]["plain_bodies"][0]["plain_text"]
    assert "hello" in plain
    assert "<" not in plain
    # attachment payload marker is never embedded
    mkey = _thread_key_for(c, "multi-01")
    mraw = c.get(f"/threads/{mkey}/export").content.decode()
    assert "%PDF-1.4" not in mraw
    assert "GIF89aFAKEGIFDATA" not in mraw


# ------------------------------------------------------------ id/headers ---


def test_missing_message_id_member_is_flagged(client):
    c, _ = client
    _ingest_thread_fixture(c)
    pk = c.get("/search", params={"q": "nobody@example.com"}).json()["results"][0]["id"]
    key = c.get(f"/messages/{pk}").json()["thread_key"]
    bundle = c.get(f"/threads/{key}/export").json()
    assert bundle["conflicts"]["members_with_missing_message_id"] == [pk]
    member = next(m for m in bundle["messages"] if m["id"] == pk)
    assert "missing_message_id" in member["flags"]


def test_export_contains_headers_plain_body_and_raw_eml_summary(client):
    c, _ = client
    _ingest_thread_fixture(c)
    key = _thread_key_for(c, "cycle-a")
    m = c.get(f"/threads/{key}/export").json()["messages"][0]
    names = {h["name"] for h in m["headers"]}
    assert {"Message-ID", "References", "In-Reply-To", "From", "Subject", "Date"} <= names
    assert m["raw_eml"]["raw_sha256"] and m["raw_eml"]["raw_path"]
    assert m["raw_eml"]["available"] is True
    assert m["raw_eml"]["raw_size"]
    body = m["plain_bodies"][0]
    assert body["plain_text"].strip() == "A references B"
    assert body["text_source"] == "plain"


# ------------------------------------------------------------ formats ------


def test_text_format_is_readable_plain_text(client):
    c, _ = client
    _ingest_thread_fixture(c)
    key = _thread_key_for(c, "cycle-a")
    r = c.get(f"/threads/{key}/export", params={"format": "text"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert "THREAD FACT EXPORT" in text
    assert "cycle-a@example.com -> cycle-b@example.com -> cycle-a@example.com" in text
    assert "Original EML summary" in text
    assert "Attachments (metadata only; bytes never embedded)" in text
    assert "END OF THREAD FACT EXPORT" in text


def test_unknown_format_rejected_and_unknown_thread_404(client):
    c, _ = client
    _ingest_thread_fixture(c)
    key = _thread_key_for(c, "cycle-a")
    assert c.get(f"/threads/{key}/export", params={"format": "xml"}).status_code == 422
    assert c.get("/threads/thread-does-not-exist@nowhere/export").status_code == 404
