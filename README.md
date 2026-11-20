# 口述史转录稿勘误供数 API

FastAPI + SQLite 实现的转录稿勘误版本管理服务。支持：导入正文与带编号的人名/
地名标注、按当前版应用勘误（左闭右开区间替换，含插入与删除）、按勘误号撤回
（带冲突检测）、标注待复核跟踪、实体检索，以及同一版本的一致性查询。展陈
引文：凭编辑提交稿号与所见版号确认引文，取得引用号，此后按号取用核准的
口述史原话。

## 启动

```bash
docker compose up --build
# API 位于 http://localhost:8000 ，交互文档 http://localhost:8000/docs
```

SQLite 数据文件落在命名卷 `app_data`（容器内 `/data/app.db`）。

本地开发（无需 Docker）：

```bash
pip install -r requirements-dev.txt
DB_PATH=./app.db uvicorn app.main:app --reload
python3 -m pytest tests/          # 全部测试
python3 -m unittest discover -s tests -p 'test_engine.py'   # 仅引擎（零依赖）
```

## 核心概念

- **勘误**：把当前版的 `[start, end)` 左闭右开区间替换为 `text`。
  `start == end` 为纯插入，`text` 为空为纯删除，其余为替换。应用后返回
  文档内递增的**勘误号**。
- **撤回**：按勘误号撤回。若仍生效的后续勘误存在以下任一情况，撤回被拒绝，
  返回 `409` 与冲突勘误号列表，**现版保持不变**：
  1. 改删了该笔写入的文字；
  2. 在该笔写入的文字中插字；
  3. 该笔为纯删除时，在其留下的接缝上插字，或跨缝改删。
  
  否则只还原这一笔，其他勘误（包括更晚的）的成果原样保留。冲突勘误本身
  撤回后，原勘误即可撤回。
- **标注待复核**：勘误改删了某标注区间内的文字，或在其中插字，该标注即转为
  待复核；影响它的勘误全部撤回后自动恢复有效。落在标注边界上的插入不算。
- **版本查询**：正文、有效标注的当前字符区间、待复核标注在同一次读事务中
  返回，并附单调递增的 `revision`，保证三者来自同一版。
- **展陈引文**：编辑凭**提交稿号**（`draft_number`，即某笔勘误号；未改动
  原文的稿可填 `0`）、**所见版号**（`version_number`，须等于当前
  `revision`，过期则 `409` 拒绝确认）和非空左闭右开区间确认引文，取得
  文档内递增的**引用号**，同时留存核准文字快照与确认时的区间。
  - 之后的勘误只改到引文外（含边界插字）时，原号继续有效，区间随正文移动；
  - 改删引文内的字、或在引文内部插字时，原号**停供**（`suspended`），
    查询只返回状态；
  - 撤回早先的纯删除、把文字还原到引文内部，同样立即停供；
  - 停供不可恢复：即使正文改回同样的文字，也须重新确认并发新号；
  - 正文变更与引用的状态/区间在同一事务内生效，保存失败则一并回滚。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/documents` | 导入正文与实体标注 |
| GET | `/documents` | 文档列表 |
| GET | `/documents/{doc}` | 版本查询：正文 + 有效标注区间 + 待复核标注 |
| POST | `/documents/{doc}/corrections` | 应用勘误，返回勘误号 |
| POST | `/documents/{doc}/corrections/{n}/retract` | 按号撤回（冲突 409） |
| GET | `/documents/{doc}/corrections` | 勘误流水（含状态与触及关系） |
| GET | `/documents/{doc}/entities?kind=&q=` | 实体检索（按人名/地名、名称子串） |
| GET | `/documents/{doc}/entities/{number}` | 单个实体 |
| POST | `/documents/{doc}/citations` | 确认引文，返回引用号 |
| GET | `/documents/{doc}/citations` | 引用号列表 |
| GET | `/documents/{doc}/citations/{number}` | 按号取用（有效/停供） |

## 示例

```bash
# 1. 导入正文与标注（人名“张三”[0,2)，地名“北京市”[4,7)）
curl -X POST localhost:8000/documents -H 'Content-Type: application/json' -d '{
  "text": "张三住在北京市东城区。",
  "entities": [
    {"number": 1, "kind": "person", "name": "张三", "start": 0, "end": 2},
    {"number": 2, "kind": "place", "name": "北京市", "start": 4, "end": 7}
  ]}'
