from rest_framework import serializers
from .models import User
class UserSerializer(serializers.ModelSerializer):
 class Meta:model=User;fields=['id','phone','full_name','email','role','is_active','credential_state','last_login','last_seen','created_at'];read_only_fields=fields
class MemberCreateSerializer(serializers.Serializer):
 full_name=serializers.CharField(max_length=150);phone=serializers.CharField(max_length=18);email=serializers.EmailField(required=False,allow_blank=True)
class PinSerializer(serializers.Serializer):
 current_pin=serializers.RegexField(r'^\d{6}$');new_pin=serializers.RegexField(r'^\d{6}$')

class ProfileSerializer(serializers.ModelSerializer):
 class Meta:
  model=User;fields=['id','phone','full_name','role','theme','show_phone','show_last_seen','push_enabled','last_seen']
  read_only_fields=['id','phone','role','last_seen']
