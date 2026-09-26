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
 if actor.role!=User.Role.ADMIN:raise PermissionError
 normalized=normalize_phone(phone); pin=initial_pin(normalized)
 return User.objects.create_user(phone=normalized,password=pin,full_name=full_name,email=email,role=User.Role.MEMBER,credential_state=User.Credential.INITIAL)
def register_failure(user):
 if not user:return
 user.failed_login_count+=1
 if user.failed_login_count>=5:user.locked_until=timezone.now()+timezone.timedelta(minutes=15)
 user.save(update_fields=['failed_login_count','locked_until'])
