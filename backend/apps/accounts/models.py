import uuid
from django.contrib.auth.models import AbstractBaseUser,PermissionsMixin,BaseUserManager
from django.db import models
from django.core.validators import RegexValidator
from apps.core.models import TimeStampedModel
class UserManager(BaseUserManager):
 def create_user(self,phone,password=None,**extra):
  from .services import normalize_phone
  phone=normalize_phone(phone); u=self.model(phone=phone,**extra); u.set_password(password); u.save(using=self._db); return u
 def create_superuser(self,phone,password,**extra):
  extra.update(role='ADMIN',is_staff=True,is_superuser=True,is_active=True,credential_state='CHANGED'); return self.create_user(phone,password,**extra)
class User(AbstractBaseUser,PermissionsMixin,TimeStampedModel):
 class Role(models.TextChoices): ADMIN='ADMIN'; MEMBER='MEMBER'
 class Credential(models.TextChoices): INITIAL='INITIAL'; CHANGED='CHANGED'; RESET_REQUIRED='RESET_REQUIRED'
 id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False); phone=models.CharField(max_length=18,unique=True,db_index=True); full_name=models.CharField(max_length=150); email=models.EmailField(blank=True); role=models.CharField(max_length=10,choices=Role.choices); credential_state=models.CharField(max_length=20,choices=Credential.choices,default=Credential.INITIAL); is_active=models.BooleanField(default=True,db_index=True); is_staff=models.BooleanField(default=False); last_seen=models.DateTimeField(null=True,blank=True); deactivated_at=models.DateTimeField(null=True,blank=True); failed_login_count=models.PositiveSmallIntegerField(default=0); locked_until=models.DateTimeField(null=True,blank=True);theme=models.CharField(max_length=10,choices=[('SYSTEM','SYSTEM'),('LIGHT','LIGHT'),('DARK','DARK')],default='SYSTEM');show_phone=models.BooleanField(default=False);show_last_seen=models.BooleanField(default=True);push_enabled=models.BooleanField(default=True)
 USERNAME_FIELD='phone'; REQUIRED_FIELDS=['full_name']; objects=UserManager()
class DeviceSession(TimeStampedModel):
 id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False); user=models.ForeignKey(User,on_delete=models.CASCADE,related_name='device_sessions'); jti=models.CharField(max_length=64,unique=True); device_label=models.CharField(max_length=120,blank=True); ip_hash=models.CharField(max_length=64,blank=True); last_active=models.DateTimeField(auto_now=True); expires_at=models.DateTimeField(); revoked_at=models.DateTimeField(null=True,blank=True)
