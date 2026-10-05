"""Thread fact export for offline / legal review.

The exporter rebuilds a review bundle **from persisted facts only**: stored
headers, extracted plain-text bodies, attachment metadata, raw-EML provenance
and the already-persisted ``thread_key`` assignments. It never reparses raw
bytes, never reassigns threads, and it contains neither attachment bytes nor
raw/sanitized HTML (HTML bodies are represented solely by the extracted plain
text). Threading conflicts (cycles, duplicate Message-IDs, dangling
references, missing ids) are *recomputed as read-only hints* via
:func:`app.threads.compute_threads`; the hints are reported, never applied.

Two renderings are provided: a JSON-friendly dict bundle
(:func:`build_thread_export`) and a stable human-readable text rendering
(:func:`render_thread_export_text`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from app.threads import ThreadInput, compute_threads

# Callable(relative_path) -> True when the bytes are currently retrievable.
# None means availability cannot be determined; the stored flag is then used.
AvailabilityCheck = Callable[[str | None], bool] | None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _iso(value: datetime | None) -> str | None:
    value = _utc(value)
    return value.isoformat() if value is not None else None


def _addresses(values: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in values or []:
        out.append(
            {
                "display_name": a.get("display_name", ""),
                "address": a.get("address", ""),
                "raw": a.get("raw", ""),
            }
        )
    return out


def _identifiers(
    rows: Iterable[dict[str, Any]], kind: str
) -> list[dict[str, Any]]:
    items = [r for r in rows if r.get("kind") == kind]
    items.sort(key=lambda r: r.get("ordinal", 0))
    return [{"ordinal": r.get("ordinal", i), "value": r.get("value", "")} for i, r in enumerate(items)]


def _headers(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    items = sorted(rows, key=lambda r: r.get("ordinal", 0))
    return [
        {
            "ordinal": h.get("ordinal", i),
            "name": h.get("name", ""),
            "value": h.get("value", ""),
            "raw_value": h.get("raw_value", h.get("value", "")),
        }
        for i, h in enumerate(items)
    ]


def _plain_bodies(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project body facts down to plain text only.

    ``text`` / ``safe_html`` / ``escaped_html`` are intentionally dropped so an
    export can never carry unprocessed or markup-bearing content.
    """
    out: list[dict[str, Any]] = []
    for b in rows:
        ctype = b.get("content_type", "")
        text = b.get("plain_text")
        if not text and ctype == "text/plain":
            text = b.get("text")
        out.append(
            {
                "mime_path": b.get("mime_path"),
                "content_type": ctype,
                "charset": b.get("charset"),
                "declared_charset": b.get("declared_charset"),
                "disposition": b.get("disposition"),
                "content_id": b.get("content_id"),
                "content_location": b.get("content_location"),
                "byte_size": b.get("byte_size"),
                "text_source": "plain" if ctype == "text/plain" else "plain_text_from_html",
                "plain_text": text or "",
                "referenced_cids": list(b.get("referenced_cids") or []),
            }
        )
    return out


def _attachment_meta(
    row: dict[str, Any],
    attachment_available: AvailabilityCheck,
) -> dict[str, Any]:
    """Attachment metadata plus an explicit download verdict.

    Bytes are never embedded. ``downloadable`` is False both when no storage
    location exists (never persisted / storage failure at ingest) and when the
    stored file is missing on disk; ``not_downloadable_reason`` says which.
    """
    rel = row.get("storage_path")
    stored = bool(rel) and bool(row.get("stored", rel is not None))
    if not rel:
        downloadable = False
        reason = "not_stored"
    elif attachment_available is not None:
        downloadable = bool(attachment_available(rel))
        reason = None if downloadable else "bytes_missing"
    else:
        downloadable = bool(row.get("stored", True))
        reason = None if downloadable else "not_stored"
    return {
        "id": row.get("id"),
        "mime_path": row.get("mime_path"),
        "content_type": row.get("content_type"),
        "charset": row.get("charset"),
        "disposition": row.get("disposition"),
        "filename": row.get("filename"),
        "raw_filename": row.get("raw_filename"),
        "content_id": row.get("content_id"),
        "content_location": row.get("content_location"),
        "byte_size": row.get("byte_size"),
        "sha256": row.get("checksum_sha256"),
        "storage_path": rel,
        "stored": stored,
        "downloadable": downloadable,
        "not_downloadable_reason": reason,
    }


