from django.core.cache import cache
from .models import PlatformConfiguration
def messaging_policy():
 key='platform:messaging_policy';value=cache.get(key)
 if value is not None:return value
 c=PlatformConfiguration.objects.first()
 value={'max_message_length':c.max_message_length if c else 5000,'max_image_size_mb':c.max_image_size_mb if c else 15,'max_video_size_mb':c.max_video_size_mb if c else 250,'max_voice_duration_seconds':c.max_voice_duration_seconds if c else 600,'message_edit_window_minutes':c.message_edit_window_minutes if c else 15,'message_delete_window_minutes':c.message_delete_window_minutes if c else 15,'allow_delete_everyone':c.allow_delete_everyone if c else True,'allow_member_name_edit':c.allow_member_name_edit if c else True}
 cache.set(key,value,60);return value
