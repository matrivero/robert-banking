
#!/usr/bin/env python3
"""Autoriza/renueva el consentimiento AIS en Enable Banking.

Configura con .env junto al script. Nunca comparte claves ni sesiones con Hermes.

    python authorize_sabadell.py --check-consent  # Solo consulta límites
    python authorize_sabadell.py                  # Solicita nueva autorización

EB_CONSENT_DAYS=auto solicita el máximo anunciado por el banco (margen de 5 min).
Se puede limitar con EB_CONSENT_DAYS=10, por ejemplo.
"""

import argparse
import hmac
import json
import os
import secrets
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jwt
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
os.umask(0o077)

API_URL = "https://api.enablebanking.com"
APP_ID = os.environ["EB_APP_ID"]
ENVIRONMENT = os.getenv("EB_ENV", "sandbox").strip().lower()
BANK_NAME = os.getenv("EB_BANK_NAME", "Banco de Sabadell").strip()
BANK_COUNTRY = os.getenv("EB_BANK_COUNTRY", "ES").strip().upper()
REDIRECT_URL = os.getenv(
    "EB_REDIRECT_URL", "https://example.com/callback"
).strip()
CONSENT_DAYS = os.getenv("EB_CONSENT_DAYS", "auto").strip().lower()

# Margen de seguridad de 5 minutos para evitar que la API
# rechace una fecha demasiado cercana al límite máximo.
MAX_VALIDITY_MARGIN_SECONDS = 300


def config_path(variable: str, fallback: str) -> Path:
    """Resuelve rutas absolutas y relativas respecto al proyecto."""
    path = Path(os.getenv(variable, fallback)).expanduser()
    return path if path.is_absolute() else ROOT / path


PRIVATE_KEY_FILE = config_path(
    "EB_PRIVATE_KEY", "robert_banking.pem"
)
SESSION_FILE = config_path(
    "EB_SESSION_FILE", "sandbox_session.json"
)


def create_headers() -> dict:
    """Genera un JWT firmado con la clave privada de la aplicación."""

    if not PRIVATE_KEY_FILE.is_file():
        raise FileNotFoundError(
            f"Clave privada no encontrada: {PRIVATE_KEY_FILE}"
        )

    now = int(time.time())

    token = jwt.encode(
        {
            "iss": "enablebanking.com",
            "aud": "api.enablebanking.com",
            "iat": now,
            "exp": now + 3600,
        },
        PRIVATE_KEY_FILE.read_bytes(),
        algorithm="RS256",
        headers={"kid": APP_ID},
    )

    return {"Authorization": f"Bearer {token}"}


