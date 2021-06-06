from dataclasses import dataclass
from typing import Optional

from dataclasses_json import dataclass_json

from thenewboston_node.business_logic.enums import NodeType

from .base import BaseDataclass


@dataclass
class Node(BaseDataclass):
    identifier: str
    """Identifier"""

    network_addresses: list[str]
    """Network addresses"""

    fee_amount: int
    """Fee amount"""

    fee_account: Optional[str] = None
    """Fee account"""

    def serialize_to_dict(self, skip_none_values=True, coerce_to_json_types=True, exclude=('identifier',)):
        return super(Node, self).serialize_to_dict(
            skip_none_values=skip_none_values,
            coerce_to_json_types=coerce_to_json_types,
            exclude=exclude,
        )


@dataclass_json
@dataclass
class PrimaryValidator(Node):
    node_type: str = NodeType.PRIMARY_VALIDATOR.value


@dataclass_json
@dataclass
class RegularNode(Node):
    node_type: str = NodeType.REGULAR_NODE.value
