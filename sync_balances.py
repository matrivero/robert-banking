
#!/usr/bin/env python3
"""Sincronización read-only de Enable Banking -> SQLite -> JSON para Hermes.

La clave RSA y la sesión se quedan en el host. El contenedor ve solo los JSON.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import jwt
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
API = 'https://api.enablebanking.com'
MADRID = ZoneInfo('Europe/Madrid')


class ConsentExpired(Exception):
    pass


def iso_utc():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(
        value.replace('Z', '+00:00')
    ).astimezone(timezone.utc)


def atomic_json(path: Path, payload: dict) -> None:
    """Publica el JSON solo después de escribirlo completamente."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(
        prefix='.' + path.name + '.',
        dir=str(path.parent)
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(
                payload, out,
                indent=2,
                ensure_ascii=False
            )
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def make_auth_headers(app_id: str, key_path: Path) -> dict:
    now = int(time.time())
    token = jwt.encode(
        {
            'iss': 'enablebanking.com',
            'aud': 'api.enablebanking.com',
            'iat': now,
            'exp': now + 3600
        },
        key_path.read_bytes(),
        algorithm='RS256',
        headers={'kid': app_id},
    )
    return {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json'
    }


def ensure_valid_session(session: dict) -> str:
    until = session.get('access', {}).get('valid_until')
    if not until:
        raise ValueError(
            'La sesión no contiene access.valid_until'
        )
    if datetime.now(timezone.utc) >= parse_time(until):
        raise ConsentExpired(
            'Consentimiento bancario caducado: vuelve a autorizar la cuenta'
        )
    if not session.get('accounts'):
        raise ValueError(
            'La sesión no contiene cuentas autorizadas'
        )
    return until


def clean_amount(raw: object) -> str:
    try:
        d = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError(
            'Importe no numérico en la API'
        ) from exc
    if not d.is_finite():
        raise ValueError(
            'Importe no finito en la API'
        )
    return format(d, 'f')


def fetch_balances(
    session: dict,
    headers: dict,
    http=None
) -> list[dict]:
    http = http or requests.Session()
    results = []
    seen_identities = set()

    for pos, account in enumerate(
        session['accounts'], start=1
    ):
        uid = (
            account.get('uid')
            if isinstance(account, dict)
            else account
        )

        if not uid:
            raise ValueError(
                f'Falta UID en la cuenta {pos}'
            )

        identity = (
            (account.get('identification_hash') or uid)
            if isinstance(account, dict)
            else uid
        )

        if identity in seen_identities:
            raise ValueError(
                'La misma cuenta aparece duplicada en la sesión'
            )

        seen_identities.add(identity)

        response = http.get(
            f'{API}/accounts/{uid}/balances',
            headers=headers,
            timeout=30
        )
        response.raise_for_status()

        payload = response.json()
        raw_balances = payload.get('balances')

        if not isinstance(raw_balances, list) or not raw_balances:
            raise ValueError(
                f'Sin saldos válidos en cuenta {pos}'
            )

        normalized = []

        for b in raw_balances:
            amount_obj = b['balance_amount']
            normalized.append({
                'type': str(b['balance_type']),
                'amount': clean_amount(
                    amount_obj['amount']
                ),
                'currency': str(
                    amount_obj['currency']
                ),
                'reference_date': b.get(
                    'reference_date'
                ),
            })

        results.append({
            'identity': str(identity),
            'balances': normalized
        })

    return results


