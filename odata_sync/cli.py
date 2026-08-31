"""CLI: выгрузка из 1С и загрузка обратно.

python -m odata_sync.cli export Catalog_Номенклатура --out nomenclature.json
python -m odata_sync.cli import Catalog_Номенклатура --file data.json --key Code --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .client import ODataClient, ODataError
from .sync import upsert


def _client_from_env() -> ODataClient:
    """Реквизиты доступа берутся только из окружения.

    Пароль от базы 1С не должен попадать ни в аргументы командной строки
    (они видны в истории и в списке процессов), ни тем более в репозиторий.
    """
    try:
        return ODataClient(
            base_url=os.environ["ODATA_URL"],
            username=os.environ["ODATA_USER"],
            password=os.environ["ODATA_PASSWORD"],
        )
    except KeyError as exc:
        raise SystemExit(f"Не задана переменная окружения {exc}. Смотри .env.example") from exc


def cmd_export(args: argparse.Namespace) -> int:
    with _client_from_env() as client:
        records = client.list(args.entity, select=args.select)

    payload = json.dumps(records, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
        print(f"Выгружено записей: {len(records)} → {args.out}")
    else:
        print(payload)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    with open(args.file, encoding="utf-8") as fh:
        records = json.load(fh)

    if not isinstance(records, list):
        raise SystemExit("Ожидался JSON-массив записей")

    with _client_from_env() as client:
        report = upsert(client, args.entity, records, key_field=args.key, dry_run=args.dry_run)

    print(("Пробный прогон — " if args.dry_run else "") + report.summary())
    for error in report.errors:
        print(f"  ошибка: {error}", file=sys.stderr)

    return 0 if report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="odata-sync", description="Обмен данными с 1С по OData")
    parser.add_argument("-v", "--verbose", action="store_true", help="Подробный лог")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="Выгрузить сущность из 1С")
    export.add_argument("entity", help="Например: Catalog_Номенклатура")
    export.add_argument("--select", nargs="*", help="Только эти реквизиты — выгрузка идёт быстрее")
    export.add_argument("--out", help="Файл для записи, по умолчанию — stdout")
    export.set_defaults(func=cmd_export)

    load = sub.add_parser("import", help="Загрузить записи в 1С")
    load.add_argument("entity")
    load.add_argument("--file", required=True, help="JSON-массив записей")
    load.add_argument("--key", required=True, help="Реквизит-бизнес-ключ, например Code")
    load.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать, что будет сделано, ничего не записывая",
    )
    load.set_defaults(func=cmd_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:
        return args.func(args)
    except ODataError as exc:
        print(f"1С вернула ошибку: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
