import os
from unittest.mock import patch

from django.http import HttpResponse
from django.middleware.csrf import get_token
from django.test import RequestFactory, SimpleTestCase

from feedora_project.cors_middleware import (
    LocalExpoCorsMiddleware,
    LocalExpoCsrfViewMiddleware,
    is_local_expo_development_origin,
)


class LocalExpoOriginTests(SimpleTestCase):
    def test_debug_allows_loopback_expo_fallback_origins(self):
        for origin in ('http://localhost:8160', 'http://127.0.0.1:8081', 'http://localhost:19006'):
            with self.subTest(origin=origin), patch.dict(os.environ, {'DJANGO_DEBUG': 'True'}):
                self.assertTrue(is_local_expo_development_origin(origin))

    def test_debug_rejects_non_loopback_and_unrelated_origins(self):
        for origin in (
            'https://localhost:8160',
            'http://localhost.evil.test:8160',
            'http://192.168.1.8:8160',
            'http://localhost:3000',
            'http://localhost:8200',
            'http://localhost',
            'http://localhost:8160/respondent',
            'http://user@localhost:8160',
            'http://localhost:not-a-port',
        ):
            with self.subTest(origin=origin), patch.dict(os.environ, {'DJANGO_DEBUG': 'True'}):
                self.assertFalse(is_local_expo_development_origin(origin))

    def test_production_rejects_dynamic_loopback_origins(self):
        with patch.dict(os.environ, {'DJANGO_DEBUG': 'False'}):
            self.assertFalse(is_local_expo_development_origin('http://localhost:8160'))

    def test_cors_echoes_only_allowed_loopback_origin(self):
        with patch.dict(os.environ, {'DJANGO_DEBUG': 'True', 'FEEDORA_CORS_ALLOWED_ORIGINS': ''}):
            def response_with_existing_vary(request):
                response = HttpResponse('ok')
                response['Vary'] = 'Accept-Language'
                return response

            middleware = LocalExpoCorsMiddleware(response_with_existing_vary)
            request = RequestFactory().get('/', HTTP_ORIGIN='http://localhost:8160')
            response = middleware(request)
            self.assertEqual(response['Access-Control-Allow-Origin'], 'http://localhost:8160')
            self.assertCountEqual(response['Vary'].split(', '), ('Accept-Language', 'Origin'))

            denied_request = RequestFactory().get('/', HTTP_ORIGIN='http://attacker.test:8160')
            denied_response = middleware(denied_request)
            self.assertNotIn('Access-Control-Allow-Origin', denied_response)

    def test_csrf_accepts_the_same_local_expo_origin(self):
        with patch.dict(os.environ, {'DJANGO_DEBUG': 'True'}):
            middleware = LocalExpoCsrfViewMiddleware(lambda request: HttpResponse('ok'))
            request = RequestFactory().post('/', HTTP_ORIGIN='http://localhost:8160')
            self.assertTrue(middleware._origin_verified(request))

            denied_request = RequestFactory().post('/', HTTP_ORIGIN='http://attacker.test:8160')
            self.assertFalse(middleware._origin_verified(denied_request))

    def test_csrf_protected_post_passes_only_for_the_matching_loopback_origin(self):
        with patch.dict(os.environ, {'DJANGO_DEBUG': 'True'}):
            middleware = LocalExpoCsrfViewMiddleware(lambda request: HttpResponse('ok'))

            allowed_request = RequestFactory().post('/', HTTP_ORIGIN='http://localhost:8160')
            masked_token = get_token(allowed_request)
            allowed_request.COOKIES['csrftoken'] = allowed_request.META['CSRF_COOKIE']
            allowed_request.META['HTTP_X_CSRFTOKEN'] = masked_token
            accepted = middleware.process_view(allowed_request, lambda request: HttpResponse('ok'), (), {})
            self.assertIsNone(accepted)

            denied_request = RequestFactory().post('/', HTTP_ORIGIN='http://attacker.test:8160')
            masked_token = get_token(denied_request)
            denied_request.COOKIES['csrftoken'] = denied_request.META['CSRF_COOKIE']
            denied_request.META['HTTP_X_CSRFTOKEN'] = masked_token
            rejected = middleware.process_view(denied_request, lambda request: HttpResponse('ok'), (), {})
            self.assertEqual(rejected.status_code, 403)
