from rest_framework.permissions import BasePermission
class CredentialChangedOrAllowed(BasePermission):
    message='You must change your PIN before continuing.'
    code='PIN_CHANGE_REQUIRED'
    allowed={'/api/auth/change-pin/','/api/auth/logout/','/api/auth/refresh/','/api/me/','/api/auth/sessions/'}
    def has_permission(self,request,view):
        user=request.user
        if not getattr(user,'is_authenticated',False):return True
        if getattr(user,'credential_state',None)=='CHANGED':return True
        return request.path in self.allowed or request.path.startswith('/api/auth/sessions/')
