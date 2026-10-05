"""Thread fact export for offline / legal review.

The export is a **read-only reconstruction from persisted facts**:

* Membership and order come from the stored ``thread_key`` assignment (the
  existing conversation merge result is never recomputed or mutated).
* Conflict hints (cycles, dangling references, duplicate Message-IDs, weak
  subject suggestions) are recomputed with the same pure
  :func:`app.threads.compute_threads` function used by ``/threads/rebuild`` —
  but only for the *report*, nothing is written back.
* Every member mail carries its headers, **plain-text only** bodies,
  attachment *metadata* (never bytes) and a digest/size summary of the
  original EML (never the EML bytes). Raw/sanitized HTML is deliberately
  excluded; an HTML part contributes extracted plain text only.

Two renderings are offered: a JSON document (:func:`build_thread_export`) and
a human-readable text rendering (:func:`render_text_export`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from app.threads import ThreadInput, compute_threads

EXPORT_KIND = "thread_facts"
EXPORT_SCHEMA_VERSION = "1.0"

# Given a storage-relative path, return whether the bytes are currently on
# disk. Injected by the API layer (controlled storage); ``None`` means the
# availability is unknown and left out of the document.
AvailabilityChecker = Callable[[str], bool]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _format_addresses(addrs: list[dict[str, Any]] | None) -> str:
    parts: list[str] = []
    for a in addrs or []:
        name = (a.get("display_name") or "").strip()
        addr = (a.get("address") or "").strip()
        if name and addr:
            parts.append(f"{name} <{addr}>")
        elif addr:
            parts.append(addr)
        elif name:
            parts.append(name)
        elif a.get("raw"):
            parts.append(str(a["raw"]))
    return "; ".join(parts)


def _plain_body(body: dict[str, Any]) -> str:
    """Return only safe plain text for a body row.

    ``text/plain`` rows store their text in ``text``; ``text/html`` rows only
    contribute the already extracted ``plain_text``. ``safe_html`` /
    ``escaped_html`` are never exported.
    """
    if body.get("content_type") == "text/plain":
        return body.get("text") or ""
    return body.get("plain_text") or ""


def _attachment_record(
    att: dict[str, Any], attachment_available: AvailabilityChecker | None
) -> dict[str, Any]:
    storage_path = att.get("storage_path")
    stored = bool(att.get("stored")) and storage_path is not None
    downloadable = False
    reason: str | None = None
    if not stored:
        # Parser metadata exists but the bytes were never persisted (e.g. the
        # storage layer rejected/failed the write).
        reason = "not_stored"
    elif attachment_available is not None:
        try:
            downloadable = bool(attachment_available(storage_path))
        except Exception:  # a broken store must not break the export
            downloadable = False
        if not downloadable:
            reason = "bytes_missing"
    else:
        # No disk probe available; the record claims storage but availability
        # is unverified. Report conservatively.
        downloadable = True
    return {
        "attachment_id": att["id"],
        "message_pk": att["message_pk"],
        "mime_path": att.get("mime_path"),
        "content_type": att.get("content_type"),
        "charset": att.get("charset"),
        "disposition": att.get("disposition"),
        "filename": att.get("filename"),
        "raw_filename": att.get("raw_filename"),
        "content_id": att.get("content_id"),
        "content_location": att.get("content_location"),
        "byte_size": att.get("byte_size"),
        "sha256": att.get("checksum_sha256"),
        "storage_path": storage_path,
        "stored": bool(att.get("stored")),
        "downloadable": downloadable,
        "not_downloadable_reason": reason,
    }


def _member_hints(
    pk: int,
    mid: str | None,
    cycles: list[list[str]],
    duplicates: dict[str, list[int]],
    dangling: list[dict[str, Any]],
    weak: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    if not mid:
        hints.append(
            {
                "kind": "missing_message_id",
                "detail": "message carries no Message-ID; it is threaded only via reference tokens",
            }
        )
    else:
        low = mid.lower()
        for cyc in cycles:
            if low in {token.lower() for token in cyc}:
                hints.append({"kind": "reference_cycle", "cycle": list(cyc)})
        if low in duplicates:
            hints.append(
                {
                    "kind": "duplicate_message_id",
                    "message_id": mid,
                    "message_pks": list(duplicates[low]),
                }
            )
    for d in dangling:
        if d["message_pk"] != pk:
            continue
        hints.append(
            {
                "kind": "dangling_reference",
                "header": d["header"],
                "target_message_id": d["message_id"],
                "detail": "reference names a Message-ID with no stored message",
            }
        )
    for w in weak:
        if w["message_pk_a"] != pk and w["message_pk_b"] != pk:
            continue
        other = w["message_pk_b"] if w["message_pk_a"] == pk else w["message_pk_a"]
        hints.append(
            {
                "kind": "weak_subject_suggestion",
                "subject": w["subject"],
                "other_message_pk": other,
                "reason": w["reason"],
            }
        )
    return hints


def build_thread_export(
    repo: Any,
    thread_key: str,
    *,
    attachment_available: AvailabilityChecker | None = None,
    raw_available: AvailabilityChecker | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Build the JSON-serializable fact export for one thread.

    Returns ``None`` when no stored message belongs to ``thread_key``.

    The function only reads from the repository and the pure threader; the
    stored conversation assignment (``messages.thread_key``) is never changed.
    """
    thread = repo.get_thread(thread_key)
    if thread is None:
        return None
    member_rows = thread["messages"]  # already date ASC NULLS LAST, id ASC
    pks = [row["id"] for row in member_rows]
    member_set = set(pks)

    # Recompute the id graph from *currently persisted* identifier facts to
    # derive conflict hints, without persisting anything.
    facts = repo.get_thread_facts()
    inputs: list[ThreadInput] = [
        ThreadInput(
            f["message_pk"],
            f["message_id"],
            list(f["references"]),
            list(f["in_reply_to"]),
            f["subject"],
            f["timestamp"],
        )
        for f in facts["messages"]
    ]
    result = compute_threads(inputs)
    pk_to_input = {ti.message_pk: ti for ti in inputs}

    member_ids = {
        pk_to_input[pk].message_id.lower()
        for pk in pks
        if pk in pk_to_input and pk_to_input[pk].message_id
    }

    # Scope every global conflict down to the members of this thread.
    member_cycles = [
        list(cyc) for cyc in result.cycles if member_ids & {token.lower() for token in cyc}
    ]
    member_duplicates = {
        mid: list(dup_pks)
        for mid, dup_pks in result.duplicate_ids.items()
        if member_set & set(dup_pks)
    }
    member_dangling = [dict(d) for d in result.dangling_references if d["message_pk"] in member_set]
    member_weak = [
        dict(w)
        for w in result.weak_suggestions
        if w["message_pk_a"] in member_set or w["message_pk_b"] in member_set
    ]

    members: list[dict[str, Any]] = []
    for ordinal, row in enumerate(member_rows, start=1):
        pk = row["id"]
        detail = repo.get_message(pk) or {}
        ingest = repo.get_ingest(detail.get("ingest_id")) or {}
        ti = pk_to_input.get(pk)
        mid = row.get("message_id")

        headers = [dict(h) for h in repo.list_message_headers(pk)]
        headers.sort(key=lambda h: h.get("ordinal", 0))

        bodies = [
            {
                "mime_path": b.get("mime_path"),
                "content_type": b.get("content_type"),
                "charset": b.get("charset"),
                "declared_charset": b.get("declared_charset"),
                "disposition": b.get("disposition"),
                "content_id": b.get("content_id"),
                "content_location": b.get("content_location"),
                "byte_size": b.get("byte_size"),
                # Plain text only — safe_html / escaped_html never exported.
                "plain_text": _plain_body(b),
            }
            for b in detail.get("bodies", [])
        ]
        attachments = [
            _attachment_record(a, attachment_available) for a in detail.get("attachments", [])
        ]

        raw_path = detail.get("raw_path") or ingest.get("raw_path")
        raw_state: str | None = None
        if raw_path is None:
            raw_state = "not_stored"
        elif raw_available is not None:
            try:
                raw_state = "available" if raw_available(raw_path) else "bytes_missing"
            except Exception:
                raw_state = "bytes_missing"
        raw_eml = {
            "ingest_id": detail.get("ingest_id"),
            "ingest_status": ingest.get("status"),
            "source_name": ingest.get("source_name"),
            "raw_sha256": detail.get("raw_sha256") or ingest.get("raw_sha256"),
            "raw_size": ingest.get("raw_size"),
            "storage_path": raw_path,
            "availability": raw_state,  # None when no disk probe was supplied
        }

        hints = _member_hints(
            pk, mid, member_cycles, member_duplicates, member_dangling, member_weak
        )

        members.append(
            {
                "ordinal": ordinal,
                "message_pk": pk,
                "message_id": mid,
                "missing_message_id": mid is None,
                "subject": row.get("subject"),
                "raw_subject": detail.get("raw_subject"),
                "date": _iso(row.get("date")),
                "from": detail.get("from_json", []),
                "to": detail.get("to_json", []),
                "cc": detail.get("cc_json", []),
                "bcc": detail.get("bcc_json", []),
                "reply_to": detail.get("reply_to_json", []),
                "sender": detail.get("sender_json", []),
                "references": list(ti.references) if ti else [],
                "in_reply_to": list(ti.in_reply_to) if ti else [],
                "headers": headers,
                "bodies": bodies,
                "attachments": attachments,
                "raw_eml": raw_eml,
                "hints": hints,
            }
        )

    dates = [m["date"] for m in members if m["date"]]
    generated = generated_at or datetime.now(timezone.utc)
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    return {
        "export_kind": EXPORT_KIND,
        "schema_version": EXPORT_SCHEMA_VERSION,
        "generated_at": _iso(generated),
        "thread_key": thread_key,
        "root_message_id": result.roots.get(thread_key),
        "ordering": "date:asc,message_pk:asc",
        "message_count": len(members),
        "started_at": min(dates) if dates else None,
        "last_at": max(dates) if dates else None,
        "conflicts": {
            "cycles": member_cycles,
            "duplicate_message_ids": [
                {"message_id": mid, "message_pks": pks_} for mid, pks_ in sorted(member_duplicates.items())
            ],
            "dangling_references": member_dangling,
            "weak_suggestions": member_weak,
        },
        "messages": members,
    }


