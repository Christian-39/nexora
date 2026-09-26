from rest_framework import serializers
from .models import PlatformConfiguration
class PlatformSerializer(serializers.ModelSerializer):
 class Meta:
  model=PlatformConfiguration
  exclude=['singleton']
  read_only_fields=['created_at','updated_at']
 def validate_max_message_length(self,v):
  if not 100<=v<=20000:raise serializers.ValidationError('Must be between 100 and 20000.')
  return v
 def validate_max_image_size_mb(self,v):
  if not 1<=v<=50:raise serializers.ValidationError('Must be between 1 and 50 MB.')
  return v
 def validate_max_video_size_mb(self,v):
  if not 5<=v<=2048:raise serializers.ValidationError('Must be between 5 and 2048 MB.')
  return v
 def validate_max_voice_duration_seconds(self,v):
  if not 10<=v<=3600:raise serializers.ValidationError('Must be between 10 and 3600 seconds.')
  return v
 def validate_message_edit_window_minutes(self,v):
  if v>1440:raise serializers.ValidationError('Cannot exceed 1440 minutes.')
  return v
 def validate_message_delete_window_minutes(self,v):
  if v>10080:raise serializers.ValidationError('Cannot exceed seven days.')
  return v