def get_max_consent_seconds(headers: dict) -> int:
    """Consulta el máximo consentimiento del banco, en segundos."""

    response = requests.get(
        f"{API_URL}/aspsps",
        params={"country": BANK_COUNTRY},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()

    banks = response.json().get("aspsps", [])

    matches = [
        bank
        for bank in banks
        if bank.get("name", "").casefold() == BANK_NAME.casefold()
        and bank.get("country", "").upper() == BANK_COUNTRY
    ]

    if len(matches) != 1:
        available = ", ".join(
            sorted(bank.get("name", "?") for bank in banks)
        )
        raise RuntimeError(
            f"No se encontró exactamente un banco "
            f"{BANK_NAME!r} ({BANK_COUNTRY}). "
            f"Bancos disponibles: {available}"
        )

    raw = matches[0].get("maximum_consent_validity")

    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise RuntimeError(
            "El banco no informa un maximum_consent_validity válido"
        )

    return raw


def choose_consent_seconds(bank_limit_seconds: int) -> int:
    """Calcula la duración solicitada según EB_CONSENT_DAYS."""

    safe_max = (
        bank_limit_seconds - MAX_VALIDITY_MARGIN_SECONDS
    )

    if safe_max <= 0:
        raise RuntimeError(
            "El máximo de consentimiento informado es demasiado corto"
        )

    if CONSENT_DAYS == "auto":
        return safe_max

    try:
        requested_days = int(CONSENT_DAYS)
    except ValueError as exc:
        raise ValueError(
            "EB_CONSENT_DAYS debe ser 'auto' o un entero positivo"
        ) from exc

    if requested_days <= 0:
        raise ValueError(
            "EB_CONSENT_DAYS debe ser mayor que cero"
        )

    return min(requested_days * 86400, safe_max)


def save_session_atomically(session: dict) -> None:
    """Crea un backup y publica la nueva sesión de forma atómica."""

    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Conservar copia de seguridad de la sesión anterior.
    if SESSION_FILE.exists():
        stamp = datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )

        backup = SESSION_FILE.with_name(
            f"{SESSION_FILE.name}.{stamp}.bak"
        )

        shutil.copyfile(SESSION_FILE, backup)
        backup.chmod(0o600)

        print(f"Copia de seguridad: {backup}")

    tmp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=SESSION_FILE.parent,
            prefix=f".{SESSION_FILE.name}.",
            suffix=".tmp",
            delete=False,
        ) as out:
            tmp_path = Path(out.name)

            os.chmod(tmp_path, 0o600)

            json.dump(
                session,
                out,
                indent=2,
                ensure_ascii=False,
            )
            out.write("\n")

            out.flush()
            os.fsync(out.fileno())

        # Sustituir el archivo anterior de forma atómica.
        os.replace(tmp_path, SESSION_FILE)

    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--check-consent",
        action="store_true",
        help=(
            "Consulta la duración máxima sin iniciar una "
            "autorización ni modificar la sesión"
        ),
    )

    args = parser.parse_args()

    # 1. Autenticación de la aplicación
    headers = create_headers()

    print(
        f"Entorno: {ENVIRONMENT} | "
        f"Banco: {BANK_NAME} ({BANK_COUNTRY})"
    )

    # 2. Consultar el límite de consentimiento del banco
    bank_limit_seconds = get_max_consent_seconds(headers)

    consent_seconds = choose_consent_seconds(
        bank_limit_seconds
    )

    print(
        f"Máximo que declara el banco: "
        f"{bank_limit_seconds / 86400:.2f} días"
    )

    print(
        f"Duración que solicitaremos: "
        f"{consent_seconds / 86400:.2f} días"
    )

    # Modo de consulta: no modifica la sesión.
    if args.check_consent:
        print(
            "Solo consulta: no se ha iniciado "
            "ni modificado ninguna sesión."
        )
        return

    # 3. Solicitar autorización bancaria
    state = secrets.token_urlsafe(32)

    # Calcular la caducidad lo más cerca posible de POST /auth
    valid_until = (
        datetime.now(timezone.utc)
        + timedelta(seconds=consent_seconds)
    ).isoformat(timespec="seconds")

    request_body = {
        "access": {
            "balances": True,
            "transactions": False,
            "valid_until": valid_until,
        },
        "aspsp": {
            "name": BANK_NAME,
            "country": BANK_COUNTRY,
        },
        "state": state,
        "redirect_url": REDIRECT_URL,
        "psu_type": "personal",
    }

    response = requests.post(
        f"{API_URL}/auth",
        headers=headers,
        json=request_body,
        timeout=30,
    )
    response.raise_for_status()

    auth_url = response.json()["url"]

    print("\n1. Abre esta URL en el navegador:\n")
    print(auth_url)
    print("\n2. Completa la autorización bancaria.")
    print("3. Copia la URL FINAL de retorno (no la compartas).\n")

    callback_url = input(
        "Pega aquí la URL final: "
    ).strip()

    # 4. Validar la respuesta de autorización
    actual = urlparse(callback_url)
    expected = urlparse(REDIRECT_URL)

    if (
        actual.scheme,
        actual.netloc,
        actual.path,
    ) != (
        expected.scheme,
        expected.netloc,
        expected.path,
    ):
        raise RuntimeError(
            "URL de retorno inesperada"
        )

    params = parse_qs(actual.query)

    received_state = params.get("state", [""])[0]

    if not hmac.compare_digest(received_state, state):
        raise RuntimeError(
            "State inválido: respuesta de autorización rechazada"
        )

    if "error" in params:
        raise RuntimeError(
            f"Error de autorización: {params['error'][0]}"
        )

    code = params.get("code", [""])[0]

    if not code:
        raise RuntimeError(
            "No se encontró el código de autorización"
        )

    # 5. Intercambiar código temporal por nueva sesión
    response = requests.post(
        f"{API_URL}/sessions",
        headers=headers,
        json={"code": code},
        timeout=30,
    )
    response.raise_for_status()

    session = response.json()
    accounts = session.get("accounts")

    if not isinstance(accounts, list) or not accounts:
        raise RuntimeError(
            "La sesión nueva no contiene cuentas autorizadas"
        )

    # 6. Verificar los saldos antes de guardar la nueva sesión
    for index, account in enumerate(
        accounts, start=1
    ):
        uid = account["uid"]

        response = requests.get(
            f"{API_URL}/accounts/{uid}/balances",
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()

        print(f"\nCuenta {index}")

        for balance in response.json().get(
            "balances", []
        ):
            amount = balance["balance_amount"]

            print(
                f"  {balance['balance_type']}: "
                f"{amount['amount']} {amount['currency']}"
            )

    # 7. Guardar sesión verificada
    save_session_atomically(session)

    print(f"\nSesión nueva guardada: {SESSION_FILE}")
    print(f"Cuentas autorizadas: {len(accounts)}")

    print(
        "Válida hasta:",
        session.get("access", {}).get(
            "valid_until", "no informado"
        ),
    )

    print(
        "Ejecuta el servicio de sincronización "
        "para actualizar SQLite y JSON."
    )


if __name__ == "__main__":
    main()
