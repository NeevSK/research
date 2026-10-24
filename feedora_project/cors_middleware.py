"""Exact-origin CORS bridge for the Expo preview and configured web clients."""

import os

from urllib.parse import urlsplit

from django.middleware.csrf import CsrfViewMiddleware


def is_local_expo_development_origin(origin):
    """Match Expo's local web origins without trusting arbitrary websites."""
    if os.environ.get('DJANGO_DEBUG', 'True').lower() not in ('true', '1', 'yes'):
        return False
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == 'http'
        and parsed.hostname in {'localhost', '127.0.0.1'}
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ''
        and parsed.query == ''
        and parsed.fragment == ''
        and (port == 19006 or (port is not None and 8081 <= port <= 8199))
    )


class LocalExpoCsrfViewMiddleware(CsrfViewMiddleware):
    """Trust only the same loopback Expo origins as the CORS bridge in debug."""

    def _origin_verified(self, request):
        if super()._origin_verified(request):
            return True
        return is_local_expo_development_origin(request.META.get('HTTP_ORIGIN', ''))


class LocalExpoCorsMiddleware:
    """Allow the local Expo web origin to call the local Django API."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        origin = request.headers.get('Origin', '')
        configured_origins = {
            item.strip().rstrip('/')
            for item in os.environ.get('FEEDORA_CORS_ALLOWED_ORIGINS', '').split(',')
            if item.strip()
        }
        local_development_origin = is_local_expo_development_origin(origin)
        allowed = origin.rstrip('/') in configured_origins or local_development_origin
        if request.method == 'OPTIONS' and allowed:
            response = self._preflight()
        else:
            response = self.get_response(request)
        if allowed:
            from django.utils.cache import patch_vary_headers

            response['Access-Control-Allow-Origin'] = origin
            response['Access-Control-Allow-Credentials'] = 'true'
            patch_vary_headers(response, ('Origin',))
        return response

    @staticmethod
    def _preflight():
        from django.http import HttpResponse

        response = HttpResponse(status=204)
        response['Access-Control-Allow-Methods'] = 'GET, POST, PUT, PATCH, DELETE, OPTIONS'
        response['Access-Control-Allow-Headers'] = 'Content-Type, X-CSRFToken, Authorization, X-Feedora-Guest-Token'
        response['Access-Control-Max-Age'] = '600'
        return response
