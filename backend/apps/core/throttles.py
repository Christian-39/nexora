from rest_framework.throttling import UserRateThrottle
class MessageThrottle(UserRateThrottle):scope='messages'
class SearchThrottle(UserRateThrottle):scope='search'
class UploadThrottle(UserRateThrottle):scope='uploads'
class PushThrottle(UserRateThrottle):scope='push'
class CredentialThrottle(UserRateThrottle):scope='credentials'
