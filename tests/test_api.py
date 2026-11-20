"""API 集成测试：FastAPI TestClient + 临时 SQLite 库。

运行：python3 -m pytest tests/test_api.py
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def import_doc(client, text="张三住在北京市东城区。", entities=None):
    if entities is None:
        entities = [
            {"number": 1, "kind": "person", "name": "张三", "start": 0, "end": 2},
            {"number": 2, "kind": "place", "name": "北京市", "start": 4, "end": 7},
        ]
    payload = {"text": text, "entities": entities}
    r = client.post("/documents", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def correct(client, doc, start, end, text=""):
    r = client.post(f"/documents/{doc}/corrections",
                    json={"start": start, "end": end, "text": text})
    assert r.status_code == 201, r.text
    return r.json()["correction_number"]


def retract(client, doc, number):
    return client.post(f"/documents/{doc}/corrections/{number}/retract")


def version(client, doc):
    r = client.get(f"/documents/{doc}")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 导入与查询

def test_import_and_version_roundtrip(client):
    doc = import_doc(client)
    v = version(client, doc)
    assert v["text"] == "张三住在北京市东城区。"
    assert v["revision"] == 0
    assert v["pending_entities"] == []
    assert [(e["number"], e["start"], e["end"]) for e in v["valid_entities"]] == [
        (1, 0, 2), (2, 4, 7)]


def test_import_entity_number_autofill_and_validation(client):
    r = client.post("/documents", json={"text": "北京市。", "entities": [
        {"kind": "place", "name": "北京市", "start": 0, "end": 3}]})
    assert r.status_code == 201
    assert client.get(f"/documents/{r.json()['document_id']}/entities"
                      ).json()[0]["number"] == 1
    # 区间越界
    r = client.post("/documents", json={"text": "短", "entities": [
        {"kind": "place", "name": "x", "start": 0, "end": 9}]})
    assert r.status_code == 400
    # 编号重复
    r = client.post("/documents", json={"text": "北京市。", "entities": [
        {"number": 1, "kind": "place", "name": "北京", "start": 0, "end": 2},
        {"number": 1, "kind": "place", "name": "市", "start": 2, "end": 3}]})
    assert r.status_code == 400


# ---------------------------------------------------------------- 勘误与撤回

def test_correction_lifecycle(client):
    doc = import_doc(client, "ABCDEFG")
    n = correct(client, doc, 2, 4, "XY")     # ABXYEFG
    assert n == 1
    assert version(client, doc)["text"] == "ABXYEFG"
    r = retract(client, doc, n)
    assert r.status_code == 200
    assert version(client, doc)["text"] == "ABCDEFG"


def test_archivist_retracts_wrong_correction_keeps_later_fix(client):
    """档案员场景：撤回改错的一笔，保留后来改对的文字。"""
    doc = import_doc(client, "张三住在胡同里。")
    # 勘误 1（改错了）：胡同 -> 海滩
    correct(client, doc, 4, 6, "海滩")
    assert version(client, doc)["text"] == "张三住在海滩里。"
    # 勘误 2（后来改对别处）：补“曾”字
    correct(client, doc, 2, 2, "曾")
    assert version(client, doc)["text"] == "张三曾住在海滩里。"
    # 撤回勘误 1：无冲突，只还原这一笔，勘误 2 的成果保留
    r = retract(client, doc, 1)
    assert r.status_code == 200
    assert version(client, doc)["text"] == "张三曾住在胡同里。"


def test_conflict_when_later_correction_modifies_written_text(client):
    doc = import_doc(client, "ABCDEFG")
    correct(client, doc, 3, 3, "XYZ")        # 1: ABCXYZDEFG
    correct(client, doc, 4, 5, "q")          # 2: 改掉了 1 写入的 Y
    r = retract(client, doc, 1)
    assert r.status_code == 409
    assert r.json()["conflicts"] == [2]
    assert version(client, doc)["text"] == "ABCXqZDEFG"   # 保留现版
    assert retract(client, doc, 2).status_code == 200
    r = retract(client, doc, 1)
    assert r.status_code == 200
    assert version(client, doc)["text"] == "ABCDEFG"


def test_conflict_when_insert_within_written_text(client):
    doc = import_doc(client, "ABCDEFG")
    correct(client, doc, 3, 3, "XYZ")        # 1
    correct(client, doc, 4, 4, "q")          # 2: 在 1 写入的文字中插字
    r = retract(client, doc, 1)
    assert r.status_code == 409
    assert r.json()["conflicts"] == [2]


def test_no_conflict_at_boundary_of_written_text(client):
    doc = import_doc(client, "ABCDEFG")
    correct(client, doc, 3, 3, "XYZ")        # 1: ABCXYZDEFG
    correct(client, doc, 3, 3, "q")          # 2: 边界插入，不算“在其中”
    r = retract(client, doc, 1)
    assert r.status_code == 200
    assert version(client, doc)["text"] == "ABCqDEFG"


def test_pure_delete_seam_insert_conflicts(client):
    doc = import_doc(client, "ABCDEFGHIJ")
    correct(client, doc, 2, 5)               # 1: 纯删除 CDE -> ABFGHIJ
    correct(client, doc, 2, 2, "Z")          # 2: 恰在接缝上插字
    r = retract(client, doc, 1)
    assert r.status_code == 409
    assert r.json()["conflicts"] == [2]
    assert retract(client, doc, 2).status_code == 200
    assert retract(client, doc, 1).status_code == 200
    assert version(client, doc)["text"] == "ABCDEFGHIJ"


def test_pure_delete_cross_seam_edit_conflicts(client):
    doc = import_doc(client, "ABCDEFGHIJ")
    correct(client, doc, 2, 5)               # 1: 删 CDE，接缝在 2
    correct(client, doc, 1, 4)               # 2: 跨缝改删 BFG
    r = retract(client, doc, 1)
    assert r.status_code == 409
    assert r.json()["conflicts"] == [2]
    assert version(client, doc)["text"] == "AHIJ"           # 保留现版


def test_pure_delete_one_side_edit_no_conflict(client):
    doc = import_doc(client, "ABCDEFGHIJ")
    correct(client, doc, 2, 5)               # 1: 删 CDE
    correct(client, doc, 2, 4)               # 2: 只删缝后的 FG
    r = retract(client, doc, 1)
    assert r.status_code == 200
    assert version(client, doc)["text"] == "ABCDEHIJ"
    assert retract(client, doc, 2).status_code == 200
    assert version(client, doc)["text"] == "ABCDEFGHIJ"


def test_multiple_conflicts_reported(client):
    doc = import_doc(client, "ABCDEFG")
    correct(client, doc, 3, 3, "XYZ")        # 1
    correct(client, doc, 4, 4, "p")          # 2: 在 XYZ 中插字
    correct(client, doc, 5, 6, "q")          # 3: 改掉 XYZ 的 Y
    r = retract(client, doc, 1)
    assert r.status_code == 409
    assert r.json()["conflicts"] == [2, 3]


def test_correction_validation(client):
    doc = import_doc(client, "ABC", entities=[])
    assert client.post(f"/documents/{doc}/corrections",
                       json={"start": 0, "end": 9, "text": "x"}).status_code == 400
    assert client.post(f"/documents/{doc}/corrections",
                       json={"start": 1, "end": 1, "text": ""}).status_code == 400
    assert retract(client, doc, 99).status_code == 404
    correct(client, doc, 0, 1, "x")
    assert retract(client, doc, 1).status_code == 200
    assert retract(client, doc, 1).status_code == 400   # 重复撤回
    assert client.post("/documents/999/corrections",
                       json={"start": 0, "end": 0, "text": "x"}).status_code == 404


def test_revision_increases_and_is_consistent(client):
    doc = import_doc(client)
    correct(client, doc, 0, 0, "旁白：")
    v = version(client, doc)
    assert v["revision"] == 1
    assert v["text"].startswith("旁白：")
    retract(client, doc, 1)
    assert version(client, doc)["revision"] == 2


# ---------------------------------------------------------------- 标注待复核

def test_entity_pending_on_inner_edit_and_recovers(client):
    doc = import_doc(client)
    correct(client, doc, 5, 6, "津")         # 改“北京市”内的“京”
    v = version(client, doc)
    assert [e["number"] for e in v["valid_entities"]] == [1]
    assert len(v["pending_entities"]) == 1
    pend = v["pending_entities"][0]
    assert pend["number"] == 2 and pend["status"] == "pending_review"
    assert pend["affecting_corrections"] == [1]
    retract(client, doc, 1)
    v = version(client, doc)
    assert v["pending_entities"] == []
    assert [(e["number"], e["start"], e["end"]) for e in v["valid_entities"]] == [
        (1, 0, 2), (2, 4, 7)]


def test_entity_pending_until_all_affecting_retracted(client):
    doc = import_doc(client)
    correct(client, doc, 5, 6, "津")         # 1: 触及标注 2
    correct(client, doc, 6, 7, "城")         # 2: 也触及标注 2
    assert retract(client, doc, 1).status_code == 200
    pend = version(client, doc)["pending_entities"]
    assert len(pend) == 1 and pend[0]["affecting_corrections"] == [2]
    assert retract(client, doc, 2).status_code == 200
    assert version(client, doc)["pending_entities"] == []


def test_entity_pending_on_insert_within_and_delete(client):
    doc = import_doc(client)
    correct(client, doc, 5, 5, "（音）")      # 在“北京市”中插字
    assert len(version(client, doc)["pending_entities"]) == 1
    retract(client, doc, 1)
    correct(client, doc, 4, 7)               # 删掉整个“北京市”
    v = version(client, doc)
    assert len(v["pending_entities"]) == 1
    assert v["text"] == "张三住在东城区。"


def test_entity_boundary_insert_does_not_pending(client):
    doc = import_doc(client)
    correct(client, doc, 4, 4, "曾")          # 标注起点处插入：不算“其中”
    v = version(client, doc)
    assert v["pending_entities"] == []
    place = [e for e in v["valid_entities"] if e["number"] == 2][0]
    assert (place["start"], place["end"]) == (5, 8)   # 区间随版本平移


def test_entity_range_shifts_with_distant_edits(client):
    doc = import_doc(client)
    correct(client, doc, 0, 0, "旁白：")      # 文首插入 3 字
    v = version(client, doc)
    rng = {e["number"]: (e["start"], e["end"]) for e in v["valid_entities"]}
    assert rng[1] == (3, 5)
    assert rng[2] == (7, 10)


# ---------------------------------------------------------------- 实体检索

def test_entity_search_filters(client):
    doc = import_doc(client)
    r = client.get(f"/documents/{doc}/entities", params={"kind": "person"})
    assert [e["name"] for e in r.json()] == ["张三"]
    r = client.get(f"/documents/{doc}/entities", params={"q": "北京"})
    assert [e["number"] for e in r.json()] == [2]
    r = client.get(f"/documents/{doc}/entities/2")
    assert r.json()["name"] == "北京市"
    assert client.get(f"/documents/{doc}/entities/99").status_code == 404


def test_entity_search_reflects_pending_status(client):
    doc = import_doc(client)
    correct(client, doc, 5, 6, "津")
    ent = client.get(f"/documents/{doc}/entities/2").json()
    assert ent["status"] == "pending_review"
    assert ent["affecting_corrections"] == [1]


# ---------------------------------------------------------------- 勘误流水

def test_corrections_listing(client):
    doc = import_doc(client, "ABCDEFG")
    correct(client, doc, 3, 3, "XYZ")
    correct(client, doc, 4, 5, "q")
    rows = client.get(f"/documents/{doc}/corrections").json()
    assert [r["number"] for r in rows] == [1, 2]
    assert rows[0]["kind"] == "insert" and rows[0]["status"] == "active"
    assert rows[1]["touches_corrections"] == [1]
    assert rows[1]["deleted_text"] == "Y"


# ---------------------------------------------------------------- 展陈引文

def confirm(client, doc, draft, version, start, end):
    return client.post(f"/documents/{doc}/citations", json={
        "draft_number": draft, "version_number": version,
        "start": start, "end": end})


def cite(client, doc, draft, start, end, seen=None):
    if seen is None:
        seen = version(client, doc)["revision"]
    r = confirm(client, doc, draft, seen, start, end)
    assert r.status_code == 201, r.text
    return r.json()["citation_number"]


def citation(client, doc, number):
    r = client.get(f"/documents/{doc}/citations/{number}")
    assert r.status_code == 200, r.text
    return r.json()


def test_confirm_citation_returns_number_and_snapshot(client):
    doc = import_doc(client, "张三住在北京市东城区。")
    r = confirm(client, doc, 0, 0, 4, 7)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["citation_number"] == 1
    assert body["text"] == "北京市"
    assert (body["start"], body["end"]) == (4, 7)
    got = citation(client, doc, 1)
    assert got["status"] == "active"
    assert got["text"] == "北京市"
    assert (got["start"], got["end"]) == (4, 7)
    assert got["confirmed_version"] == 0
    assert got["version_number"] == 0     # 现稿版号


def test_confirm_rejects_stale_version(client):
    doc = import_doc(client, "ABCDEFG")
    correct(client, doc, 0, 0, "x")            # revision -> 1
    r = confirm(client, doc, 0, 0, 1, 4)       # 用旧版号 0
    assert r.status_code == 409
    r = confirm(client, doc, 1, 1, 1, 4)       # 现版号 1
    assert r.status_code == 201


def test_confirm_rejects_empty_or_out_of_range_span(client):
    doc = import_doc(client, "ABCDEFG", entities=[])
    assert confirm(client, doc, 0, 0, 2, 2).status_code == 400   # 空区间
    assert confirm(client, doc, 0, 0, 3, 2).status_code == 400   # 反向
    assert confirm(client, doc, 0, 0, 0, 8).status_code == 400   # 越界
    assert confirm(client, doc, 0, 0, 0, 7).status_code == 201
    assert client.get(f"/documents/{doc}/citations/99").status_code == 404



def test_citation_shifts_on_edits_outside_span(client):
    doc = import_doc(client, "张三住在北京市东城区。")
    n = cite(client, doc, 0, 4, 7)                 # “北京市”
    correct(client, doc, 0, 0, "旁白：")            # 文首插 3 字，引文整体右移
    got = citation(client, doc, n)
    assert got["status"] == "active"
    assert got["text"] == "北京市"
    assert (got["start"], got["end"]) == (7, 10)
    correct(client, doc, 0, 3)                     # 又删掉“旁白：”，左移回来
    got = citation(client, doc, n)
    assert (got["start"], got["end"]) == (4, 7)
    assert got["status"] == "active"


def test_boundary_inserts_are_outside_citation(client):
    doc = import_doc(client, "张三住在北京市东城区。")
    n = cite(client, doc, 0, 4, 7)
    correct(client, doc, 4, 4, "老")               # 起点处插字：算引文外，区间右移
    got = citation(client, doc, n)
    assert got["status"] == "active"
    assert (got["start"], got["end"]) == (5, 8)
    correct(client, doc, 8, 8, "的")               # 终点处插字：算引文外，区间不动
    got = citation(client, doc, n)
    assert got["status"] == "active"
    assert (got["start"], got["end"]) == (5, 8)
    assert got["text"] == "北京市"


def test_edit_inside_citation_suspends(client):
    doc = import_doc(client, "张三住在北京市东城区。")
    n = cite(client, doc, 0, 4, 7)
    correct(client, doc, 5, 6, "津")               # 改删引文内“京”
    got = citation(client, doc, n)
    assert got["status"] == "suspended"
    assert "text" not in got and "start" not in got and "end" not in got


def test_insert_inside_citation_suspends(client):
    doc = import_doc(client, "张三住在北京市东城区。")
    n = cite(client, doc, 0, 4, 7)
    correct(client, doc, 5, 5, "（音）")           # 在引文内部插字
    got = citation(client, doc, n)
    assert got["status"] == "suspended"


def test_retract_restoring_text_inside_citation_suspends(client):
    """撤回早先的纯删除，文字还原到引文内部：立即停供。"""
    doc = import_doc(client, "ABCDEFGHIJ", entities=[])
    correct(client, doc, 2, 5)                      # 1: 删 CDE -> ABFGHIJ
    n = cite(client, doc, 1, 1, 4)                  # 引文 “BFG”
    assert retract(client, doc, 1).status_code == 200
    assert version(client, doc)["text"] == "ABCDEFGHIJ"
    got = citation(client, doc, n)
    assert got["status"] == "suspended"
    assert "text" not in got


def test_retract_restoring_text_outside_citation_shifts(client):
    doc = import_doc(client, "ABCDEFGHIJ", entities=[])
    correct(client, doc, 2, 5)                      # 1: 删 CDE -> ABFGHIJ
    n = cite(client, doc, 1, 4, 6)                  # 现版 ABFGHIJ，引文 “HI”
    assert retract(client, doc, 1).status_code == 200
    got = citation(client, doc, n)
    assert got["status"] == "active"
    assert got["text"] == "HI"
    assert (got["start"], got["end"]) == (7, 9)


def test_restored_same_text_requires_new_citation(client):
    """正文改回同样的文字，原号仍停供；须重新确认并发新号。"""
    doc = import_doc(client, "张三住在北京市东城区。")
    n1 = cite(client, doc, 0, 4, 7)
    correct(client, doc, 5, 6, "津")                # 1: 停供
    assert citation(client, doc, n1)["status"] == "suspended"
    correct(client, doc, 5, 6, "京")                # 2: 改回原文
    assert version(client, doc)["text"] == "张三住在北京市东城区。"
    assert citation(client, doc, n1)["status"] == "suspended"
    n2 = cite(client, doc, 2, 4, 7)
    assert n2 == n1 + 1                             # 新号，不复用
    got = citation(client, doc, n2)
    assert got["status"] == "active" and got["text"] == "北京市"


def test_citation_status_survives_unrelated_retraction(client):
    doc = import_doc(client, "ABCDEFG", entities=[])
    correct(client, doc, 0, 0, "x")                 # 1
    n = cite(client, doc, 1, 1, 4)                  # 现版 “xABCDEFG”，引文 “ABC”
    assert citation(client, doc, n)["text"] == "ABC"
    retract(client, doc, 1)                         # 撤回文首插入，引文左移
    got = citation(client, doc, n)
    assert got["status"] == "active"
    assert got["text"] == "ABC"
    assert (got["start"], got["end"]) == (0, 3)


def test_citations_listing(client):
    doc = import_doc(client, "ABCDEFG", entities=[])
    cite(client, doc, 0, 0, 2)
    cite(client, doc, 0, 2, 4)
    rows = client.get(f"/documents/{doc}/citations").json()
    assert [r["citation_number"] for r in rows] == [1, 2]
    assert [r["status"] for r in rows] == ["active", "active"]


def test_failed_save_rolls_back_text_and_citation(client, monkeypatch):
    """正文变更与引用状态/位置同一事务：保存失败则均保留原状。"""
    doc = import_doc(client, "ABCDEFG", entities=[])
    n = cite(client, doc, 0, 2, 4)                  # “CD”
    before = citation(client, doc, n)

    from app import db as dbmod
    real = dbmod.save_pieces

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(dbmod, "save_pieces", boom)
    with pytest.raises(RuntimeError):
        client.post(f"/documents/{doc}/corrections",
                    json={"start": 0, "end": 0, "text": "Z"})
    monkeypatch.setattr(dbmod, "save_pieces", real)

    # 正文、版号、引用位置与状态均未改变
    v = version(client, doc)
    assert v["text"] == "ABCDEFG" and v["revision"] == 0
    assert citation(client, doc, n) == before
