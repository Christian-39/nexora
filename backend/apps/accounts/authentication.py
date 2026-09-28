from django.conf import settings
from django.utils import timezone
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.settings import api_settings
from rest_framework.authentication import CSRFCheck
from rest_framework.exceptions import PermissionDenied, AuthenticationFailed


from .models import DeviceSession


class CookieJWTAuthentication(JWTAuthentication):
    """Cookie JWT + server-side session check in ONE database query.

    Previously this issued two queries per request (user fetch + session
    existence). With a remote database each round-trip is expensive, so the
    session row and its user are fetched together. Every security check is
    preserved: signature/expiry validation, session revocation/expiry, the
    token-to-user binding (``user_id`` claim must match the session's user),
    the active-user check and the CSRF double-submit check.
    """

    def authenticate(self, request):
        raw = request.COOKIES.get(settings.ACCESS_COOKIE)
        if not raw:
            return None
        token = self.get_validated_token(raw)
        sid = token.get("sid")
        try:
            user_id = token[api_settings.USER_ID_CLAIM]
        except KeyError:
            raise AuthenticationFailed("Token contained no recognizable user identification")
        if not sid:
            raise AuthenticationFailed("Session revoked or expired.")
        session = (
            DeviceSession.objects.select_related("user")
            .filter(
                id=sid,
                user_id=user_id,
                revoked_at__isnull=True,
                expires_at__gt=timezone.now(),
            )
            .first()
        )
        if session is None:
            raise AuthenticationFailed("Session revoked or expired.")
        user = session.user
        if not user.is_active:
            raise AuthenticationFailed("User is inactive", code="user_inactive")
        check = CSRFCheck(lambda req: None)
        check.process_request(request._request)
        reason = check.process_view(request._request, None, (), {})
        if reason:
            raise PermissionDenied("CSRF validation failed.")
        return user, token
