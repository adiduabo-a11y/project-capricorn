"""A deliberately small JSON Schema validator.

Only the keywords Capri's own tool manifests use are supported. Pulling in the
full ``jsonschema`` package would be fine functionally, but the point of this
module is to produce *model-readable* error strings that the repair pass can act
on, which a stock validator does not give us.
"""

from __future__ import annotations

from typing import Any

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def validate(schema: dict[str, Any] | None, value: Any, path: str = "") -> list[str]:
    """Return a list of human-readable problems; empty means valid."""
    schema = schema or {}
    errors: list[str] = []
    where = path or "args"

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: must be one of {schema['enum']!r}, got {value!r}")
        return errors

    kind = schema.get("type")
    if isinstance(kind, str):
        check = _TYPE_CHECKS.get(kind)
        if check and not check(value):
            errors.append(f"{where}: expected {kind}, got {type(value).__name__}")
            return errors
    elif isinstance(kind, list) and kind and not any(
        _TYPE_CHECKS.get(k, lambda _v: True)(value) for k in kind
    ):
        errors.append(f"{where}: expected one of {kind}, got {type(value).__name__}")
        return errors

    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{where}: missing required property {key!r}")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{where}: unexpected property {key!r}")
        for key, sub in props.items():
            if key in value:
                errors.extend(validate(sub, value[key], f"{where}.{key}"))

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            errors.extend(validate(schema["items"], item, f"{where}[{index}]"))

    cases = schema.get("oneOf") or schema.get("anyOf")
    if isinstance(cases, list) and cases:
        if not any(not validate(case, value, where) for case in cases):
            errors.append(f"{where}: does not match any allowed shape")

    return errors