# ---------------------------------------------------------------------------
# Readable text rendering
# ---------------------------------------------------------------------------

_RULE = "=" * 79
_SUBRULE = "-" * 79


def _oneline(value: Any) -> str:
    """Flatten a scalar to one line so header data cannot forge the layout."""
    if value is None:
        return "-"
    text = str(value).replace("\r", " ").replace("\n", " ")
    return " ".join(text.split()) or "-"


def _envelope_lines(m: dict[str, Any]) -> list[str]:
    lines = []
    for label, key in (
        ("From", "from"),
        ("To", "to"),
        ("Sender", "sender"),
        ("Reply-To", "reply_to"),
        ("Cc", "cc"),
        ("Bcc", "bcc"),
    ):
        value = _format_addresses(m.get(key))
        if value:
            lines.append(f"{label}: {value}")
    return lines


def _hint_line(h: dict[str, Any]) -> str:
    kind = h["kind"]
    if kind == "reference_cycle":
        return f"* [cycle] reference cycle: {' -> '.join(h['cycle'])}"
    if kind == "duplicate_message_id":
        return (
            f"* [duplicate-message-id] {h['message_id']} is claimed by "
            f"message pks {', '.join(map(str, h['message_pks']))}"
        )
    if kind == "dangling_reference":
        return (
            f"* [dangling-reference] {h['header']} points to unknown "
            f"Message-ID {h['target_message_id']}"
        )
    if kind == "missing_message_id":
        return "* [missing-message-id] no Message-ID header; threaded via reference tokens only"
    if kind == "weak_subject_suggestion":
        return (
            f"* [weak-subject-suggestion] subject {h['subject']!r} also resembles "
            f"message pk {h['other_message_pk']} ({h['reason']}; not merged)"
        )
    return f"* [{kind}] {h.get('detail', '')}".rstrip()


