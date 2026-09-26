import os
from pathlib import Path
from datetime import timedelta
import dj_database_url
BASE_DIR=Path(__file__).resolve().parent.parent
SECRET_KEY=os.environ.get('SECRET_KEY','unsafe-development-only')
DEBUG=os.environ.get('DEBUG','False').lower()=='true'
ALLOWED_HOSTS=[x for x in os.environ.get('ALLOWED_HOSTS','localhost,127.0.0.1').split(',') if x]
INSTALLED_APPS=['django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','django.contrib.staticfiles','corsheaders','rest_framework','rest_framework_simplejwt.token_blacklist','channels','apps.core','apps.accounts','apps.media','apps.conversations','apps.groups','apps.notifications','apps.platform_settings','apps.audit','apps.security']
MIDDLEWARE=['django.middleware.security.SecurityMiddleware','corsheaders.middleware.CorsMiddleware','django.contrib.sessions.middleware.SessionMiddleware','django.middleware.common.CommonMiddleware','django.middleware.csrf.CsrfViewMiddleware','django.contrib.auth.middleware.AuthenticationMiddleware','apps.core.middleware.RequestIDMiddleware','apps.core.security_headers.SecurityHeadersMiddleware','django.middleware.clickjacking.XFrameOptionsMiddleware']
ROOT_URLCONF='config.urls'; ASGI_APPLICATION='config.asgi.application'; WSGI_APPLICATION='config.wsgi.application'
DATABASES={'default':dj_database_url.config(default=os.environ.get('DATABASE_URL','sqlite:///'+str(BASE_DIR/'db.sqlite3')),conn_max_age=60)}
AUTH_USER_MODEL='accounts.User'; DEFAULT_AUTO_FIELD='django.db.models.BigAutoField'; USE_TZ=True; TIME_ZONE=os.environ.get('TIME_ZONE','UTC')
STATIC_URL='/static/'; MEDIA_ROOT=BASE_DIR/'media'; MEDIA_URL='/media/'
CORS_ALLOWED_ORIGINS=[x for x in os.environ.get('CORS_ALLOWED_ORIGINS','').split(',') if x]; CORS_ALLOW_CREDENTIALS=True
CSRF_TRUSTED_ORIGINS=[x for x in os.environ.get('CSRF_TRUSTED_ORIGINS','').split(',') if x]
REST_FRAMEWORK={'DEFAULT_AUTHENTICATION_CLASSES':['apps.accounts.authentication.CookieJWTAuthentication'],'DEFAULT_PERMISSION_CLASSES':['rest_framework.permissions.IsAuthenticated','apps.accounts.permissions.CredentialChangedOrAllowed'],'DEFAULT_PAGINATION_CLASS':'apps.core.pagination.StandardCursorPagination','PAGE_SIZE':30,'EXCEPTION_HANDLER':'apps.core.exceptions.api_exception_handler','DEFAULT_THROTTLE_CLASSES':['rest_framework.throttling.AnonRateThrottle','rest_framework.throttling.UserRateThrottle'],'DEFAULT_THROTTLE_RATES':{'anon':'60/min','user':'300/min','login':'5/min','messages':'60/min','search':'30/min','uploads':'20/hour','push':'20/hour','credentials':'5/hour'}}
SIMPLE_JWT={'ACCESS_TOKEN_LIFETIME':timedelta(minutes=int(os.environ.get('ACCESS_TOKEN_MINUTES','10'))),'REFRESH_TOKEN_LIFETIME':timedelta(days=int(os.environ.get('REFRESH_TOKEN_DAYS','7'))),'ROTATE_REFRESH_TOKENS':True,'BLACKLIST_AFTER_ROTATION':True,'UPDATE_LAST_LOGIN':True,'ALGORITHM':'HS256','SIGNING_KEY':SECRET_KEY}
REDIS_URL=os.environ.get('REDIS_URL')
CHANNEL_LAYERS={'default':{'BACKEND':'channels_redis.core.RedisChannelLayer','CONFIG':{'hosts':[REDIS_URL]}}} if REDIS_URL else {'default':{'BACKEND':'channels.layers.InMemoryChannelLayer'}}
SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https'); SESSION_COOKIE_HTTPONLY=True; CSRF_COOKIE_SECURE=not DEBUG; SESSION_COOKIE_SECURE=not DEBUG; SECURE_CONTENT_TYPE_NOSNIFF=True; X_FRAME_OPTIONS='DENY'; SECURE_REFERRER_POLICY='same-origin'; SECURE_HSTS_SECONDS=31536000 if not DEBUG else 0
ACCESS_COOKIE='nexora_access'; REFRESH_COOKIE='nexora_refresh'; COOKIE_SAMESITE=os.environ.get('COOKIE_SAMESITE','Lax'); COOKIE_SECURE=not DEBUG
MESSAGE_MAX_LENGTH=int(os.environ.get('MESSAGE_MAX_LENGTH','5000'))

if os.environ.get('STORAGE_BUCKET'):
 STORAGES={'default':{'BACKEND':'storages.backends.s3.S3Storage','OPTIONS':{'bucket_name':os.environ['STORAGE_BUCKET'],'endpoint_url':os.environ.get('STORAGE_ENDPOINT'),'access_key':os.environ.get('STORAGE_ACCESS_KEY'),'secret_key':os.environ.get('STORAGE_SECRET_KEY'),'default_acl':'private','querystring_auth':True,'querystring_expire':300}},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}}

PUSH_PUBLIC_KEY=os.environ.get('PUSH_PUBLIC_KEY','')
PUSH_PRIVATE_KEY=os.environ.get('PUSH_PRIVATE_KEY','')
PUSH_CONTACT=os.environ.get('PUSH_CONTACT','')

FFPROBE_BINARY=os.environ.get('FFPROBE_BINARY','ffprobe')
CACHES={'default':{'BACKEND':'django.core.cache.backends.redis.RedisCache','LOCATION':REDIS_URL}} if REDIS_URL else {'default':{'BACKEND':'django.core.cache.backends.locmem.LocMemCache','LOCATION':'nexora-dev'}}

if not DEBUG:
 from django.core.exceptions import ImproperlyConfigured
 if SECRET_KEY=='unsafe-development-only' or len(SECRET_KEY)<32:raise ImproperlyConfigured('A unique SECRET_KEY of at least 32 characters is required.')
 if not REDIS_URL:raise ImproperlyConfigured('REDIS_URL is mandatory in production.')
 if DATABASES['default']['ENGINE'].endswith('sqlite3'):raise ImproperlyConfigured('SQLite is not supported in production.')
 if not os.environ.get('STORAGE_BUCKET'):raise ImproperlyConfigured('Private object storage is mandatory in production.')

FILE_UPLOAD_MAX_MEMORY_SIZE=int(os.environ.get('FILE_UPLOAD_MAX_MEMORY_SIZE','2621440'))
DATA_UPLOAD_MAX_MEMORY_SIZE=int(os.environ.get('DATA_UPLOAD_MAX_MEMORY_SIZE','10485760'))
FFMPEG_BINARY=os.environ.get('FFMPEG_BINARY','ffmpeg')
