"""业务操作层（框架无关）：导入、勘误、撤回、版本查询、实体检索、展陈引文。

所有写操作在单个事务内完成；版本查询在一次读事务内取正文与标注，
保证“从同一版返回”。引用的状态与位置随正文变更在同一事务内更新。
"""

from __future__ import annotations

import sqlite3

from . import db, engine


class NotFoundError(Exception):
    pass


class ValidationError(Exception):
    pass


class ConflictError(Exception):
    def __init__(self, message: str, conflicts: list[int]):
        super().__init__(message)
        self.conflicts = conflicts


class StaleVersionError(Exception):
    pass


# ---------------------------------------------------------------- 导入

def import_document(conn: sqlite3.Connection, text: str, entities: list[dict]) -> dict:
    """导入正文及带编号、按字符区间标出的人名/地名。"""
    used: set[int] = set()
    for ent in entities:
        n = ent.get("number")
        if n is not None:
            if n in used:
                raise ValidationError(f"实体编号重复: {n}")
            used.add(n)

    assigned: list[dict] = []
    next_n = 1
    for ent in entities:
        ent = dict(ent)
        if ent.get("number") is None:
            while next_n in used:
                next_n += 1
            ent["number"] = next_n
            used.add(next_n)
        s, t = ent["start"], ent["end"]
        if not (0 <= s < t <= len(text)):
            raise ValidationError(
                f"实体 {ent['number']} 区间 [{s}, {t}) 超出正文范围或为空")
        assigned.append(ent)

    cur = conn.execute(
        "INSERT INTO documents (original_text) VALUES (?)", (text,))
    doc_id = cur.lastrowid
    conn.executemany(
        'INSERT INTO entities (document_id, number, kind, name, start, "end")'
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(doc_id, e["number"], e["kind"], e["name"], e["start"], e["end"])
         for e in assigned],
    )
    db.save_pieces(conn, doc_id, [engine.piece("orig", None, 0, len(text))])
    return {"document_id": doc_id, "revision": 0, "entity_count": len(assigned)}


# ---------------------------------------------------------------- 勘误

def apply_correction(conn: sqlite3.Connection, doc_id: int,
                     s: int, e: int, text: str) -> dict:
    """按当前版把 [s, e) 替换为 text，返回勘误号。"""
    doc = _get_document(conn, doc_id)
    pieces = db.load_pieces(conn, doc_id)
    total = engine.total_length(pieces)
    if not (0 <= s <= e <= total):
        raise ValidationError(f"区间 [{s}, {e}) 超出当前版长度 {total}")
    if s == e and not text:
        raise ValidationError("空操作：区间为空且未提供替换文本")

    number = db.next_correction_number(conn, doc_id)
    kind = "insert" if s == e else ("delete" if not text else "replace")

    new_pieces, deleted, conflicts = engine.apply_edit(pieces, s, e, text, number)
    deleted_text = engine.render(deleted, doc["original_text"],
                                 db.correction_texts(conn, doc_id))

    # 标注受影响判定：用应用前的片段表（与 [s, e) 同一坐标系）
    effects = []
    for ent in _entities(conn, doc_id):
        ca, cb = engine.current_range(pieces, ent["start"], ent["end"])
        if engine.edit_touches_span(s, e, bool(text), ca, cb):
            effects.append(ent["id"])

    # 引用随之移动或停供（与正文变更同一事务）
    _shift_citations(conn, doc_id, s, e, len(text))

    conn.execute(
        "INSERT INTO corrections"
        " (document_id, number, kind, text, range_start, range_end,"
        "  deleted_text, deleted_pieces)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (doc_id, number, kind, text, s, e, deleted_text, db.dumps_pieces(deleted)),
    )
    conn.executemany(
        "INSERT INTO correction_conflicts (document_id, correction_number, target_number)"
        " VALUES (?, ?, ?)",
        [(doc_id, number, t) for t in sorted(conflicts)],
    )
    conn.executemany(
        "INSERT INTO entity_effects (document_id, entity_id, correction_number)"
        " VALUES (?, ?, ?)",
        [(doc_id, ent_id, number) for ent_id in effects],
    )
    db.save_pieces(conn, doc_id, new_pieces)
    revision = _bump_revision(conn, doc_id)
    return {"correction_number": number, "kind": kind, "revision": revision}


