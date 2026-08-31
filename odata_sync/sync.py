"""Синхронизация записей в 1С.

Главное требование к обмену — идемпотентность. Обмен падает посреди работы
регулярно: сеть, перезапуск сервера 1С, блокировка объекта. Повторный запуск
должен доводить дело до конца, а не создавать дубли справочников.

Идемпотентность обеспечивается ключом сопоставления: запись ищется по бизнес-ключу
(коду, артикулу, номеру), и если найдена — обновляется, а не создаётся заново.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .client import ODataClient, ODataError

logger = logging.getLogger(__name__)


@dataclass
class SyncReport:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (
            f"создано {self.created}, обновлено {self.updated}, "
            f"пропущено {self.skipped}, ошибок {len(self.errors)}"
        )


def _escape_odata_string(value: str) -> str:
    """В OData одинарная кавычка экранируется удвоением.

    Без этого запись с апострофом в названии («Кабель О'Коннор») ломает $filter
    и, в худшем случае, позволяет подмешать своё условие в запрос.
    """
    return value.replace("'", "''")


def find_by_key(
    client: ODataClient, entity: str, key_field: str, key_value: str
) -> dict[str, Any] | None:
    """Найти запись по бизнес-ключу. None, если такой нет."""
    filter_ = f"{key_field} eq '{_escape_odata_string(key_value)}'"
    found = client.list(entity, filter_=filter_)

    if len(found) > 1:
        logger.warning(
            "По ключу %s=%s найдено %s записей, беру первую", key_field, key_value, len(found)
        )
    return found[0] if found else None


def upsert(
    client: ODataClient,
    entity: str,
    records: list[dict[str, Any]],
    key_field: str,
    dry_run: bool = False,
) -> SyncReport:
    """Загрузить записи в 1С: обновить существующие, создать недостающие.

    Записи без заполненного ключа пропускаются — без ключа невозможно
    гарантировать, что повторный запуск не создаст дубль.
    """
    report = SyncReport()

    for record in records:
        key_value = record.get(key_field)
        if not key_value:
            logger.warning("Пропускаю запись без ключа %s: %r", key_field, record)
            report.skipped += 1
            continue

        try:
            existing = find_by_key(client, entity, key_field, str(key_value))

            if existing is None:
                if not dry_run:
                    client.create(entity, record)
                report.created += 1
                logger.info("Создано: %s=%s", key_field, key_value)
            else:
                if not dry_run:
                    client.update(entity, existing["Ref_Key"], record)
                report.updated += 1
                logger.info("Обновлено: %s=%s", key_field, key_value)

        except ODataError as exc:
            # Одна проблемная запись не должна останавливать весь обмен:
            # собираем ошибки и продолжаем, отчёт покажет, что не доехало
            message = f"{key_field}={key_value}: {exc}"
            logger.error("Ошибка синхронизации %s", message)
            report.errors.append(message)

    logger.info("Обмен завершён — %s", report.summary())
    return report