def load_account_aliases(
    environment: str
) -> dict[str, str]:
    """Carga nombres locales por identification_hash.

    Formato de account_aliases.json:
    {
        "production": {
            "hash_de_cuenta": "Cuenta compartida"
        }
    }
    """
    configured = os.getenv('EB_ACCOUNT_ALIASES')

    path = (
        Path(configured).expanduser()
        if configured
        else ROOT / 'account_aliases.json'
    )

    if not path.is_absolute():
        path = ROOT / path

    if not path.exists():
        if configured:
            raise FileNotFoundError(
                f'No existe EB_ACCOUNT_ALIASES: {path}'
            )
        return {}

    data = json.loads(
        path.read_text(encoding='utf-8')
    )

    if not isinstance(data, dict):
        raise ValueError(
            'account_aliases.json debe contener un objeto JSON'
        )

    aliases = data.get(environment, {})

    if not isinstance(aliases, dict):
        raise ValueError(
            f'Los alias de {environment} deben ser un objeto JSON'
        )

    normalized = {}

    for identity, label in aliases.items():
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError(
                'Los identificadores de alias deben ser cadenas no vacías'
            )
        if not isinstance(label, str) or not label.strip():
            raise ValueError(
                'Los nombres de cuenta deben ser cadenas no vacías'
            )

        normalized[identity] = label.strip()

    labels = [
        label.casefold()
        for label in normalized.values()
    ]

    if len(labels) != len(set(labels)):
        raise ValueError(
            'Dos cuentas no pueden tener el mismo alias'
        )

    return normalized