def retract_correction(conn: sqlite3.Connection, doc_id: int, number: int) -> dict:
    """按号撤回一笔勘误。

    若仍生效的后续勘误改删了该笔写入的文字、在其中插字，或在该笔纯删除
    留下的接缝插字/跨缝改删，则抛出 ConflictError（携带冲突勘误号），
    当前版保持不变；否则只还原这一笔。
    """
    _get_document(conn, doc_id)
    row = conn.execute(
        "SELECT * FROM corrections WHERE document_id = ? AND number = ?",
        (doc_id, number),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"勘误号不存在: {number}")
    if row["status"] == "retracted":
        raise ValidationError(f"勘误 {number} 已撤回")

    conflicts = db.active_conflicts_against(conn, doc_id, number)
    if conflicts:
        raise ConflictError("存在仍生效的冲突后续勘误，保留现版", conflicts)

    pieces = db.load_pieces(conn, doc_id)
    deleted = db.loads_pieces(row["deleted_pieces"])
    new_pieces = engine.revert_edit(pieces, deleted, number, row["kind"])

    # 撤回把文字还原回现版：若还原内容落在某引文内部，该引用随即停供。
    # 位置取勘误还原前后的当前坐标（还原前该笔写入内容仍在位）。
    rs, re_ = engine.correction_span(pieces, number, row["kind"])
    _shift_citations(conn, doc_id, rs, re_, len(row["deleted_text"]))

    db.save_pieces(conn, doc_id, new_pieces)
    conn.execute(
        "UPDATE corrections SET status = 'retracted'"
        " WHERE document_id = ? AND number = ?",
        (doc_id, number),
    )
    # 该笔不再生效：它对标注的影响随之解除
    conn.execute(
        "DELETE FROM entity_effects WHERE document_id = ? AND correction_number = ?",
        (doc_id, number),
    )
    revision = _bump_revision(conn, doc_id)
    return {"retracted": number, "revision": revision}


# ---------------------------------------------------------------- 引用

def confirm_citation(conn: sqlite3.Connection, doc_id: int,
                     correction_number: int, version_number: int,
                     s: int, e: int) -> dict:
    """确认引文：凭编辑提交的稿号、所见版号与非空左闭右开区间取得引用号。

    版号必须等于当前版（过期的所见版拒绝确认）；区间必须非空且落在现版内。
    """
    doc = _get_document(conn, doc_id)
    if version_number != doc["revision"]:
        raise StaleVersionError(
            f"所见版号 {version_number} 已过期，当前版为 {doc['revision']}")
    if correction_number < 0:
        raise ValidationError("稿号必须是非负整数")

    pieces = db.load_pieces(conn, doc_id)
    total = engine.total_length(pieces)
    if not (0 <= s < e <= total):
        raise ValidationError(
            f"引文区间须非空且落在现版 [0, {total}) 内：收到 [{s}, {e})")

    text = engine.render(pieces, doc["original_text"],
                         db.correction_texts(conn, doc_id))[s:e]
    number = db.next_citation_number(conn, doc_id)
    conn.execute(
        'INSERT INTO citations'
        ' (document_id, number, correction_number, version_number, text, start, "end")'
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (doc_id, number, correction_number, version_number, text, s, e),
    )
    return {
        "citation_number": number,
        "status": "active",
        "text": text,
        "version_number": version_number,
        "document_revision": doc["revision"],
        "draft_number": correction_number,
        "start": s,
        "end": e,
    }


def get_citation(conn: sqlite3.Connection, doc_id: int, number: int) -> dict:
    """查询引用号：有效时返回核准文字、现稿版号与当前区间；停供只报状态。"""
    doc = _get_document(conn, doc_id)
    row = conn.execute(
        "SELECT * FROM citations WHERE document_id = ? AND number = ?",
        (doc_id, number),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"引用号不存在: {number}")
    out = {
        "citation_number": row["number"],
        "status": row["status"],
        "draft_number": row["correction_number"],
        # 现稿版号；确认时所见版号另以 confirmed_version 留存
        "version_number": doc["revision"],
        "document_revision": doc["revision"],
        "confirmed_version": row["version_number"],
    }
    if row["status"] == "active":
        # 仍核准：返回核准文字快照与随正文移动后的当前区间
        out["text"] = row["text"]
        out["start"] = row["start"]
        out["end"] = row["end"]
    else:
        # 停供：只报状态，不再返回文字与区间
        out["suspended_at"] = row["suspended_at"]
    return out


def list_citations(conn: sqlite3.Connection, doc_id: int) -> list[dict]:
    _get_document(conn, doc_id)
    rows = conn.execute(
        "SELECT number FROM citations WHERE document_id = ? ORDER BY number", (doc_id,)
    ).fetchall()
    return [get_citation(conn, doc_id, r["number"]) for r in rows]


# ---------------------------------------------------------------- 查询

def get_version(conn: sqlite3.Connection, doc_id: int) -> dict:
    """从同一版返回正文、有效标注的当前字符区间及待复核标注。"""
    doc = _get_document(conn, doc_id)
    pieces = db.load_pieces(conn, doc_id)
    text = engine.render(pieces, doc["original_text"],
                         db.correction_texts(conn, doc_id))
    valid, pending = [], []
    for ent in _entities(conn, doc_id):
        item = _entity_view(pieces, ent, _effects_map(conn, doc_id))
        (pending if item["affecting_corrections"] else valid).append(item)
    return {
        "document_id": doc_id,
        "revision": doc["revision"],
        "text": text,
        "valid_entities": valid,
        "pending_entities": pending,
    }


