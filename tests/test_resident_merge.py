from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from app.core.errors import DomainError


def create_resident(client, id_card: str, name: str = "张三", phone: str = "13800000000") -> int:
    response = client.post(
        "/residents",
        json={"name": name, "id_card": id_card, "gender": "男", "birth_date": "1990-01-01", "phone": phone, "address": "幸福路一号", "village": "幸福村"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_affair(client, resident_id: int, title: str = "社保材料补录") -> int:
    response = client.post("/affairs", json={"title": title, "category": "社保", "applicant_id": resident_id})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def merge_payload(source: int, target: int, key: str, take: list[str] | None = None, reason: str = "同一居民重复建档") -> dict:
    return {"source_id": source, "target_id": target, "take_from_source": take or [], "reason": reason, "idempotency_key": key}


def do_merge(client, admin, source: int, target: int, key: str, take: list[str] | None = None, reason: str = "同一居民重复建档"):
    return client.post("/api/resident-merges", headers=admin["headers"], json=merge_payload(source, target, key, take, reason))


def test_preview_shows_field_and_business_differences(client, admin):
    source = create_resident(client, "110101199001010011", name="张三", phone="13800000001")
    target = create_resident(client, "110101199001010012", name="张三", phone="13800000002")
    source_affair = create_affair(client, source, "低保申请")
    target_affair = create_affair(client, target, "医保参保")

    preview = client.get(f"/api/resident-merges/preview?source_id={source}&target_id={target}", headers=admin["headers"])
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["mergeable"] is True
    assert body["blockers"] == []
    fields = {item["field"]: item for item in body["fields"]}
    assert fields["id_card"]["different"] is True
    assert fields["id_card"]["decidable"] is True
    assert fields["phone"]["different"] is True
    assert fields["name"]["different"] is False
    assert fields["gender"]["decidable"] is False
    assert [item["id"] for item in body["source_affairs"]] == [source_affair]
    assert [item["id"] for item in body["target_affairs"]] == [target_affair]


def test_merge_applies_field_choices_and_moves_affairs(client, admin):
    source = create_resident(client, "110101199001010021", phone="13800000011")
    target = create_resident(client, "110101199001010022", phone="13800000022")
    affair_ids = [create_affair(client, source, f"事务{i}") for i in range(2)]

    response = do_merge(client, admin, source, target, "merge-basic-1", take=["phone"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["replayed"] is False
    assert body["moved_affair_ids"] == affair_ids
    assert body["field_changes"] == [{"field": "phone", "label": "联系电话", "before": "13800000022", "after": "13800000011"}]

    master = client.get(f"/residents/{target}").json()
    assert master["phone"] == "13800000011"
    assert master["status"] == "active"

    retired = client.get(f"/residents/{source}").json()
    assert retired["status"] == "merged"
    assert retired["merged_into_id"] == target
    assert retired["canonical_id"] == target

    for affair_id in affair_ids:
        assert client.get(f"/affairs/{affair_id}").json()["applicant_id"] == target

    detail = client.get(f"/api/resident-merges/{body['merge_id']}", headers=admin["headers"]).json()
    assert detail["source_id"] == source
    assert detail["target_id"] == target
    assert detail["reason"] == "同一居民重复建档"
    assert detail["moved_affair_ids"] == affair_ids
    assert detail["field_changes"][0]["field"] == "phone"
    assert detail["operator_name"]


def test_id_card_move_keeps_old_number_traceable(client, admin):
    source = create_resident(client, "110101199001010031")
    target = create_resident(client, "110101199001010032")

    response = do_merge(client, admin, source, target, "merge-idcard-1", take=["id_card"])
    assert response.status_code == 201, response.text

    master = client.get(f"/residents/{target}").json()
    assert master["id_card"] == "110101199001010031"
    retired = client.get(f"/residents/{source}").json()
    assert retired["id_card"] == f"MERGED-{source}"

    traced = client.get("/api/resident-merges/resolve?id_card=110101199001010031", headers=admin["headers"])
    assert traced.status_code == 200
    assert traced.json()["canonical_id"] == target

    duplicate = client.post(
        "/residents",
        json={"name": "李四", "id_card": "110101199001010031", "gender": "男", "birth_date": "1991-02-02", "address": "别处", "village": "幸福村"},
    )
    assert duplicate.status_code == 409


def test_merged_archive_is_closed_for_business(client, admin):
    source = create_resident(client, "110101199001010041")
    target = create_resident(client, "110101199001010042")
    assert do_merge(client, admin, source, target, "merge-close-1").status_code == 201

    affair = client.post("/affairs", json={"title": "新业务", "category": "社保", "applicant_id": source})
    assert affair.status_code == 409
    update = client.put(f"/residents/{source}", json={"phone": "13900000000"})
    assert update.status_code == 409
    delete = client.delete(f"/residents/{source}")
    assert delete.status_code == 409

    still_active = client.post("/affairs", json={"title": "正常业务", "category": "社保", "applicant_id": target})
    assert still_active.status_code == 201


def test_old_number_resolves_to_master_across_chain(client, admin):
    first = create_resident(client, "110101199001010051")
    second = create_resident(client, "110101199001010052")
    third = create_resident(client, "110101199001010053")
    affair_id = create_affair(client, first)

    assert do_merge(client, admin, first, second, "merge-chain-1").status_code == 201
    assert do_merge(client, admin, second, third, "merge-chain-2").status_code == 201

    resolved = client.get(f"/api/resident-merges/resolve?resident_id={first}", headers=admin["headers"])
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert [item["id"] for item in body["chain"]] == [first, second, third]
    assert body["canonical_id"] == third
    assert body["canonical"]["status"] == "active"
    assert client.get(f"/affairs/{affair_id}").json()["applicant_id"] == third

    retired = client.get(f"/residents/{first}").json()
    assert retired["canonical_id"] == third


def test_merge_cycle_and_merged_target_are_rejected(client, admin):
    first = create_resident(client, "110101199001010061")
    second = create_resident(client, "110101199001010062")
    third = create_resident(client, "110101199001010063")
    assert do_merge(client, admin, first, second, "merge-cycle-1").status_code == 201

    into_merged = do_merge(client, admin, third, first, "merge-cycle-2")
    assert into_merged.status_code == 409
    assert "主档案" in into_merged.json()["error"]["message"]

    repeat_source = do_merge(client, admin, first, third, "merge-cycle-3")
    assert repeat_source.status_code == 409

    preview = client.get(f"/api/resident-merges/preview?source_id={third}&target_id={first}", headers=admin["headers"])
    assert preview.json()["mergeable"] is False
    assert preview.json()["blockers"]

    # 被拒绝的合并不产生任何迁移
    assert client.get("/api/resident-merges", headers=admin["headers"]).json()["total"] == 1
    assert client.get(f"/residents/{third}").json()["status"] == "active"


def test_duplicate_request_replays_without_double_migration(client, admin):
    source = create_resident(client, "110101199001010071")
    target = create_resident(client, "110101199001010072")
    create_affair(client, source)

    first = do_merge(client, admin, source, target, "merge-idem-1", take=["phone"])
    assert first.status_code == 201
    replay = do_merge(client, admin, source, target, "merge-idem-1", take=["phone"])
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    assert replay.json()["merge_id"] == first.json()["merge_id"]

    assert client.get("/api/resident-merges", headers=admin["headers"]).json()["total"] == 1
    affairs = client.get(f"/affairs?applicant_id={target}").json()
    assert affairs["total"] == 1

    conflict = do_merge(client, admin, source, target, "merge-idem-1", take=["address"])
    assert conflict.status_code == 409


def test_concurrent_merges_serialize_without_partial_migration(client, admin):
    source = create_resident(client, "110101199001010081")
    target_a = create_resident(client, "110101199001010082")
    target_b = create_resident(client, "110101199001010083")
    affair_id = create_affair(client, source)

    from app.core.security import Principal
    from app.database import close_connection, transaction
    from app.services.merge import ResidentMergeService

    principal = Principal(
        user_id=1, username="admin", display_name="管理员", department_id=None,
        permissions=frozenset({"residents.merge"}), session_id=1,
    )

    def attempt(target_id: int, key: str) -> int:
        try:
            with transaction(immediate=True) as connection:
                stored = ResidentMergeService(connection).execute(
                    principal, merge_payload(source, target_id, key)
                )
            return stored.status_code
        except DomainError as exc:
            return exc.status_code
        finally:
            close_connection()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda args: attempt(*args), [(target_a, "merge-race-1"), (target_b, "merge-race-2")]))

    assert sorted(outcomes) == [201, 409]
    assert client.get("/api/resident-merges", headers=admin["headers"]).json()["total"] == 1
    winner = target_a if outcomes[0] == 201 else target_b
    loser = target_b if winner == target_a else target_a
    assert client.get(f"/residents/{source}").json()["merged_into_id"] == winner
    assert client.get(f"/residents/{loser}").json()["status"] == "active"
    assert client.get(f"/affairs/{affair_id}").json()["applicant_id"] == winner


def test_audit_trail_restores_every_field_and_association_change(client, admin):
    source = create_resident(client, "110101199001010091", phone="13800000091")
    target = create_resident(client, "110101199001010092", phone="13800000092")
    affair_ids = [create_affair(client, source, f"历史事务{i}") for i in range(2)]

    merged = do_merge(client, admin, source, target, "merge-audit-1", take=["phone"], reason="证件录入差异")
    assert merged.status_code == 201
    merge_id = merged.json()["merge_id"]
    correlation_id = f"resident-merge:{merge_id}"

    fields_events = client.get("/api/audit?action=resident.merge.fields", headers=admin["headers"]).json()["data"]
    assert len(fields_events) == 1
    fields_event = fields_events[0]
    assert fields_event["resource_id"] == str(target)
    assert json.loads(fields_event["before_json"]) == {"phone": "13800000092"}
    assert json.loads(fields_event["after_json"]) == {"phone": "13800000091"}
    assert json.loads(fields_event["metadata_json"])["reason"] == "证件录入差异"
    assert fields_event["correlation_id"] == correlation_id

    retire_events = client.get("/api/audit?action=resident.merge.retire", headers=admin["headers"]).json()["data"]
    assert len(retire_events) == 1
    retire_event = retire_events[0]
    assert retire_event["resource_id"] == str(source)
    assert json.loads(retire_event["before_json"])["status"] == "active"
    assert json.loads(retire_event["after_json"]) == {"status": "merged", "id_card": "110101199001010091", "merged_into_id": target}
    assert retire_event["correlation_id"] == correlation_id

    move_events = client.get("/api/audit?action=resident.merge.move_affair", headers=admin["headers"]).json()["data"]
    assert {int(event["resource_id"]) for event in move_events} == set(affair_ids)
    for event in move_events:
        assert json.loads(event["before_json"]) == {"applicant_id": source}
        assert json.loads(event["after_json"]) == {"applicant_id": target}
        assert event["correlation_id"] == correlation_id


def test_merge_requires_permission(client, admin):
    source = create_resident(client, "110101199001010101")
    target = create_resident(client, "110101199001010102")

    anonymous = client.post("/api/resident-merges", json=merge_payload(source, target, "merge-perm-0"))
    assert anonymous.status_code == 401

    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": "records.reader", "name": "档案查看员", "permission_codes": ["residents.read"]},
    )
    assert role.status_code == 201
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": "merge.viewer", "password": "Viewer!23456", "display_name": "查看员", "role_codes": ["records.reader"]},
    )
    assert user.status_code == 201
    login = client.post("/api/auth/login", json={"username": "merge.viewer", "password": "Viewer!23456", "client_label": "tests"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    denied = client.post("/api/resident-merges", headers=headers, json=merge_payload(source, target, "merge-perm-1"))
    assert denied.status_code == 403
    allowed_preview = client.get(f"/api/resident-merges/preview?source_id={source}&target_id={target}", headers=headers)
    assert allowed_preview.status_code == 200
    assert client.get(f"/residents/{source}").json()["status"] == "active"


def test_merge_validation_errors(client, admin):
    source = create_resident(client, "110101199001010111")
    target = create_resident(client, "110101199001010112")

    same = client.post("/api/resident-merges", headers=admin["headers"], json=merge_payload(source, source, "merge-val-1"))
    assert same.status_code == 422

    unknown_field = client.post("/api/resident-merges", headers=admin["headers"], json=merge_payload(source, target, "merge-val-2", take=["gender"]))
    assert unknown_field.status_code == 422

    missing = do_merge(client, admin, source, 999999, "merge-val-3")
    assert missing.status_code == 404

    missing_preview = client.get(f"/api/resident-merges/preview?source_id={source}&target_id=999999", headers=admin["headers"])
    assert missing_preview.status_code == 404

    bad_resolve = client.get("/api/resident-merges/resolve", headers=admin["headers"])
    assert bad_resolve.status_code == 422
    unknown_card = client.get("/api/resident-merges/resolve?id_card=110101199001019999", headers=admin["headers"])
    assert unknown_card.status_code == 404