def init_db(db: sqlite3.Connection) -> None:
    db.execute('PRAGMA foreign_keys = ON')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS accounts (
      id INTEGER PRIMARY KEY,
      environment TEXT NOT NULL,
      bank TEXT NOT NULL,
      identification_hash TEXT NOT NULL,
      label TEXT NOT NULL,
      UNIQUE(environment, bank, identification_hash)
    );

    CREATE TABLE IF NOT EXISTS snapshots (
      id INTEGER PRIMARY KEY,
      environment TEXT NOT NULL,
      bank TEXT NOT NULL,
      fetched_at TEXT NOT NULL,
      consent_valid_until TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS balances (
      id INTEGER PRIMARY KEY,
      snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
      account_id INTEGER NOT NULL REFERENCES accounts(id),
      balance_type TEXT NOT NULL,
      currency TEXT NOT NULL,
      amount TEXT NOT NULL,
      reference_date TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_snapshots_date
    ON snapshots(environment, bank, fetched_at);

    CREATE INDEX IF NOT EXISTS idx_balances_account
    ON balances(account_id, snapshot_id);
    ''')


def store_snapshot(
    db: sqlite3.Connection,
    environment: str,
    bank: str,
    fetched_at: str,
    valid_until: str,
    accounts: list[dict],
    aliases: dict[str, str] | None = None
) -> tuple[int, list[dict]]:
    """Toda la recogida se guarda en una única transacción."""
    labelled = []
    aliases = aliases or {}

    with db:
        cursor = db.execute(
            '''
            INSERT INTO snapshots(
                environment, bank, fetched_at, consent_valid_until
            ) VALUES(?,?,?,?)
            ''',
            (environment, bank, fetched_at, valid_until),
        )
        snap_id = cursor.lastrowid

        for record in accounts:
            identity = record['identity']

            row = db.execute(
                '''
                SELECT id,label FROM accounts
                WHERE environment=?
                  AND bank=?
                  AND identification_hash=?
                ''',
                (environment, bank, identity),
            ).fetchone()

            desired_label = aliases.get(identity)

            if row:
                account_id, label = row

                # Actualiza también las cuentas existentes.
                if desired_label and label != desired_label:
                    db.execute(
                        'UPDATE accounts SET label=? WHERE id=?',
                        (desired_label, account_id)
                    )
                    label = desired_label

            else:
                num = db.execute(
                    '''
                    SELECT COUNT(*) FROM accounts
                    WHERE environment=? AND bank=?
                    ''',
                    (environment, bank),
                ).fetchone()[0] + 1

                label = (
                    desired_label
                    or f'Cuenta {num}'
                )

                account_id = db.execute(
                    '''
                    INSERT INTO accounts(
                        environment, bank, identification_hash, label
                    ) VALUES(?,?,?,?)
                    ''',
                    (environment, bank, identity, label),
                ).lastrowid

            for b in record['balances']:
                db.execute(
                    '''
                    INSERT INTO balances(
                        snapshot_id,
                        account_id,
                        balance_type,
                        currency,
                        amount,
                        reference_date
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        snap_id,
                        account_id,
                        b['type'],
                        b['currency'],
                        b['amount'],
                        b['reference_date']
                    )
                )

            labelled.append({
                'name': label,
                'balances': record['balances']
            })

    return snap_id, labelled


def summarize(
    environment: str,
    bank: str,
    fetched_at: str,
    consent_until: str,
    accounts: list[dict]
) -> dict:
    totals: dict[str, Decimal] = {}
    complete = True

    for acct in accounts:
        booked = [
            b for b in acct['balances']
            if b['type'] == 'CLBD'
        ]

        if not booked:
            complete = False

        currencies = set()

        for b in booked:
            currency = b['currency']

            if currency in currencies:
                raise ValueError(
                    f'Duplicación de saldo CLBD en {acct["name"]}'
                )

            currencies.add(currency)

            totals[currency] = (
                totals.get(currency, Decimal('0'))
                + Decimal(b['amount'])
            )

    if not totals:
        complete = False

    return {
        'schema_version': 1,
        'bank': bank,
        'environment': environment,
        'fetched_at': fetched_at,
        'consent_valid_until': consent_until,
        'consent_days_remaining': (
            parse_time(consent_until).astimezone(MADRID).date()
            - datetime.now(MADRID).date()
        ).days,
        'account_count': len(accounts),
        'accounts': accounts,
        'total_clbd_by_currency': {
            c: format(v, 'f')
            for c, v in sorted(totals.items())
        },
        'total_clbd_complete': complete,
        'note': (
            'CLBD=saldo contable; XPCD=saldo esperado; '
            'ninguno implica saldo disponible'
        ),
    }


def format_euros_style(raw: str) -> str:
    """Formato español sin perder decimales originales."""
    value = Decimal(raw)
    decimals = max(2, -value.as_tuple().exponent)

    return (
        f'{value:,.{decimals}f}'
        .replace(',', '_')
        .replace('.', ',')
        .replace('_', '.')
    )


def render_report(summary: dict) -> str:
    """Informe determinista: las cuentas y sus totales coinciden."""
    stamp = parse_time(
        summary['fetched_at']
    ).astimezone(MADRID)

    expires = parse_time(
        summary['consent_valid_until']
    ).astimezone(MADRID)

    lines = [
        f"{summary['bank']} — saldos contables",
        f"Consulta: {stamp:%d/%m/%Y %H:%M} (Europe/Madrid)",
        f"Entorno: {summary['environment']}",
        f"Cuentas consultadas: {summary['account_count']}",
        '',
    ]

    check_totals: dict[str, Decimal] = {}
    listed = 0

    for acct in summary['accounts']:
        booked = [
            b for b in acct['balances']
            if b['type'] == 'CLBD'
        ]

        if not booked:
            lines.append(
                f"- {acct['name']}: CLBD no disponible"
            )

        for item in booked:
            currency = item['currency']
            value = Decimal(item['amount'])

            lines.append(
                f"- {acct['name']}: "
                f"{format_euros_style(item['amount'])} "
                f"{currency} (CLBD)"
            )

            check_totals[currency] = (
                check_totals.get(currency, Decimal('0'))
                + value
            )

        listed += 1

    if listed != summary['account_count']:
        raise ValueError(
            'El recuento de cuentas no coincide con el resumen'
        )

    expected = {
        c: Decimal(v)
        for c, v in summary['total_clbd_by_currency'].items()
    }

    if check_totals != expected:
        raise ValueError(
            'Los totales no coinciden con los saldos desglosados'
        )

    lines.append('')

    if check_totals:
        prefix = (
            'TOTAL CONTABLE'
            if summary['total_clbd_complete']
            else 'SUBTOTAL CONTABLE (incompleto)'
        )

        for currency, value in sorted(check_totals.items()):
            lines.append(
                f'{prefix}: '
                f'{format_euros_style(format(value, "f"))} '
                f'{currency}'
            )
    else:
        lines.append(
            'No es posible calcular el total contable '
            '(faltan todos los CLBD).'
        )

    lines.extend([
        '',
        f"Consentimiento válido hasta: "
        f"{expires:%d/%m/%Y %H:%M} (Europe/Madrid)",
        f"Días restantes (calendario): "
        f"{summary['consent_days_remaining']}",
        'CLBD es saldo contable, no necesariamente disponible.',
    ])

    return '\n'.join(lines) + '\n'


def atomic_text(path: Path, content: str) -> None:
    """Publica un texto completo sin archivo parcial."""
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700
    )

    fd, tmp = tempfile.mkstemp(
        prefix='.' + path.name + '.',
        dir=str(path.parent)
    )

    try:
        os.fchmod(fd, 0o600)

        with os.fdopen(
            fd, 'w', encoding='utf-8'
        ) as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())

        os.replace(tmp, path)

    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def build_history(
    db: sqlite3.Connection,
    environment: str,
    bank: str,
    limit: int = 30
) -> dict:
    """Última consulta de cada día local (máximo 30 días)."""
    recent = db.execute(
        '''
        SELECT id, fetched_at FROM snapshots
        WHERE environment=? AND bank=?
        ORDER BY fetched_at DESC, id DESC LIMIT 1500
        ''',
        (environment, bank)
    ).fetchall()

    picked = []
    days = set()

    for sid, fetched_at in recent:
        day = (
            parse_time(fetched_at)
            .astimezone(MADRID)
            .date()
            .isoformat()
        )

        if day not in days:
            days.add(day)
            picked.append(
                (sid, fetched_at, day)
            )

        if len(picked) >= limit:
            break

    history = []

    for sid, fetched_at, day in reversed(picked):
        rows = db.execute(
            '''
            SELECT a.label, b.currency, b.amount
            FROM balances b
            JOIN accounts a ON a.id=b.account_id
            WHERE b.snapshot_id=?
              AND b.balance_type='CLBD'
            ORDER BY a.id
            ''',
            (sid,)
        ).fetchall()

        totals: dict[str, Decimal] = {}
        acct = []

        for label, currency, amount in rows:
            acct.append({
                'name': label,
                'currency': currency,
                'clbd': amount
            })

            totals[currency] = (
                totals.get(currency, Decimal('0'))
                + Decimal(amount)
            )

        history.append({
            'date': day,
            'fetched_at': fetched_at,
            'accounts': acct,
            'total_clbd_by_currency': {
                c: format(v, 'f')
                for c, v in totals.items()
            }
        })

    return {
        'schema_version': 1,
        'bank': bank,
        'environment': environment,
        'history_type': 'last_snapshot_per_calendar_day',
        'days': history
    }


