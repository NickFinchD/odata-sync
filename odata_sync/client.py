"""Клиент к OData-интерфейсу 1С:Предприятие.

1С публикует стандартный OData по адресу вида
    http://server/base/odata/standard.odata/
и отдаёт справочники и документы как Catalog_Номенклатура, Document_ЗаказПокупателя.

Особенности, из-за которых нельзя взять готовую OData-библиотеку:
1С возвращает свои коды ошибок, использует Basic-аутентификацию,
а выборку режет постранично только через $top/$skip.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Больше 1000 записей за раз 1С отдаёт неохотно и часто отваливается по таймауту
PAGE_SIZE = 500
RETRY_STATUSES = {429, 500, 502, 503, 504}


class ODataError(RuntimeError):
    """Ошибка на стороне 1С."""


class ODataClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        # 1С чувствительна к завершающему слешу — нормализуем на входе
        self.base_url = base_url.rstrip("/") + "/"
        self._max_retries = max_retries
        self._client = httpx.Client(
            base_url=self.base_url,
            auth=(username, password),
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    def __enter__(self) -> ODataClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if response.status_code not in RETRY_STATUSES:
                    if response.status_code >= 400:
                        raise ODataError(
                            f"1С ответила {response.status_code} на {method} {path}: "
                            f"{response.text[:300]}"
                        )
                    # DELETE возвращает 204 без тела
                    return response.json() if response.content else {}
                last_error = ODataError(f"1С ответила {response.status_code} на {path}")

            logger.warning("Попытка %s для %s не удалась", attempt, path)

        assert last_error is not None
        raise last_error

    def list(
        self,
        entity: str,
        select: list[str] | None = None,
        filter_: str | None = None,
    ) -> list[dict[str, Any]]:
        """Выгрузить все записи сущности, разбирая постраничность самостоятельно.

        $select указывать стоит почти всегда: справочник номенклатуры с полным
        набором реквизитов выгружается в разы дольше, чем с пятью нужными полями.
        """
        collected: list[dict[str, Any]] = []
        skip = 0

        while True:
            params: dict[str, Any] = {
                "$format": "json",
                "$top": PAGE_SIZE,
                "$skip": skip,
            }
            if select:
                params["$select"] = ",".join(select)
            if filter_:
                params["$filter"] = filter_

            payload = self._request("GET", entity, params=params)
            batch = payload.get("value", [])
            collected.extend(batch)

            # Последняя страница короче размера окна — значит данные кончились
            if len(batch) < PAGE_SIZE:
                break
            skip += PAGE_SIZE

        logger.info("Из %s выгружено записей: %s", entity, len(collected))
        return collected

    def get(self, entity: str, guid: str) -> dict[str, Any]:
        """Одна запись по GUID."""
        return self._request("GET", f"{entity}(guid'{quote(guid)}')", params={"$format": "json"})

    def create(self, entity: str, data: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", entity, json=data, params={"$format": "json"})

    def update(self, entity: str, guid: str, data: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"{entity}(guid'{quote(guid)}')",
            json=data,
            params={"$format": "json"},
        )
