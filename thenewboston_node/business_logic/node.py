from django.conf import settings

from thenewboston_node.core.utils.cryptography import derive_public_key
from thenewboston_node.core.utils.types import hexstr


def get_node_signing_key() -> hexstr:
    signing_key = settings.NODE_SIGNING_KEY
    assert signing_key is not NotImplemented
    return signing_key


# TODO(dmu) LOW: Cache get_node_identifier() function
def get_node_identifier() -> hexstr:
    return derive_public_key(get_node_signing_key())