def build_thread_export(
    thread_key: str,
    facts: dict[str, Any],
    *,
    attachment_available: AvailabilityCheck = None,
    raw_available: AvailabilityCheck = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the JSON-serializable thread fact bundle from persisted facts.

    ``facts`` is the read-only result of ``Repository.thread_export_facts``:
    stored header/body/attachment/identifier facts for the thread's members in
    time order. Conflict hints are recomputed here purely for reporting; no
    persisted thread assignment is touched.
    """
    messages = list(facts.get("messages") or [])

    inputs: list[ThreadInput] = []
    for m in messages:
        ts = _utc(m.get("date"))
        inputs.append(
            ThreadInput(
                message_pk=m["id"],
                message_id=m.get("message_id"),
                references=[i["value"] for i in _identifiers(m.get("identifiers") or [], "references")],
                in_reply_to=[i["value"] for i in _identifiers(m.get("identifiers") or [], "in_reply_to")],
                subject=m.get("subject"),
                timestamp=ts.timestamp() if ts else None,
            )
        )
    result = compute_threads(inputs)

    pk_set = {m["id"] for m in messages}

    # --- conflict hints (read-only recomputation) -------------------------
    dup_records: list[dict[str, Any]] = []
    for mid, pks in result.duplicate_ids.items():
        dup_records.append(
            {
                "message_id": _canonical_id(messages, mid),
                "message_id_normalized": mid,
                "message_pks": sorted(pks),
                "message": "duplicate Message-ID claimed by multiple messages; records are retained separately",
            }
        )
    dup_records.sort(key=lambda d: d["message_id_normalized"])

    cycle_records: list[dict[str, Any]] = []
    for cyc in result.cycles:
        nodes = [c for c in cyc if not c.startswith("pk:")]
        node_set = set(nodes)
        involved_pks = sorted(
            m["id"]
            for m in messages
            if m.get("message_id") and m["message_id"].lower() in node_set
        )
        cycle_records.append(
            {
                "cycle": [_canonical_id(messages, c) for c in cyc],
                "cycle_normalized": cyc,
                "message_pks": involved_pks,
                "message": "reference cycle: messages reference each other; order shown is not a causal chain",
            }
        )

    dangling_records: list[dict[str, Any]] = []
    for d in result.dangling_references:
        dangling_records.append(
            {
                "message_pk": d.get("message_pk"),
                "header": d.get("header"),
                "message_id": d.get("message_id"),
                "message": "reference target is not present in the archive",
            }
        )
    dangling_records.sort(key=lambda d: (d["message_pk"] or 0, d["header"] or "", d["message_id"] or ""))

    cycle_nodes = {c for cyc in result.cycles for c in cyc}
    duplicate_id_set = set(result.duplicate_ids)
    dangling_by_pk: dict[int, list[dict[str, Any]]] = {}
    for d in dangling_records:
        dangling_by_pk.setdefault(d["message_pk"], []).append(d)

    # --- per-message records ----------------------------------------------
    out_messages: list[dict[str, Any]] = []
    for m in messages:
        pk = m["id"]
        own_id = (m.get("message_id") or "").lower() or None
        flags: list[str] = []
        if m.get("missing_id") or not m.get("message_id"):
            flags.append("missing_message_id")
        if own_id and own_id in duplicate_id_set:
            flags.append("duplicate_message_id")
        if own_id and own_id in cycle_nodes:
            flags.append("in_reference_cycle")
        if dangling_by_pk.get(pk):
            flags.append("has_dangling_reference")

        rel = m.get("raw_path")
        raw_on_disk = bool(raw_available(rel)) if (rel is not None and raw_available is not None) else (rel is not None)
        raw_eml = {
            "ingest_id": m.get("ingest_id"),
            "raw_sha256": m.get("raw_sha256"),
            "raw_size": None,
            "raw_path": rel,
            "available": bool(rel) and raw_on_disk,
            "not_available_reason": None
            if (rel and raw_on_disk)
            else ("raw_path_missing" if rel is None else "raw_bytes_missing"),
            "mime_tree": m.get("tree_json"),
            "defect_count": m.get("defect_count", 0),
        }
        ingest = m.get("ingest") or {}
        raw_eml["raw_size"] = ingest.get("raw_size")

        out_messages.append(
            {
                "id": pk,
                "ingest_id": m.get("ingest_id"),
                "message_id": m.get("message_id"),
                "subject": m.get("subject"),
                "raw_subject": m.get("raw_subject"),
                "date": _iso(m.get("date")),
                "from": _addresses(m.get("from_json")),
                "to": _addresses(m.get("to_json")),
                "cc": _addresses(m.get("cc_json")),
                "bcc": _addresses(m.get("bcc_json")),
                "reply_to": _addresses(m.get("reply_to_json")),
                "sender": _addresses(m.get("sender_json")),
                "headers": _headers(m.get("headers") or []),
                "references": _identifiers(m.get("identifiers") or [], "references"),
                "in_reply_to": _identifiers(m.get("identifiers") or [], "in_reply_to"),
                "plain_bodies": _plain_bodies(m.get("bodies") or []),
                "attachments": [
                    _attachment_meta(a, attachment_available) for a in m.get("attachments") or []
                ],
                "raw_eml": raw_eml,
                "flags": flags,
                "warnings": _message_warnings(
                    pk,
                    own_id,
                    duplicate_id_set,
                    cycle_nodes,
                    dangling_by_pk.get(pk),
                    m.get("message_id"),
                ),
            }
        )

    member_pks = sorted(pk_set)
    return {
        "schema_version": "thread-fact-export/1",
        "generated_at": _iso(generated_at or datetime.now(timezone.utc)),
        "thread_key": thread_key,
        "root_message_id": result.roots.get(thread_key),
        "member_count": len(out_messages),
        "message_pks": member_pks,
        "messages": out_messages,
        "conflicts": {
            "cycles": cycle_records,
            "duplicate_message_ids": dup_records,
            "dangling_references": dangling_records,
            "members_with_missing_message_id": [
                m["id"] for m in messages if m.get("missing_id") or not m.get("message_id")
            ],
        },
        "notes": [
            "Rebuilt from persisted facts; thread merge results were not modified.",
            "Attachment bytes and HTML markup are not embedded; bodies are extracted plain text only.",
        ],
    }


def _canonical_id(messages: list[dict[str, Any]], node: str) -> str:
    """Resolve a normalized graph node back to an original Message-ID token."""
    if node.startswith("pk:"):
        return node
    for m in messages:
        if m.get("message_id") and m["message_id"].lower() == node:
            return m["message_id"]
    return node


def _message_warnings(
    pk: int,
    own_norm: str | None,
    duplicate_id_set: set[str],
    cycle_nodes: set[str],
    dangling: list[dict[str, Any]] | None,
    own_id: str | None,
) -> list[str]:
    warnings: list[str] = []
    if not own_id:
        warnings.append("Message has no Message-ID; threading relies on reference tokens only.")
    if own_norm and own_norm in duplicate_id_set:
        warnings.append(f"Message-ID {own_id} is also claimed by another message (duplicate conflict).")
    if own_norm and own_norm in cycle_nodes:
        warnings.append("Message participates in a reference cycle; no consistent predecessor exists.")
    for d in dangling or []:
        warnings.append(
            f"{d['header']} references <{d['message_id']}>, which is absent from the archive."
        )
    return warnings


# ---------------------------------------------------------------------------
# Readable text rendering
# ---------------------------------------------------------------------------


def render_thread_export_text(bundle: dict[str, Any]) -> str:
    """Render an export bundle as stable, reviewer-friendly plain text."""
    lines: list[str] = []
    add = lines.append

    add("THREAD FACT EXPORT")
    add("=" * 72)
    add(f"Thread key:        {bundle['thread_key']}")
    add(f"Root Message-ID:   {bundle.get('root_message_id') or '(none / unresolved)'}")
    add(f"Generated at:      {bundle['generated_at']}")
    add(f"Members:           {bundle['member_count']}  pks={bundle['message_pks']}")
    add("Notes:")
    for note in bundle.get("notes", []):
        add(f"  - {note}")

    conflicts = bundle.get("conflicts", {})
    add("")
    add("CONFLICT HINTS")
    add("-" * 72)
    cycles = conflicts.get("cycles", [])
    add(f"Reference cycles: {len(cycles)}")
    for c in cycles:
        add(f"  * cycle: {' -> '.join(c['cycle'])}")
        add(f"    messages pks={c['message_pks']}: {c['message']}")
    dups = conflicts.get("duplicate_message_ids", [])
    add(f"Duplicate Message-IDs: {len(dups)}")
    for d in dups:
        add(f"  * <{d['message_id']}> claimed by messages pks={d['message_pks']}")
        add(f"    {d['message']}")
    dangling = conflicts.get("dangling_references", [])
    add(f"Dangling references: {len(dangling)}")
    for d in dangling:
        add(
            f"  * message pk={d['message_pk']} {d['header']}: <{d['message_id']}> "
            f"({d['message']})"
        )
    missing = conflicts.get("members_with_missing_message_id", [])
    add(f"Members missing Message-ID: {len(missing)} {missing if missing else ''}".rstrip())

    for idx, m in enumerate(bundle.get("messages", []), 1):
        add("")
        add("=" * 72)
        add(f"MESSAGE [{idx}/{bundle['member_count']}]  pk={m['id']}  ingest={m['ingest_id']}")
        add("-" * 72)
        if m["flags"]:
            add("Flags: " + ", ".join(m["flags"]))
        for w in m.get("warnings", []):
            add(f"WARNING: {w}")
        add(f"Message-ID: {('<' + m['message_id'] + '>') if m['message_id'] else '(none)'}")
        add(f"Subject:    {m['subject'] or '(none)'}")
        add(f"Date:       {m['date'] or '(unknown)'}")
        for label, key in (
            ("From", "from"),
            ("To", "to"),
            ("Cc", "cc"),
            ("Bcc", "bcc"),
            ("Reply-To", "reply_to"),
            ("Sender", "sender"),
        ):
            rendered = _render_addresses(m.get(key) or [])
            if rendered:
                add(f"{label:<9}{rendered}")

        add("")
        add("Headers (verbatim facts, decoded + raw):")
        for h in m.get("headers", []):
            add(f"  [{h['ordinal']}] {h['name']}: {h['value']}")
            if h["raw_value"] != h["value"]:
                add(f"        raw: {h['raw_value']}")

        for label, key in (("References", "references"), ("In-Reply-To", "in_reply_to")):
            refs = m.get(key) or []
            if refs:
                add(f"{label}: " + " ".join(f"<{r['value']}>" for r in refs))

        add("")
        add("Body (plain text only; HTML markup never embedded):")
        bodies = m.get("plain_bodies") or []
        if not bodies:
            add("  (no textual body parts)")
        for b in bodies:
            add(
                f"  -- part {b['mime_path']} {b['content_type']} "
                f"charset={b['charset'] or '?'} source={b['text_source']} "
                f"bytes={b['byte_size']}"
            )
            text = b.get("plain_text") or ""
            for line in text.splitlines() or [""]:
                add("    " + line)

        add("")
        add("Attachments (metadata only; bytes never embedded):")
        atts = m.get("attachments") or []
        if not atts:
            add("  (none)")
        for a in atts:
            verdict = "downloadable" if a["downloadable"] else f"NOT downloadable ({a['not_downloadable_reason']})"
            add(
                f"  * id={a['id']} part={a['mime_path']} {a['content_type']} "
                f"name={a['filename']!r} bytes={a['byte_size']} sha256={a['sha256']}"
            )
            add(f"    disposition={a['disposition']} content_id={a['content_id']} stored_path={a['storage_path']}")
            add(f"    {verdict}")

        raw = m.get("raw_eml", {})
        add("")
        add("Original EML summary:")
        add(f"  ingest:        {raw.get('ingest_id')}")
        add(f"  sha256:        {raw.get('raw_sha256')}")
        add(f"  size:          {raw.get('raw_size')}")
        add(f"  stored_path:   {raw.get('raw_path')}")
        status = "available" if raw.get("available") else f"NOT available ({raw.get('not_available_reason')})"
        add(f"  raw eml:       {status}")
        add(f"  defect_count:  {raw.get('defect_count', 0)}")
        tree = raw.get("mime_tree")
        if tree:
            add("  mime tree:")
            for line in _render_tree(tree):
                add("    " + line)

    add("")
    add("END OF THREAD FACT EXPORT")
    return "\n".join(lines) + "\n"


def _render_addresses(addrs: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for a in addrs:
        name = a.get("display_name") or ""
        addr = a.get("address") or ""
        if name and addr:
            parts.append(f'"{name}" <{addr}>')
        elif addr:
            parts.append(addr)
        elif a.get("raw"):
            parts.append(a["raw"])
    return ", ".join(parts)


def _render_tree(node: dict[str, Any], prefix: str = "") -> list[str]:
    line = f"{prefix}{node.get('mime_path')} {node.get('content_type')}"
    if node.get("filename"):
        line += f" name={node['filename']!r}"
    lines = [line]
    for child in node.get("children") or []:
        lines.extend(_render_tree(child, prefix + "  "))
    return lines