def write_error_status(
    share_dir: Path,
    exc: Exception
) -> None:
    # No borrar el último balances.json.
    # Su fecha permite reconocerlo como antiguo.
    prior = share_dir / 'balances.json'
    last_success = None

    if prior.is_file():
        try:
            last_success = json.loads(
                prior.read_text(encoding='utf-8')
            ).get('fetched_at')
        except (ValueError, OSError):
            pass

    if isinstance(exc, ConsentExpired):
        reason = (
            'El consentimiento ha caducado; '
            'es necesario renovar la autorización bancaria.'
        )
    elif isinstance(exc, requests.HTTPError):
        code = (
            exc.response.status_code
            if exc.response is not None
            else 'desconocido'
        )
        reason = (
            f'Enable Banking devolvió HTTP {code}; '
            'revisar conexión o autorización.'
        )
    elif isinstance(exc, requests.RequestException):
        reason = (
            'No fue posible conectar con Enable Banking.'
        )
    elif isinstance(
        exc, (FileNotFoundError, PermissionError)
    ):
        reason = (
            'Falta un archivo de configuración/sesión '
            'o sus permisos son incorrectos.'
        )
    else:
        reason = (
            f'Falló la sincronización ({type(exc).__name__}).'
        )

    atomic_json(
        share_dir / 'status.json',
        {
            'status': 'error',
            'last_attempt_at': iso_utc(),
            'last_success_at': last_success,
            'message': reason,
        }
    )


