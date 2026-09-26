import phonenumbers
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from .models import User

def normalize_phone(value):
 try:p=phonenumbers.parse(str(value),None)
 except phonenumbers.NumberParseException:raise ValidationError('Enter an international phone number, e.g. +2348012345678.')
 if not phonenumbers.is_valid_number(p):raise ValidationError('Invalid phone number.')
 return phonenumbers.format_number(p,phonenumbers.PhoneNumberFormat.E164)
def initial_pin(phone):return ''.join(c for c in normalize_phone(phone) if c.isdigit())[:6]
@transaction.atomic
def create_member(*,actor,phone,full_name,email=''):
 """Only an administrator can create a member.

 The initial PIN is the first six digits of the normalized phone number and
 must be changed at first sign-in (credential_state=INITIAL). It is never
 returned by the API nor written to any log.
 """
 from rest_framework.exceptions import PermissionDenied,ValidationError as DRFValidationError
 if actor.role!=User.Role.ADMIN:raise PermissionDenied()
 try:normalized=normalize_phone(phone)
 except ValidationError as exc:raise DRFValidationError({'phone':exc.messages})
 if User.objects.filter(phone=normalized).exists():raise DRFValidationError({'phone':['A user with that phone number already exists.']})
 pin=initial_pin(normalized)
 return User.objects.create_user(phone=normalized,password=pin,full_name=full_name,email=email,role=User.Role.MEMBER,credential_state=User.Credential.INITIAL)
def register_failure(user,*,limit=5,minutes=15):
 """Count a failed sign-in and lock the account once the limit is reached.

 Called for a *known* phone number only; an unknown number produces the same
 generic response so accounts cannot be enumerated.
 """
 if not user:return
 user.failed_login_count+=1
 if user.failed_login_count>=max(1,limit):user.locked_until=timezone.now()+timezone.timedelta(minutes=max(1,minutes))
 user.save(update_fields=['failed_login_count','locked_until'])
