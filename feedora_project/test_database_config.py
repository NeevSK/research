from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from feedora_project.database_config import build_database_config


class DatabaseConfigurationTests(SimpleTestCase):
    def test_local_default_remains_sqlite(self):
        config = build_database_config({}, Path('C:/feedora'))['default']
        self.assertEqual(config['ENGINE'], 'django.db.backends.sqlite3')
        self.assertEqual(config['NAME'], Path('C:/feedora/db.sqlite3'))
        self.assertEqual(config['OPTIONS']['timeout'], 20)

    def test_mysql_uses_explicit_credentials_and_safe_connection_defaults(self):
        config = build_database_config({
            'DJANGO_DB_ENGINE': 'mysql',
            'DJANGO_DB_NAME': 'research_db',
            'DJANGO_DB_USER': 'feedora',
            'DJANGO_DB_PASSWORD': 'secret-value',
            'DJANGO_DB_HOST': 'db.example.test',
            'DJANGO_DB_CONN_MAX_AGE': '90',
        }, Path('C:/feedora'))['default']

        self.assertEqual(config['ENGINE'], 'django.db.backends.mysql')
        self.assertEqual(config['OPTIONS'], {'charset': 'utf8mb4'})
        self.assertEqual(config['CONN_MAX_AGE'], 90)
        self.assertTrue(config['CONN_HEALTH_CHECKS'])

    def test_mysql_can_use_a_precreated_test_database(self):
        config = build_database_config({
            'DJANGO_DB_ENGINE': 'mysql',
            'DJANGO_DB_NAME': 'research_db',
            'DJANGO_DB_USER': 'feedora',
            'DJANGO_DB_PASSWORD': 'secret-value',
            'DJANGO_DB_HOST': 'db.example.test',
            'DJANGO_DB_TEST_NAME': 'feedora$test_research_db',
        }, Path('C:/feedora'))['default']

        self.assertEqual(config['TEST'], {'NAME': 'feedora$test_research_db'})

    def test_blank_mysql_test_database_name_keeps_django_default(self):
        config = build_database_config({
            'DJANGO_DB_ENGINE': 'mysql',
            'DJANGO_DB_NAME': 'research_db',
            'DJANGO_DB_USER': 'feedora',
            'DJANGO_DB_PASSWORD': 'secret-value',
            'DJANGO_DB_HOST': 'db.example.test',
            'DJANGO_DB_TEST_NAME': '   ',
        }, Path('C:/feedora'))['default']

        self.assertNotIn('TEST', config)

    def test_mysql_rejects_test_database_name_that_matches_production(self):
        env = {
            'DJANGO_DB_ENGINE': 'mysql',
            'DJANGO_DB_NAME': 'Research_DB',
            'DJANGO_DB_USER': 'feedora',
            'DJANGO_DB_PASSWORD': 'secret-value',
            'DJANGO_DB_HOST': 'db.example.test',
            'DJANGO_DB_TEST_NAME': 'research_db',
        }

        with self.assertRaisesMessage(ImproperlyConfigured, 'must be different from DJANGO_DB_NAME'):
            build_database_config(env, Path('C:/feedora'))

    def test_mysql_fails_fast_when_connection_settings_are_missing(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'DJANGO_DB_PASSWORD'):
            build_database_config({'DJANGO_DB_ENGINE': 'mysql'}, Path('C:/feedora'))

    def test_unknown_backend_and_invalid_connection_age_are_rejected(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'must be sqlite or mysql'):
            build_database_config({'DJANGO_DB_ENGINE': 'postgres'}, Path('C:/feedora'))
        with self.assertRaisesMessage(ImproperlyConfigured, 'non-negative integer'):
            build_database_config({'DJANGO_DB_CONN_MAX_AGE': '-1'}, Path('C:/feedora'))