def main() -> int:
    os.umask(0o077)

    share_dir = Path(
        os.getenv(
            'EB_SHARE_DIR',
            '/home/pi/robert-banking-share'
        )
    )

    try:
        env = os.getenv(
            'EB_ENV', 'sandbox'
        ).strip().lower()

        if env not in ('sandbox', 'production'):
            raise ValueError(
                'EB_ENV debe ser sandbox o production'
            )

        app_id = os.environ['EB_APP_ID']

        private_key = Path(
            os.getenv(
                'EB_PRIVATE_KEY',
                str(ROOT / 'robert_banking.pem')
            )
        )

        session_path = Path(
            os.getenv(
                'EB_SESSION_FILE',
                str(ROOT / 'sandbox_session.json')
            )
        )

        db_path = Path(
            os.getenv(
                'EB_DB',
                str(ROOT / 'data' / 'banking.db')
            )
        )

        session = json.loads(
            session_path.read_text(encoding='utf-8')
        )

        valid_until = ensure_valid_session(session)

        bank = (
            session.get('aspsp', {}).get('name')
            or 'Banco de Sabadell'
        )

        headers = make_auth_headers(
            app_id, private_key
        )

        # Alias locales. No se comparten con Robert.
        aliases = load_account_aliases(env)

        # Consultar todas las cuentas ANTES de modificar SQLite.
        accounts = fetch_balances(
            session, headers
        )

        unmatched = set(aliases) - {
            a['identity'] for a in accounts
        }

        if unmatched:
            print(
                f'AVISO: {len(unmatched)} alias no '
                'corresponden a cuentas de esta sesión.',
                file=sys.stderr
            )

        fetched = iso_utc()

        db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
            mode=0o700
        )

        with sqlite3.connect(
            db_path, timeout=20
        ) as db:
            init_db(db)

            # Validar los saldos antes de almacenarlos.
            preview = [
                {
                    'name': f'Cuenta {i}',
                    'balances': a['balances']
                }
                for i, a in enumerate(accounts, start=1)
            ]

            summarize(
                env, bank, fetched,
                valid_until, preview
            )

            _, labelled = store_snapshot(
                db, env, bank, fetched,
                valid_until, accounts, aliases
            )

            latest = summarize(
                env, bank, fetched,
                valid_until, labelled
            )

            latest['report_text'] = render_report(
                latest
            )

            history = build_history(
                db, env, bank
            )

        os.chmod(db_path, 0o600)

        # Archivos compartidos con Robert.
        atomic_json(
            share_dir / 'balances.json',
            latest
        )

        atomic_json(
            share_dir / 'history.json',
            history
        )

        atomic_text(
            share_dir / 'daily_report.txt',
            latest['report_text']
        )

        atomic_json(
            share_dir / 'status.json',
            {
                'status': 'ok',
                'last_attempt_at': fetched,
                'last_success_at': fetched,
                'message': 'Consulta completada',
            }
        )

        print(
            f'OK: {len(labelled)} cuentas consultadas '
            f'({env}) a las {fetched}'
        )

        return 0

    except Exception as exc:
        try:
            write_error_status(
                share_dir, exc
            )
        except Exception:
            print(
                'ERROR: no se pudo escribir status.json',
                file=sys.stderr
            )

        print(
            f'ERROR: sincronización no completada '
            f'({type(exc).__name__}). '
            'Revisa status.json.',
            file=sys.stderr
        )

        return 1


if __name__ == '__main__':
    sys.exit(main())
