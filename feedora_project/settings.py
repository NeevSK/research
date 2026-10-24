import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured
from feedora_project.database_config import build_database_config

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = os.environ.get('DJANGO_DEBUG', 'True').lower() in ('true', '1', 'yes')
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured('DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false.')
    # Development-only fallback. Production must supply a private key through
    # the PythonAnywhere WSGI environment or another secret manager.
    SECRET_KEY = 'django-insecure-feedora-local-development-only'

env_hosts = os.environ.get('DJANGO_ALLOWED_HOSTS')
if env_hosts:
    ALLOWED_HOSTS = [h.strip() for h in env_hosts.split(',') if h.strip()]
else:
    if not DEBUG:
        raise ImproperlyConfigured('DJANGO_ALLOWED_HOSTS must list the deployed host when DJANGO_DEBUG is false.')
    ALLOWED_HOSTS = ['*']

csrf_origins = os.environ.get('DJANGO_CSRF_TRUSTED_ORIGINS', '')
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in csrf_origins.split(',') if origin.strip()]
if DEBUG:
    CSRF_TRUSTED_ORIGINS.extend([
        'http://localhost:8081',
        'http://127.0.0.1:8081',
        'http://localhost:19006',
        'http://127.0.0.1:19006',
    ])

# PythonAnywhere terminates HTTPS at its front web server. Trust its forwarded
# scheme so Django builds secure URLs and marks session/CSRF cookies correctly.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG


# Apps 
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Required by allauth
    'django.contrib.sites',

    # allauth core
    'allauth',
    'allauth.account',
    'allauth.socialaccount',

    # Notetoself-OAuth providers — Apple OAuth needs a paid Apple Developer account ($99/year), so only add googleauth
    
    'allauth.socialaccount.providers.google',

    # Your own app
    'accounts',
    'surveys'
]


# Middleware 
MIDDLEWARE = [
    'feedora_project.cors_middleware.LocalExpoCorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'feedora_project.cors_middleware.LocalExpoCsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Required by allauth — must be after AuthenticationMiddleware
    'allauth.account.middleware.AccountMiddleware',
]


ROOT_URLCONF = 'feedora_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        # Tell Django to also look in a top-level templates/ folder
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'feedora_project.wsgi.application'


# Database: local SQLite by default; hosted deployments select mysql through
# DJANGO_DB_ENGINE and provide credentials only through environment settings.
DATABASES = build_database_config(os.environ, BASE_DIR)


# Auth 

# Points Django to custom User model (in accounts/models.py)
AUTH_USER_MODEL = 'accounts.User'

# Tell Django to use allauth for authentication instead of the default backend
AUTHENTICATION_BACKENDS = [
    # Needed for Django admin to still work with username login
    'django.contrib.auth.backends.ModelBackend',
    # allauth handles email-based login and OAuth
    'allauth.account.auth_backends.AuthenticationBackend',
]

# Required by allauth — identifies which Site object to use from the database
SITE_ID = 1


# Allauth configuration 

# Use email as the login identifier, not username
ACCOUNT_LOGIN_METHODS = {'email'}
ACCOUNT_SIGNUP_FIELDS = ['email*', 'password1*', 'password2*']

# Require email verification before the user can log in
ACCOUNT_EMAIL_VERIFICATION = 'mandatory'

# Where to send users after login and logout
LOGIN_URL = 'role_select_login'
LOGIN_REDIRECT_URL = '/dashboard/'
ACCOUNT_LOGOUT_REDIRECT_URL = '/'

# To use my own custom templates instead of allauth's default ones
ACCOUNT_EMAIL_SUBJECT_PREFIX = '[Feedora] '


# Google OAuth 
SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'SCOPE': ['profile', 'email'],
        'AUTH_PARAMS': {'access_type': 'online'},
        # Skips the allauth "confirm email" step for Google logins
        # since Google already verified the email
        'SOCIALACCOUNT_EMAIL_VERIFICATION': 'none',
    }
}


# Password validation 
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# Internationalisation 
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Colombo'   # Changed from UTC
USE_I18N = True
USE_TZ = True


# Static files
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']   # local static folder
STATIC_ROOT = BASE_DIR / 'staticfiles'     # where collectstatic outputs to (for deployment)


# Email 
# During development this prints emails to terminal instead of sending them.
# Verification code appears in the console when a user registers.
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# When ready to send real emails (e.g. on PythonAnywhere), swap to:
# EMAIL_BACKEND = 'django.mail.backends.smtp.EmailBackend'
# EMAIL_HOST = 'smtp.gmail.com'
# EMAIL_PORT = 587
# EMAIL_USE_TLS = True
# EMAIL_HOST_USER = 'your@gmail.com'
# EMAIL_HOST_PASSWORD = 'your-app-password'


DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
