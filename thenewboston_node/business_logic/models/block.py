import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Type, TypeVar

from thenewboston_node.business_logic.models.node import Node
from thenewboston_node.business_logic.validators import (
    validate_exact_value, validate_in, validate_not_empty, validate_not_none, validate_type
)
from thenewboston_node.core.logging import timeit_method, validates
from thenewboston_node.core.utils.cryptography import derive_public_key
from thenewboston_node.core.utils.dataclass import cover_docstring, revert_docstring
from thenewboston_node.core.utils.types import hexstr

from .base import BaseDataclass
from .block_message import BlockMessage
from .mixins.compactable import MessagpackCompactableMixin
from .mixins.metadata import MetadataMixin
from .mixins.signable import SignableMixin
from .signed_change_request import (  # noqa: I101
    SIGNED_CHANGE_REQUEST_TYPE_MAP, CoinTransferSignedChangeRequest, SignedChangeRequest
)

T = TypeVar('T', bound='Block')

logger = logging.getLogger(__name__)


@revert_docstring
@dataclass
@cover_docstring
class Block(SignableMixin, MessagpackCompactableMixin, MetadataMixin, BaseDataclass):
    """
    Blocks represent a description of change to the network.
    """
    message: BlockMessage

    # TODO(dmu) HIGH: Do we need to store hash? We can always calculate it from block itself
    #                 We recalculate it anyway for validation
    hash: Optional[hexstr] = field(  # noqa: A003
        default=None,
        metadata={
            'example_value': 'dc6671e1132cbb7ecbc190bf145b5a5cfb139ca502b5d66aafef4d096f4d2709',
            'is_serialized_optional': False,
        }
    )
    # We have to define `meta` here because otherwise we are getting
    # "non-default argument 'signer' follows default argument" error
    meta: Optional[dict[str, Any]] = field(  # noqa: A003
        default=None, metadata={
            'is_serializable': False,
        }
    )

    @classmethod
    def deserialize_from_dict(cls, dict_, complain_excessive_keys=True, exclude=()):
        dict_ = dict_.copy()
        message_dict = dict_.pop('message', None)
        validate_not_none(f'{cls.humanized_class_name} message', message_dict)
        validate_type(f'{cls.humanized_class_name} message', message_dict, dict)

        signed_change_request_dict = message_dict.get('signed_change_request')
        validate_not_none(f'{cls.humanized_class_name} message.signed_change_request', signed_change_request_dict)
        validate_type(f'{cls.humanized_class_name} message.signed_change_request', signed_change_request_dict, dict)

        instance_block_type = message_dict.get('block_type')
        validate_not_none(f'{cls.humanized_class_name} message.block_type', instance_block_type)
        validate_in(
            f'{cls.humanized_class_name} message.block_type', instance_block_type, SIGNED_CHANGE_REQUEST_TYPE_MAP
        )
        signed_change_request_class = SIGNED_CHANGE_REQUEST_TYPE_MAP[instance_block_type]
        signed_change_request_obj = signed_change_request_class.deserialize_from_dict(signed_change_request_dict)

        message_obj = BlockMessage.deserialize_from_dict(
            message_dict, override={'signed_change_request': signed_change_request_obj}
        )

        return super().deserialize_from_dict(
            dict_, complain_excessive_keys=complain_excessive_keys, override={'message': message_obj}
        )

    @classmethod
    @timeit_method(level=logging.INFO, is_class_method=True)
    def create_from_signed_change_request(
        cls: Type[T],
        blockchain,
        signed_change_request: SignedChangeRequest,
        pv_signing_key,
    ) -> T:
        block = cls(
            signer=derive_public_key(pv_signing_key),
            message=BlockMessage.from_signed_change_request(blockchain, signed_change_request)
        )
        block.sign(pv_signing_key)
        block.hash_message()
        return block

    @classmethod
    def create_from_main_transaction(
        cls: Type[T],
        *,
        blockchain,
        recipient: hexstr,
        amount: int,
        request_signing_key: hexstr,
        pv_signing_key: hexstr,
        preferred_node: Node,
    ) -> T:
        # TODO(dmu) HIGH: This method is only used in tests (mostly for test data creation). Business rules
        #                 do not suggest creation from main transaction. There this method must be removed
        #                 from Block interface
        signed_change_request = CoinTransferSignedChangeRequest.from_main_transaction(
            blockchain=blockchain,
            recipient=recipient,
            amount=amount,
            signing_key=request_signing_key,
            node=preferred_node,
        )
        return cls.create_from_signed_change_request(blockchain, signed_change_request, pv_signing_key)

    def hash_message(self) -> None:
        message_hash = self.message.get_hash()
        stored_message_hash = self.hash
        if stored_message_hash and stored_message_hash != message_hash:
            logger.warning('Overwriting existing message hash')

        self.hash = message_hash

    def yield_account_states(self):
        yield from self.message.yield_account_states()

    def get_block_number(self):
        return self.message.block_number

    @validates('block')
    def validate(self, blockchain):
        validate_not_empty(f'{self.humanized_class_name} message', self.message)
        with validates(f'block number {self.message.block_number} (identifier: {self.message.block_identifier})'):
            self.message.validate(blockchain)
            validate_exact_value(f'{self.humanized_class_name} hash', self.hash, self.message.get_hash())
            with validates('block signature'):
                self.validate_signature()
