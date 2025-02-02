#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from typing import Any, List, Optional

from ax.exceptions.storage import SQADecodeError
from ax.utils.common.base import Base, SortableBase


def is_foreign_key_field(field: str) -> bool:
    """Return true if field name is a foreign key field, i.e. ends in `_id`."""
    return len(field) > 3 and field[-3:] == "_id"


def copy_db_ids(source: Any, target: Any, path: Optional[List[str]] = None) -> None:
    """Takes as input two objects, `source` and `target`, that should be identical,
    except that `source` has _db_ids set and `target` doesn't. Recursively copies the
    _db_ids from `source` to `target`.

    Raise a SQADecodeError when the assumption of equality on `source` and `target`
    is violated, since this method is meant to be used when returning a new
    user-facing object after saving.
    """
    if not path:
        path = []

    error_message_prefix = (
        f"Error encountered while traversing source {path + [str(source)]} and "
        f"target {path + [str(target)]}: "
    )

    if len(path) > 10:
        # this shouldn't happen, but is a precaution against accidentally
        # introducing infinite loops
        return

    if type(source) != type(target):
        raise SQADecodeError(
            error_message_prefix + "Encountered two objects of different "
            f"types: {type(source)} and {type(target)}."
        )

    if isinstance(source, Base):
        for attr, val in source.__dict__.items():
            if attr.endswith("_db_id"):
                # we're at a "leaf" node; copy the db_id and return
                setattr(target, attr, val)
                continue

            # skip over _experiment to prevent infinite loops,
            # and ignore doubly private attributes
            if attr == "_experiment" or attr.startswith("__"):
                continue

            copy_db_ids(val, getattr(target, attr), path + [attr])

    elif isinstance(source, (list, set)):
        source = list(source)
        target = list(target)

        if len(source) != len(target):
            raise SQADecodeError(
                error_message_prefix + "Encountered lists of different lengths."
            )

        # Safe to skip over lists of types (e.g. transforms)
        if len(source) == 0 or isinstance(source[0], type):
            return

        if isinstance(source[0], Base) and not isinstance(source[0], SortableBase):
            raise SQADecodeError(
                error_message_prefix + f"Cannot sort instances of {type(source[0])}; "
                "sorting is only defined on instances of SortableBase."
            )

        source = sorted(source)
        target = sorted(target)
        for index, x in enumerate(source):
            copy_db_ids(x, target[index], path + [str(index)])

    elif isinstance(source, dict):
        for k, v in source.items():
            if k not in target:
                raise SQADecodeError(
                    error_message_prefix + "Encountered key only present "
                    f"in source dictionary: {k}."
                )
            copy_db_ids(v, target[k], path + [k])

    else:
        return
