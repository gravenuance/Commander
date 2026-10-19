"""Typed accessors for untrusted JSON, raising one error type with the path to the bad field."""

from collections.abc import Mapping, Sequence
from typing import cast


class ShapeError(ValueError):
    """A JSON document did not have the expected shape."""


def as_object(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ShapeError(f"{where}: expected an object, got {type(value).__name__}")
    return cast("Mapping[str, object]", value)


def as_list(value: object, where: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ShapeError(f"{where}: expected a list, got {type(value).__name__}")
    return cast("Sequence[object]", value)


def req_str(obj: Mapping[str, object], key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise ShapeError(f"{where}.{key}: expected a string")
    return value


def opt_str(obj: Mapping[str, object], key: str, where: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ShapeError(f"{where}.{key}: expected a string or null")
    return value


def req_int(obj: Mapping[str, object], key: str, where: str) -> int:
    value = obj.get(key)
    # bool is a subclass of int, and a true/false here means the field changed meaning.
    if not isinstance(value, int) or isinstance(value, bool):
        raise ShapeError(f"{where}.{key}: expected an integer")
    return value


def opt_int(obj: Mapping[str, object], key: str, where: str) -> int | None:
    if obj.get(key) is None:
        return None
    return req_int(obj, key, where)


def req_float(obj: Mapping[str, object], key: str, where: str) -> float:
    """A number; JSON writes whole numbers such as 0 without a fraction, so an integer counts too."""
    value = obj.get(key)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ShapeError(f"{where}.{key}: expected a number")
    return float(value)


def opt_float(obj: Mapping[str, object], key: str, where: str) -> float | None:
    if obj.get(key) is None:
        return None
    return req_float(obj, key, where)


def req_bool(obj: Mapping[str, object], key: str, where: str) -> bool:
    value = obj.get(key)
    if not isinstance(value, bool):
        raise ShapeError(f"{where}.{key}: expected true or false")
    return value


def opt_bool(obj: Mapping[str, object], key: str, where: str) -> bool | None:
    if obj.get(key) is None:
        return None
    return req_bool(obj, key, where)


def req_object(obj: Mapping[str, object], key: str, where: str) -> Mapping[str, object]:
    return as_object(obj.get(key), f"{where}.{key}")


def req_list(obj: Mapping[str, object], key: str, where: str) -> Sequence[object]:
    return as_list(obj.get(key), f"{where}.{key}")


def str_list(obj: Mapping[str, object], key: str, where: str) -> tuple[str, ...]:
    """A list of strings; a missing or null field is an empty list."""
    value = obj.get(key)
    if value is None:
        return ()
    items = as_list(value, f"{where}.{key}")
    result: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise ShapeError(f"{where}.{key}[{index}]: expected a string")
        result.append(item)
    return tuple(result)