def render_text_export(doc: dict[str, Any]) -> str:
    """Render the export document as reviewer-friendly plain text."""
    out: list[str] = []
    out.append(_RULE)
    out.append("THREAD FACT EXPORT (offline review)")
    out.append(_RULE)
    out.append(f"Thread key: {doc['thread_key']}")
    out.append(f"Root Message-ID: {doc.get('root_message_id') or '-'}")
    out.append(f"Generated at: {doc['generated_at']}")
    out.append(f"Ordering: {doc['ordering']}")
    out.append(f"Messages: {doc['message_count']}")
    out.append("")
    out.append(_SUBRULE)
    out.append("CONFLICT HINTS AFFECTING THIS THREAD")
    out.append(_SUBRULE)
    conflicts = doc["conflicts"]
    if not any(conflicts.values()):
        out.append("(none)")
    for i, cyc in enumerate(conflicts["cycles"], start=1):
        out.append(f"[cycle #{i}] {' -> '.join(cyc)}")
    for d in conflicts["duplicate_message_ids"]:
        out.append(
            f"[duplicate-message-id] {d['message_id']} claimed by message pks "
            f"{', '.join(map(str, d['message_pks']))}"
        )
    for d in conflicts["dangling_references"]:
        out.append(
            f"[dangling-reference] message pk {d['message_pk']} {d['header']} -> "
            f"unknown Message-ID {d['message_id']}"
        )
    for w in conflicts["weak_suggestions"]:
        out.append(
            f"[weak-suggestion] subject {w['subject']!r}: pks "
            f"{w['message_pk_a']} <-> {w['message_pk_b']} ({w['reason']}; not merged)"
        )
    out.append("")

    total = len(doc["messages"])
    for m in doc["messages"]:
        out.append(_RULE)
        out.append(f"MESSAGE {m['ordinal']}/{total} (message pk {m['message_pk']})")
        out.append(_RULE)
        out.append(f"Message-ID: {_oneline(m['message_id'])}")
        if m["missing_message_id"]:
            out.append("Message-ID: - (MISSING)")
        out.append(f"Subject: {_oneline(m['subject'])}")
        out.append(f"Raw subject: {_oneline(m['raw_subject'])}")
        out.append(f"Date: {_oneline(m['date'])}")
        out.extend(_envelope_lines(m))
        if m["references"]:
            out.append("References: " + ", ".join(m["references"]))
        if m["in_reply_to"]:
            out.append("In-Reply-To: " + ", ".join(m["in_reply_to"]))
        out.append("")

        out.append(f"-- Headers ({len(m['headers'])}) --")
        for h in m["headers"]:
            out.append(f"[{h['ordinal']:>3}] {h['name']}: {_oneline(h['value'])}")
            if h.get("raw_value") is not None and h["raw_value"] != h["value"]:
                out.append(f"      raw: {_oneline(h['raw_value'])}")
        out.append("")

        out.append(f"-- Body parts ({len(m['bodies'])}, plain text only) --")
        for i, b in enumerate(m["bodies"], start=1):
            meta = (
                f"[body {i}] {b['content_type']} mime={b['mime_path']} "
                f"charset={b.get('charset') or '-'} bytes={b.get('byte_size')}"
            )
            if b.get("content_id"):
                meta += f" cid={b['content_id']}"
            out.append(meta)
            text = b.get("plain_text") or ""
            out.append("----- BEGIN BODY -----")
            out.append(text)
            out.append("----- END BODY -----")
        out.append("")

        out.append(f"-- Attachments ({len(m['attachments'])}, metadata only; no bytes) --")
        for i, a in enumerate(m["attachments"], start=1):
            out.append(
                f"[attachment {i}] id={a['attachment_id']} {a['content_type']} "
                f"mime={a['mime_path']} disposition={a['disposition']}"
            )
            out.append(f"    filename: {_oneline(a['filename'])}")
            if a.get("raw_filename") and a["raw_filename"] != a["filename"]:
                out.append(f"    raw filename: {_oneline(a['raw_filename'])}")
            if a.get("content_id"):
                out.append(f"    content-id: {a['content_id']}")
            if a.get("content_location"):
                out.append(f"    content-location: {a['content_location']}")
            out.append(
                f"    bytes={a['byte_size']} sha256={a.get('sha256') or '-'}"
            )
            if a["downloadable"]:
                out.append(
                    f"    stored: yes ({a['storage_path']}); downloadable: yes"
                )
            else:
                out.append(
                    f"    stored: {'yes' if a['stored'] else 'no'}"
                    f" ({a.get('storage_path') or '-'}); downloadable: NO"
                    f" [{a.get('not_downloadable_reason') or 'unverified'}]"
                )
        out.append("")

        raw = m["raw_eml"]
        out.append("-- Original EML summary (no bytes) --")
        out.append(f"    ingest id: {raw.get('ingest_id')}  status: {raw.get('ingest_status')}")
        out.append(f"    source name: {_oneline(raw.get('source_name'))}")
        out.append(f"    sha256: {raw.get('raw_sha256') or '-'}")
        out.append(f"    size: {raw.get('raw_size') if raw.get('raw_size') is not None else '-'} bytes")
        out.append(
            f"    storage path: {raw.get('storage_path') or '-'}  "
            f"availability: {raw.get('availability') or 'unverified'}"
        )
        out.append("")

        out.append("-- Message hints --")
        if m["hints"]:
            out.extend(_hint_line(h) for h in m["hints"])
        else:
            out.append("(none)")
        out.append("")

    return "\n".join(out).rstrip() + "\n"