# => {"document_id": 1, "revision": 0, "entity_count": 2}

# 2. 应用勘误：把“京”改为“津”（触及地名标注）
curl -X POST localhost:8000/documents/1/corrections \
  -H 'Content-Type: application/json' \
  -d '{"start": 5, "end": 6, "text": "津"}'
# => {"correction_number": 1, "kind": "replace", "revision": 1}

# 3. 版本查询：正文已更新，地名标注转为待复核
curl localhost:8000/documents/1
# => {"text": "张三住在津市东城区。", ...,
#     "valid_entities": [{"number": 1, ...}],
#     "pending_entities": [{"number": 2, "affecting_corrections": [1], ...}]}

# 4. 撤回勘误 1：无冲突，只还原这一笔，标注恢复有效
curl -X POST localhost:8000/documents/1/corrections/1/retract
# => {"retracted": 1, "revision": 2}

# 5. 实体检索
curl 'localhost:8000/documents/1/entities?kind=place&q=北京'
```

冲突示例：勘误 1 插入 `XYZ`，勘误 2 改掉了其中的 `Y`，此时撤回勘误 1：

```bash
curl -X POST localhost:8000/documents/1/corrections/1/retract
# => 409 {"detail": "存在仍生效的冲突后续勘误，保留现版", "conflicts": [2]}
```

展陈引文示例：

```bash
# 确认引文：稿号 0（未改动原文）、所见版号 0、取“北京市”[4,7)
curl -X POST localhost:8000/documents/1/citations \
  -H 'Content-Type: application/json' \
  -d '{"draft_number": 0, "version_number": 0, "start": 4, "end": 7}'
# => 201 {"citation_number": 1, "status": "active", "text": "北京市",
#         "start": 4, "end": 7, ...}

# 版号过期：拒绝确认
curl -X POST localhost:8000/documents/1/citations \
  -H 'Content-Type: application/json' \
  -d '{"draft_number": 0, "version_number": 0, "start": 4, "end": 7}'
# => 409 {"detail": "所见版号 0 已过期，当前版为 1"}

# 按号取用：有效时返回核准文字、现稿版号与当前区间
curl localhost:8000/documents/1/citations/1
# => {"citation_number": 1, "status": "active", "text": "北京市",
#     "start": 4, "end": 7, "document_revision": 0, ...}

# 引文内文字被改删后：只报停供状态
curl localhost:8000/documents/1/citations/1
# => {"citation_number": 1, "status": "suspended", "suspended_at": "...", ...}
```

## 实现要点

当前版正文用**片段表**表示：每个片段引用原文的一段、某笔勘误写入文本的一
段，或纯删除勘误留下的零长**接缝**标记。应用勘误时在片段表上切分/替换，
并记录本笔删掉的片段（供撤回还原）与触及的更早勘误（供冲突检测）。冲突
规则保证：一笔勘误可撤回时，它写入的片段必然完整连续、接缝必然在位，因此
撤回是精确的整体还原，不影响其他勘误的成果。标注区间按原文坐标存储，查询
时经片段表映射到当前版坐标。

引文区间按**当前版坐标**存储，每次应用勘误/撤回都在同一事务里用区间变换
（`engine.transform_span`）更新：编辑完全落在区间外则平移；与区间相交或
在区间内部插字则把该引用置为停供，仅保留状态与审计字段，不再返回文字。

```
app/
  engine.py   # 片段表核心算法（纯 Python，零依赖）
  db.py       # SQLite 模式与存取
  service.py  # 业务操作（导入/勘误/撤回/查询/检索/引文）
  main.py     # FastAPI 路由与事务
tests/
  test_engine.py  # 引擎单元测试（标准库即可运行）
  test_api.py     # API 集成测试
```