def search_entities(conn: sqlite3.Connection, doc_id: int,
                    kind: str | None = None, q: str | None = None) -> list[dict]:
    """实体检索：按类型（人名/地名）和名称子串过滤，返回当前区间与状态。"""
    doc = _get_document(conn, doc_id)
    pieces = db.load_pieces(conn, doc_id)
    effects = _effects_map(conn, doc_id)
    out = []
    for ent in _entities(conn, doc_id):
        if kind and ent["kind"] != kind:
            continue
        if q and q not in ent["name"]:
            continue
        out.append(_entity_view(pieces, ent, effects))
    return out


def list_corrections(conn: sqlite3.Connection, doc_id: int) -> list[dict]:
    _get_document(conn, doc_id)
    rows = conn.execute(
        "SELECT * FROM corrections WHERE document_id = ? ORDER BY number", (doc_id,)
    ).fetchall()
    conflicts = conn.execute(
        "SELECT correction_number, target_number FROM correction_conflicts"
        " WHERE document_id = ? ORDER BY correction_number, target_number",
        (doc_id,),
    ).fetchall()
    cmap: dict[int, list[int]] = {}
    for c in conflicts:
        cmap.setdefault(c["correction_number"], []).append(c["target_number"])
    return [
        {
            "number": r["number"],
            "kind": r["kind"],
            "text": r["text"],
            "range_start": r["range_start"],
            "range_end": r["range_end"],
            "deleted_text": r["deleted_text"],
            "status": r["status"],
            "touches_corrections": cmap.get(r["number"], []),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def list_documents(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT d.id, d.revision, d.created_at,
               length(d.original_text) AS original_length,
               (SELECT COUNT(*) FROM entities e WHERE e.document_id = d.id) AS entity_count,
               (SELECT COUNT(*) FROM corrections c
                 WHERE c.document_id = d.id AND c.status = 'active') AS active_corrections
        FROM documents d ORDER BY d.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 内部

def _shift_citations(conn: sqlite3.Connection, doc_id: int,
                     s: int, e: int, new_len: int) -> None:
    """把正文编辑（当前版 [s, e) -> new_len 字）反映到仍有效的引用上。

    编辑只改到引文外：区间随正文平移；编辑改删引文内的字、或在引文内部
    插字：该引用停供（状态置 suspended，此后只报状态）。两处更新与正文
    变更共处同一事务，保存失败则一并回滚。
    """
    for cit in db.load_active_citations(conn, doc_id):
        moved = engine.transform_span(cit["start"], cit["end"], s, e, new_len)
        if moved is None:
            conn.execute(
                "UPDATE citations SET status = 'suspended',"
                " suspended_at = datetime('now')"
                " WHERE document_id = ? AND number = ?",
                (doc_id, cit["number"]),
            )
        elif moved != (cit["start"], cit["end"]):
            conn.execute(
                'UPDATE citations SET start = ?, "end" = ?'
                " WHERE document_id = ? AND number = ?",
                (moved[0], moved[1], doc_id, cit["number"]),
            )


def _get_document(conn: sqlite3.Connection, doc_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"文档不存在: {doc_id}")
    return row


def _entities(conn: sqlite3.Connection, doc_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        'SELECT id, number, kind, name, start, "end" FROM entities'
        " WHERE document_id = ? ORDER BY number",
        (doc_id,),
    ).fetchall()


def _effects_map(conn: sqlite3.Connection, doc_id: int) -> dict[int, list[int]]:
    rows = conn.execute(
        "SELECT entity_id, correction_number FROM entity_effects"
        " WHERE document_id = ? ORDER BY correction_number",
        (doc_id,),
    ).fetchall()
    out: dict[int, list[int]] = {}
    for r in rows:
        out.setdefault(r["entity_id"], []).append(r["correction_number"])
    return out


def _entity_view(pieces: list[dict], ent: sqlite3.Row,
                 effects: dict[int, list[int]]) -> dict:
    cs, ce = engine.current_range(pieces, ent["start"], ent["end"])
    affecting = effects.get(ent["id"], [])
    return {
        "number": ent["number"],
        "kind": ent["kind"],
        "name": ent["name"],
        "start": cs,
        "end": ce,
        "status": "pending_review" if affecting else "valid",
        "affecting_corrections": affecting,
        "original_start": ent["start"],
        "original_end": ent["end"],
    }


def _bump_revision(conn: sqlite3.Connection, doc_id: int) -> int:
    conn.execute("UPDATE documents SET revision = revision + 1 WHERE id = ?", (doc_id,))
    return conn.execute(
        "SELECT revision FROM documents WHERE id = ?", (doc_id,)).fetchone()["revision"]
