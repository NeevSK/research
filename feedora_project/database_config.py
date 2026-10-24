"""Environment-driven Django database settings with a local SQLite default."""

from pathlib import Path
from typing import Mapping

from django.core.exceptions import ImproperlyConfigured


def build_database_config(env: Mapping[str, str], base_dir: Path) -> dict[str, dict[str, object]]:
    engine = env.get('DJANGO_DB_ENGINE', 'sqlite').strip().lower()
    connection_age_raw = env.get('DJANGO_DB_CONN_MAX_AGE', '0').strip()
    try:
        connection_age = int(connection_age_raw)
    except ValueError as exc:
        raise ImproperlyConfigured('DJANGO_DB_CONN_MAX_AGE must be a non-negative integer.') from exc
    if connection_age < 0:
        raise ImproperlyConfigured('DJANGO_DB_CONN_MAX_AGE must be a non-negative integer.')

    if engine in ('sqlite', 'django.db.backends.sqlite3'):
        return {
            'default': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': Path(env.get('DJANGO_SQLITE_PATH', str(base_dir / 'db.sqlite3'))),
                'OPTIONS': {'timeout': int(env.get('DJANGO_SQLITE_TIMEOUT', '20'))},
            }
        }

    if engine not in ('mysql', 'django.db.backends.mysql'):
        raise ImproperlyConfigured('DJANGO_DB_ENGINE must be sqlite or mysql.')

    required = ('DJANGO_DB_NAME', 'DJANGO_DB_USER', 'DJANGO_DB_PASSWORD', 'DJANGO_DB_HOST')
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise ImproperlyConfigured(f'MySQL requires these settings: {", ".join(missing)}.')

    database = {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': env['DJANGO_DB_NAME'],
        'USER': env['DJANGO_DB_USER'],
        'PASSWORD': env['DJANGO_DB_PASSWORD'],
        'HOST': env['DJANGO_DB_HOST'],
        'PORT': env.get('DJANGO_DB_PORT', '3306'),
        'OPTIONS': {'charset': 'utf8mb4'},
        'CONN_MAX_AGE': connection_age,
        'CONN_HEALTH_CHECKS': connection_age > 0,
    }
    # PythonAnywhere-managed MySQL users may not have CREATE DATABASE rights.
    # Allow a pre-created test database so Django's test runner can still run
    # the backend suite against the same MySQL engine as production.
    test_name = env.get('DJANGO_DB_TEST_NAME', '').strip()
    if test_name:
        production_name = str(database['NAME']).strip()
        if test_name.casefold() == production_name.casefold():
            raise ImproperlyConfigured('DJANGO_DB_TEST_NAME must be different from DJANGO_DB_NAME.')
        database['TEST'] = {'NAME': test_name}
    return {'default': database}
