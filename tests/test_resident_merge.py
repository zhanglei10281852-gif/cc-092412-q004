from __future__ import annotations

import json
import threading

import pytest

from app.core.errors import DomainError
from app.core.security import Principal
from app.database import get_connection, transaction
from app.services.resident_merge import ResidentMergeService

ID_CARD_BASE = "11010119900101"


def make_id_card(sequence: int) -> str:
    return f"{ID_CARD_BASE}{sequence:04d}"


def create_resident(client, sequence: int, *, name: str = "张三", phone: str = "13800000000", village: str = "幸福村") -> int:
    response = client.post(
        "/residents",
        json={
            "name": name,
            "id_card": make_id_card(sequence),
            "gender": "男",
            "birth_date": "1990-01-01",
            "phone": phone,
            "address": "幸福路一号",
            "village": village,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_affair(client, applicant_id: int, title: str = "社保材料补录") -> int:
    response = client.post(
        "/affairs",
        json={"title": title, "category": "社保", "applicant_id": applicant_id, "description": "补录材料"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def merge(client, headers, source_id: int, target_id: int, take=None, reason=None, key=None):
    headers = dict(headers)
    if key:
        headers["Idempotency-Key"] = key
    return client.post(
        "/api/resident-merges",
        headers=headers,
        json={"source_id": source_id, "target_id": target_id, "take_from_source": take or [], "reason": reason},
    )


def affair_applicants(client) -> dict[int, int]:
    rows = client.get("/affairs", params={"size": 100}).json()["data"]
    return {row["id"]: row["applicant_id"] for row in rows}


def test_preview_shows_field_and_business_differences(client, admin):
    source = create_resident(client, 1, name="张三", phone="13800000001")
    target = create_resident(client, 2, name="张三", phone="13800000002", village="幸福村")
    source_affair = create_affair(client, source, "源侧事务")
    create_affair(client, target, "目标侧事务")

    response = client.get("/api/resident-merges/preview", headers=admin["headers"], params={"source_id": source, "target_id": target})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mergeable"] is True and body["blockers"] == []
    fields = {item["field"]: item for item in body["fields"]}
    assert fields["id_card"]["differs"] is True
    assert fields["phone"]["differs"] is True
    assert fields["name"]["differs"] is False
    assert fields["id_card"]["source_value"] == make_id_card(1)
    assert fields["id_card"]["target_value"] == make_id_card(2)
    assert [item["id"] for item in body["source_affairs"]] == [source_affair]
    assert len(body["target_affairs"]) == 1


def test_merge_moves_affairs_and_turns_source_into_merge_record(client, admin):
    source = create_resident(client, 3, phone="13800000011")
    target = create_resident(client, 4, phone="13800000022")
    first = create_affair(client, source, "源侧事务一")
    second = create_affair(client, source, "源侧事务二")
    kept = create_affair(client, target, "目标侧事务")

    response = merge(client, admin["headers"], source, target, take=["phone"], reason="证件录入差异，同人两档")
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["source_id"] == source and record["target_id"] == target
    assert record["reason"] == "证件录入差异，同人两档"
    assert sorted(item["id"] for item in record["moved_affairs"]) == sorted([first, second])
    assert record["field_decisions"]["phone"]["decision"] == "take_source"
    assert record["field_decisions"]["phone"]["target_after"] == "13800000011"
    assert record["field_decisions"]["name"]["decision"] == "keep_target"

    # 事务关联全部迁到主档案
    applicants = affair_applicants(client)
    assert applicants[first] == target and applicants[second] == target and applicants[kept] == target

    # 主档案按决策覆盖，未选字段保持原值
    target_row = client.get(f"/residents/{target}").json()
    assert target_row["phone"] == "13800000011"
    assert target_row["id_card"] == make_id_card(4)
    assert target_row["status"] == "active"

    # 源档案成为合并记录，旧编号仍能追到主档案
    source_row = client.get(f"/residents/{source}").json()
    assert source_row["status"] == "merged"
    assert source_row["merged_into_id"] == target
    assert source_row["master_id"] == target

    master = client.get(f"/api/residents/{source}/master", headers=admin["headers"])
    assert master.status_code == 200
    assert master.json()["master_id"] == target
    assert master.json()["chain"] == [source, target]


def test_merge_can_take_id_card_and_tombstones_source(client, admin):
    source = create_resident(client, 5)
    target = create_resident(client, 6)

    response = merge(client, admin["headers"], source, target, take=["id_card"])
    assert response.status_code == 201, response.text

    target_row = client.get(f"/residents/{target}").json()
    assert target_row["id_card"] == make_id_card(5)
    source_row = client.get(f"/residents/{source}").json()
    assert source_row["id_card"] == f"MERGED#{source}"
    assert source_row["status"] == "merged"

    # 合并记录与审计都能还原证件号变化
    record = client.get(f"/api/resident-merges/{response.json()['id']}", headers=admin["headers"]).json()
    assert record["field_decisions"]["id_card"]["source_value"] == make_id_card(5)
    assert record["source_snapshot"]["id_card"] == make_id_card(5)


def test_merged_record_is_read_only_and_cannot_transact(client, admin):
    source = create_resident(client, 7)
    target = create_resident(client, 8)
    assert merge(client, admin["headers"], source, target).status_code == 201

    affair = client.post("/affairs", json={"title": "新业务", "category": "社保", "applicant_id": source})
    assert affair.status_code == 409
    update = client.put(f"/residents/{source}", json={"phone": "13900000000"})
    assert update.status_code == 409
    delete = client.delete(f"/residents/{source}")
    assert delete.status_code == 409

    # 主档案仍可正常办理
    ok = client.post("/affairs", json={"title": "正常业务", "category": "社保", "applicant_id": target})
    assert ok.status_code == 201


def test_merge_rejects_disabled_target_without_partial_changes(client, admin):
    source = create_resident(client, 9)
    target = create_resident(client, 10)
    affair_id = create_affair(client, source)

    disabled = client.post(f"/api/residents/{target}/disable", headers=admin["headers"])
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    response = merge(client, admin["headers"], source, target)
    assert response.status_code == 409
    assert "停用" in response.json()["error"]["message"]

    # 没有任何部分迁移
    assert affair_applicants(client)[affair_id] == source
    assert client.get(f"/residents/{source}").json()["status"] == "active"
    assert client.get("/api/resident-merges", headers=admin["headers"]).json()["total"] == 0

    # 预览也会提示阻碍
    preview = client.get("/api/resident-merges/preview", headers=admin["headers"], params={"source_id": source, "target_id": target})
    assert preview.json()["mergeable"] is False
    assert preview.json()["blockers"]


def test_merge_rejects_already_merged_source_and_merged_target(client, admin):
    first = create_resident(client, 11)
    second = create_resident(client, 12)
    third = create_resident(client, 13)
    assert merge(client, admin["headers"], first, second).status_code == 201

    again = merge(client, admin["headers"], first, third)
    assert again.status_code == 409
    into_merged = merge(client, admin["headers"], third, first)
    assert into_merged.status_code == 409
    assert client.get("/api/resident-merges", headers=admin["headers"]).json()["total"] == 1


def test_merge_chain_resolves_to_final_master(client, admin):
    first = create_resident(client, 14)
    second = create_resident(client, 15)
    third = create_resident(client, 16)
    affair_id = create_affair(client, first)

    assert merge(client, admin["headers"], first, second).status_code == 201
    assert merge(client, admin["headers"], second, third).status_code == 201

    row = client.get(f"/residents/{first}").json()
    assert row["status"] == "merged"
    assert row["master_id"] == third
    master = client.get(f"/api/residents/{first}/master", headers=admin["headers"]).json()
    assert master["chain"] == [first, second, third]
    assert master["master_id"] == third
    # 事务沿链最终落在主档案
    assert affair_applicants(client)[affair_id] == third


def test_merge_cycle_is_rejected(client, admin):
    first = create_resident(client, 17)
    second = create_resident(client, 18)
    # 人为制造脏数据：second 的合并指针指向 first，但状态仍为 active
    connection = get_connection()
    connection.execute("UPDATE residents SET merged_into_id=? WHERE id=?", (first, second))

    response = merge(client, admin["headers"], first, second)
    assert response.status_code == 409
    assert "循环" in response.json()["error"]["message"]
    assert client.get("/api/resident-merges", headers=admin["headers"]).json()["total"] == 0


def test_merge_validates_request(client, admin):
    first = create_resident(client, 19)
    same = merge(client, admin["headers"], first, first)
    assert same.status_code == 422
    other = create_resident(client, 20)
    unknown = merge(client, admin["headers"], first, other, take=["nickname"])
    assert unknown.status_code == 422
    missing = merge(client, admin["headers"], first, 999999)
    assert missing.status_code == 404


def test_merge_requires_merge_permission(client, admin):
    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": "records.reader", "name": "档案查看员", "permission_codes": ["residents.read"]},
    )
    assert role.status_code == 201
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": "reader.one", "password": "Reader!23456", "display_name": "查看员", "role_codes": ["records.reader"]},
    )
    assert user.status_code == 201
    login = client.post("/api/auth/login", json={"username": "reader.one", "password": "Reader!23456", "client_label": "tests"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    source = create_resident(client, 21)
    target = create_resident(client, 22)
    denied = merge(client, headers, source, target)
    assert denied.status_code == 403
    preview = client.get("/api/resident-merges/preview", headers=headers, params={"source_id": source, "target_id": target})
    assert preview.status_code == 200
    anonymous = client.post("/api/resident-merges", json={"source_id": source, "target_id": target, "take_from_source": []})
    assert anonymous.status_code == 401


def test_merge_is_idempotent_with_key(client, admin):
    source = create_resident(client, 23, phone="13800000111")
    target = create_resident(client, 24)
    affair_id = create_affair(client, source)

    first = merge(client, admin["headers"], source, target, take=["phone"], key="merge-req-1")
    assert first.status_code == 201
    replay = merge(client, admin["headers"], source, target, take=["phone"], key="merge-req-1")
    assert replay.status_code == 201
    assert replay.headers.get("x-idempotent-replay") == "true"
    assert replay.json()["id"] == first.json()["id"]

    # 只执行了一次：一条合并记录，事务只迁移一次
    assert client.get("/api/resident-merges", headers=admin["headers"]).json()["total"] == 1
    assert affair_applicants(client)[affair_id] == target

    # 同一幂等键不能用于不同请求
    conflict = merge(client, admin["headers"], source, target, take=["name"], key="merge-req-1")
    assert conflict.status_code == 409


def test_duplicate_merge_without_key_does_not_double_migrate(client, admin):
    source = create_resident(client, 25)
    target = create_resident(client, 26)
    affair_id = create_affair(client, source)

    assert merge(client, admin["headers"], source, target).status_code == 201
    duplicate = merge(client, admin["headers"], source, target)
    assert duplicate.status_code == 409
    assert client.get("/api/resident-merges", headers=admin["headers"]).json()["total"] == 1
    assert affair_applicants(client)[affair_id] == target


def test_concurrent_merges_have_exactly_one_winner(client, admin):
    source = create_resident(client, 27)
    target = create_resident(client, 28)
    affair_id = create_affair(client, source)
    me = client.get("/api/auth/me", headers=admin["headers"]).json()
    principal = Principal(
        user_id=me["user_id"], username="admin", display_name="管理员",
        department_id=None, permissions=frozenset({"*"}), session_id=me["session_id"],
    )
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker():
        barrier.wait()
        try:
            with transaction(immediate=True) as connection:
                ResidentMergeService(connection).merge(principal, source, target, [])
            results.append("ok")
        except DomainError as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["conflict", "ok"]
    assert affair_applicants(client)[affair_id] == target
    assert client.get(f"/residents/{source}").json()["status"] == "merged"
    assert client.get("/api/resident-merges", headers=admin["headers"]).json()["total"] == 1


def test_concurrent_reverse_merges_do_not_create_cycle(client, admin):
    first = create_resident(client, 29)
    second = create_resident(client, 30)
    me = client.get("/api/auth/me", headers=admin["headers"]).json()
    principal = Principal(
        user_id=me["user_id"], username="admin", display_name="管理员",
        department_id=None, permissions=frozenset({"*"}), session_id=me["session_id"],
    )
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(source_id: int, target_id: int):
        barrier.wait()
        try:
            with transaction(immediate=True) as connection:
                ResidentMergeService(connection).merge(principal, source_id, target_id, [])
            results.append("ok")
        except DomainError as exc:
            results.append(exc.code)

    threads = [
        threading.Thread(target=worker, args=(first, second)),
        threading.Thread(target=worker, args=(second, first)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["conflict", "ok"]
    statuses = {client.get(f"/residents/{item}").json()["status"] for item in (first, second)}
    assert statuses == {"active", "merged"}
    # 合并链可正常解析，不存在环
    merged_id = first if client.get(f"/residents/{first}").json()["status"] == "merged" else second
    master = client.get(f"/api/residents/{merged_id}/master", headers=admin["headers"]).json()
    assert master["master_id"] in (first, second)
    assert client.get("/api/resident-merges", headers=admin["headers"]).json()["total"] == 1


def test_audit_trail_explains_every_field_and_association(client, admin):
    source = create_resident(client, 31, phone="13800000333")
    target = create_resident(client, 32, phone="13800000444")
    affair_id = create_affair(client, source)

    response = merge(client, admin["headers"], source, target, take=["phone"], reason="同人两档合并")
    assert response.status_code == 201
    merge_id = response.json()["id"]

    events = client.get("/api/audit", headers=admin["headers"], params={"action": "resident.merge", "size": 100}).json()
    assert events["total"] == 2
    by_resource = {int(item["resource_id"]): item for item in events["data"]}

    target_event = by_resource[target]
    assert json.loads(target_event["before_json"]) == {"phone": "13800000444"}
    assert json.loads(target_event["after_json"]) == {"phone": "13800000333"}
    target_meta = json.loads(target_event["metadata_json"])
    assert target_meta["merge_id"] == merge_id
    assert target_meta["source_id"] == source
    assert target_meta["moved_affair_ids"] == [affair_id]
    assert target_meta["reason"] == "同人两档合并"

    source_event = by_resource[source]
    assert json.loads(source_event["before_json"])["status"] == "active"
    source_after = json.loads(source_event["after_json"])
    assert source_after["status"] == "merged"
    assert source_after["merged_into_id"] == target

    affair_events = client.get("/api/audit", headers=admin["headers"], params={"action": "resident.merge.affairs"}).json()
    assert affair_events["total"] == 1
    assert json.loads(affair_events["data"][0]["before_json"]) == {"affair_ids": [affair_id]}

    # 合并记录本身可完整还原每个字段的取舍
    record = client.get(f"/api/resident-merges/{merge_id}", headers=admin["headers"]).json()
    assert record["source_snapshot"]["phone"] == "13800000333"
    assert record["target_before"]["phone"] == "13800000444"
    assert record["target_after"]["phone"] == "13800000333"
    assert record["field_decisions"]["id_card"]["decision"] == "keep_target"
    assert [item["id"] for item in record["moved_affairs"]] == [affair_id]


def test_disable_and_enable_lifecycle(client, admin):
    resident = create_resident(client, 33)
    disabled = client.post(f"/api/residents/{resident}/disable", headers=admin["headers"])
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    affair = client.post("/affairs", json={"title": "停用期间业务", "category": "社保", "applicant_id": resident})
    assert affair.status_code == 409

    enabled = client.post(f"/api/residents/{resident}/enable", headers=admin["headers"])
    assert enabled.status_code == 200
    assert enabled.json()["status"] == "active"
    affair = client.post("/affairs", json={"title": "恢复后业务", "category": "社保", "applicant_id": resident})
    assert affair.status_code == 201

    # 合并记录不可再变更状态
    source = create_resident(client, 34)
    target = create_resident(client, 35)
    assert merge(client, admin["headers"], source, target).status_code == 201
    assert client.post(f"/api/residents/{source}/disable", headers=admin["headers"]).status_code == 409


def test_resident_list_filters_by_status(client, admin):
    source = create_resident(client, 36)
    target = create_resident(client, 37)
    assert merge(client, admin["headers"], source, target).status_code == 201

    merged = client.get("/residents", params={"status": "merged"}).json()
    assert [row["id"] for row in merged["data"]] == [source]
    active = client.get("/residents", params={"status": "active"}).json()
    assert all(row["status"] == "active" for row in active["data"])
    assert target in [row["id"] for row in active["data"]]
