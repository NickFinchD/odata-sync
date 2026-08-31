"""Тесты обмена с 1С.

Живая база 1С для тестов не нужна: OData — это обычный HTTP, и он мокается
через respx. Это принципиально: тесты, требующие развёрнутой 1С,
на практике не запускает никто.
"""

import httpx
import pytest
import respx

from odata_sync.client import ODataClient, ODataError
from odata_sync.sync import _escape_odata_string, find_by_key, upsert

BASE = "http://1c.local/base/odata/standard.odata"
ENTITY = "Catalog_Номенклатура"


@pytest.fixture
def client():
    with ODataClient(BASE, "user", "pass", max_retries=2) as c:
        yield c


def _values(*items):
    return httpx.Response(200, json={"value": list(items)})


@respx.mock
def test_list_returns_records(client):
    respx.get(f"{BASE}/{ENTITY}").mock(return_value=_values({"Code": "001"}, {"Code": "002"}))

    records = client.list(ENTITY)

    assert [r["Code"] for r in records] == ["001", "002"]


@respx.mock
def test_list_walks_all_pages(client):
    """Полная страница означает, что данные не кончились — нужен следующий запрос."""
    full_page = [{"Code": str(i)} for i in range(500)]
    respx.get(f"{BASE}/{ENTITY}").mock(
        side_effect=[
            httpx.Response(200, json={"value": full_page}),
            _values({"Code": "last"}),
        ]
    )

    records = client.list(ENTITY)

    assert len(records) == 501
    assert records[-1]["Code"] == "last"


@respx.mock
def test_client_raises_on_400(client):
    respx.get(f"{BASE}/{ENTITY}").mock(return_value=httpx.Response(400, text="Плохой запрос"))

    with pytest.raises(ODataError, match="400"):
        client.list(ENTITY)


@respx.mock
def test_client_retries_on_503(client):
    route = respx.get(f"{BASE}/{ENTITY}").mock(
        side_effect=[httpx.Response(503), _values({"Code": "001"})]
    )

    assert len(client.list(ENTITY)) == 1
    assert route.call_count == 2


@respx.mock
def test_find_by_key_returns_none_when_absent(client):
    respx.get(f"{BASE}/{ENTITY}").mock(return_value=_values())

    assert find_by_key(client, ENTITY, "Code", "999") is None


@respx.mock
def test_upsert_creates_missing(client):
    respx.get(f"{BASE}/{ENTITY}").mock(return_value=_values())
    create = respx.post(f"{BASE}/{ENTITY}").mock(
        return_value=httpx.Response(201, json={"Ref_Key": "guid-1"})
    )

    report = upsert(client, ENTITY, [{"Code": "001", "Description": "Болт"}], key_field="Code")

    assert report.created == 1
    assert report.updated == 0
    assert report.ok
    assert create.called


@respx.mock
def test_upsert_updates_existing(client):
    """Повторный запуск не создаёт дубль — в этом весь смысл обмена."""
    respx.get(f"{BASE}/{ENTITY}").mock(return_value=_values({"Ref_Key": "guid-1", "Code": "001"}))
    patch = respx.patch(f"{BASE}/{ENTITY}(guid'guid-1')").mock(
        return_value=httpx.Response(200, json={"Ref_Key": "guid-1"})
    )

    report = upsert(client, ENTITY, [{"Code": "001", "Description": "Болт"}], key_field="Code")

    assert report.created == 0
    assert report.updated == 1
    assert patch.called


@respx.mock
def test_upsert_skips_records_without_key(client):
    report = upsert(client, ENTITY, [{"Description": "Без кода"}], key_field="Code")

    assert report.skipped == 1
    assert report.created == 0


@respx.mock
def test_upsert_continues_after_error(client):
    """Одна проблемная запись не должна останавливать весь обмен."""
    respx.get(f"{BASE}/{ENTITY}").mock(return_value=_values())
    respx.post(f"{BASE}/{ENTITY}").mock(
        side_effect=[
            httpx.Response(400, text="Не заполнено обязательное поле"),
            httpx.Response(201, json={"Ref_Key": "guid-2"}),
        ]
    )

    report = upsert(
        client,
        ENTITY,
        [{"Code": "bad"}, {"Code": "good"}],
        key_field="Code",
    )

    assert report.created == 1
    assert len(report.errors) == 1
    assert not report.ok
    assert "Code=bad" in report.errors[0]


@respx.mock
def test_dry_run_does_not_write(client):
    respx.get(f"{BASE}/{ENTITY}").mock(return_value=_values())
    create = respx.post(f"{BASE}/{ENTITY}")

    report = upsert(client, ENTITY, [{"Code": "001"}], key_field="Code", dry_run=True)

    assert report.created == 1
    assert not create.called


@pytest.mark.parametrize(
    "value,expected",
    [("Болт", "Болт"), ("О'Коннор", "О''Коннор"), ("a'b'c", "a''b''c")],
)
def test_escape_odata_string(value, expected):
    """Апостроф в значении не должен ломать $filter и подмешивать условие."""
    assert _escape_odata_string(value) == expected
