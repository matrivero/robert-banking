
#!/usr/bin/env python3
"""Publica SOLO el saldo contable de una cuenta en un Google Sheet privado.

Se ejecuta DESPUÉS de sync_balances.py como ExecStartPost de systemd.
No consulta Enable Banking, no accede a las claves bancarias y no escribe SQLite.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Reutilizamos la misma lectura y validación de alias que sync_balances.py.
from sync_balances import load_account_aliases

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
MADRID = ZoneInfo("Europe/Madrid")
SHEETS_SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]
SHARED_ACCOUNT_LABEL = "Cuenta compartida"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Falta configurar {name} en .env")
    return value


def db_file() -> Path:
    path = Path(
        os.getenv("EB_DB", str(ROOT / "data" / "banking.db"))
    ).expanduser()

    if not path.is_absolute():
        path = ROOT / path

    if not path.is_file():
        raise FileNotFoundError(f"No existe SQLite: {path}")

    return path


def get_shared_identity() -> str:
    """Localiza el hash de la cuenta compartida en account_aliases.json.

    El fichero y su posible ruta EB_ACCOUNT_ALIASES son los mismos
    que utiliza sync_balances.py; no duplicamos identificadores
    bancarios en el .env.
    """
    aliases = load_account_aliases("production")

    matches = [
        identity
        for identity, label in aliases.items()
        if label == SHARED_ACCOUNT_LABEL
    ]

    if len(matches) != 1:
        raise ValueError(
            f"Se esperaba exactamente un alias "
            f"'{SHARED_ACCOUNT_LABEL}' en production; "
            f"encontrados: {len(matches)}. No se publica nada."
        )

    return matches[0]


def get_latest_balance(
    identity: str,
) -> tuple[str, Decimal, str, str]:
    """Devuelve (etiqueta, CLBD en EUR, fecha consulta, referencia).

    Falla de forma segura si la cuenta, moneda o saldo
    no coinciden exactamente.
    """
    db_path = db_file()

    with sqlite3.connect(
        db_path.as_uri() + "?mode=ro",
        uri=True,
        timeout=10,
    ) as db:
        db.row_factory = sqlite3.Row

        latest = db.execute(
            """
            SELECT id, bank, fetched_at
            FROM snapshots
            WHERE environment = 'production'
            ORDER BY fetched_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()

        if latest is None:
            raise ValueError(
                "No hay sincronizaciones de producción en SQLite"
            )

        matches = db.execute(
            """
            SELECT a.label, b.amount, b.reference_date
            FROM balances AS b
            JOIN accounts AS a ON a.id = b.account_id
            WHERE b.snapshot_id = ?
              AND a.environment = 'production'
              AND a.bank = ?
              AND a.identification_hash = ?
              AND b.balance_type = 'CLBD'
              AND b.currency = 'EUR'
            """,
            (latest["id"], latest["bank"], identity),
        ).fetchall()

    if len(matches) != 1:
        raise ValueError(
            "Se esperaba exactamente un saldo CLBD/EUR "
            "para la cuenta configurada; "
            f"encontrados: {len(matches)}. No se publica nada."
        )

    try:
        amount = Decimal(matches[0]["amount"])
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Saldo CLBD no numérico") from exc

    if not amount.is_finite():
        raise ValueError("Saldo CLBD no finito")

    return (
        str(matches[0]["label"]),
        amount,
        str(latest["fetched_at"]),
        str(matches[0]["reference_date"] or ""),
    )


def parse_utc(stamp: str) -> datetime:
    dt = datetime.fromisoformat(
        stamp.replace("Z", "+00:00")
    )

    if dt.tzinfo is None:
        raise ValueError(
            "fetched_at no incluye zona horaria"
        )

    return dt.astimezone(timezone.utc)


def publish(
    label: str,
    amount: Decimal,
    fetched: str,
    reference: str,
) -> None:
    import gspread
    from google.oauth2.service_account import Credentials

    spreadsheet_id = required_env("GOOGLE_SHEET_ID")

    key_file = Path(
        required_env("GOOGLE_SHEETS_CREDENTIALS")
    ).expanduser()

    if not key_file.is_file():
        raise FileNotFoundError(
            "No existe el archivo JSON de Google Sheets"
        )

    credentials = Credentials.from_service_account_file(
        str(key_file),
        scopes=SHEETS_SCOPE,
    )

    client = gspread.authorize(credentials)

    worksheet = client.open_by_key(
        spreadsheet_id
    ).sheet1

    fetched_local = (
        parse_utc(fetched)
        .astimezone(MADRID)
        .isoformat(timespec="seconds")
    )

    rows = [
        [
            "Cuenta",
            "Saldo contable (EUR)",
            "Saldo disponible (EUR)",
            "Última consulta",
            "Referencia del banco",
            "Tipo de saldo",
        ],
        [
            label,
            float(amount),
            "",
            fetched_local,
            reference,
            "CLBD (contable)",
        ],
    ]

    worksheet.update(
        values=rows,
        range_name="A1:F2",
        raw=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Verifica que la cuenta existe en SQLite, "
            "sin acceder a Google"
        ),
    )

    args = parser.parse_args()

    try:
        if (
            os.getenv("EB_ENV", "sandbox")
            .strip()
            .lower()
            != "production"
        ):
            raise ValueError(
                "Publicación desactivada: "
                "EB_ENV no es production"
            )

        identity = get_shared_identity()

        label, amount, fetched, reference = (
            get_latest_balance(identity)
        )

        # Comprobación cruzada entre SQLite y account_aliases.json.
        if label != SHARED_ACCOUNT_LABEL:
            raise ValueError(
                "La etiqueta de la cuenta en SQLite "
                "no coincide con account_aliases.json. "
                "No se publica nada."
            )

        if args.dry_run:
            print(
                f"OK (dry-run): {label}; "
                f"CLBD/EUR encontrado; fecha={fetched}. "
                "No se ha publicado nada."
            )
            return 0

        # Verificar únicamente que la fecha no está en el futuro.
        fetched_at = parse_utc(fetched)

        if fetched_at > datetime.now(timezone.utc) + timedelta(minutes=2):
            raise ValueError(
                "La fecha de sincronización está en el futuro. "
                "No se publica el saldo."
            )

        publish(
            label,
            amount,
            fetched,
            reference,
        )

        print(
            f"OK: Google Sheets actualizado "
            f"para '{label}', "
            f"saldo contable CLBD, fecha={fetched}"
        )

        return 0

    except Exception as exc:
        print(
            f"ERROR: no se publicó el saldo compartido: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
