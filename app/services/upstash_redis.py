import redis.asyncio as aioredis
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from cryptography.fernet import Fernet
class UpstashRedis:
    def __init__(self,  url: str, encryption_key: str):
            """"
            Initializes the Upstash Redis client with encryption for the OAuth store from the base client.  
            """
            #initialize the  oauth store with encryption, no need to do lifespan management, RedisStore does it all
            self.base_redis_client = aioredis.from_url(url=url, ssl_cert_reqs=None, decode_responses=False)
            self.oauth_store = FernetEncryptionWrapper(
                key_value=RedisStore(client=self.base_redis_client),
                fernet=Fernet(encryption_key),
            )
    