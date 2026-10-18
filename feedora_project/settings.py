import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-*$9+bas)y6ow=9ryveu!(!0itxz25$etc#r5e834b$wr!#kr(7'
)

DEBUG = os.environ.get('DJANGO_DEBUG', 'True').lower() in ('true', '1', 'yes')

env_hosts = os.environ.get('DJANGO_ALLOWED_HOSTS')
if env_hosts:
    ALLOWED_HOSTS = [h.strip() for h in env_hosts.split(',') if h.strip()]
else:
    ALLOWED_HOSTS = ['*'] if DEBUG else ['localhost', '127.0.0.1']


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
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
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


# Database 
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
        'OPTIONS': {
            'timeout': 20,
        },
    }
}


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