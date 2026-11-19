"""JSON Schema -> GBNF, for constrained decoding.

Small local models produce malformed JSON often enough that "ask nicely" is not
a strategy. llama-server accepts a ``grammar`` field on the completion request
that masks the logits, so the model *cannot* emit a token that breaks the shape.

The grammar deliberately allows more than the schema in one respect: object
properties may appear in any order and may repeat. Rejecting a duplicate key is
cheap; forbidding it in the grammar explodes the rule count. Validation runs
afterwards and a single repair attempt covers the remainder.
"""

from __future__ import annotations

import json
from typing import Any

MAX_DEPTH = 4

_HEADER = r"""ws ::= [ \t\n\r]*
integer ::= "-"? [0-9]+
number ::= "-"? [0-9]+ ("." [0-9]+)?
boolean ::= "true" | "false"
string ::= "\"" ( [^"\\] | "\\" . )* "\""
value ::= object | array | string | number | boolean | "null"
object ::= "{" ws (pair (ws "," ws pair)*)? ws "}"
pair ::= string ws ":" ws value
array ::= "[" ws (value (ws "," ws value)*)? ws "]"
"""


def _lit(text: str) -> str:
    """A GBNF string literal for ``text``."""
    out = ['"']
    for ch in text:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _jstr(text: str) -> str:
    """A GBNF expression matching a JSON-quoted string literal for ``text``."""
    inner = json.dumps(str(text), ensure_ascii=False)[1:-1]
    return f'"\\"" {_lit(inner)} "\\""'


class _Builder:
    def __init__(self) -> None:
        self.rules: list[str] = []
        self._n = 0

    def rule(self, expr: str) -> str:
        name = f"r{self._n}"
        self._n += 1
        self.rules.append(f"{name} ::= {expr}")
        return name

    # ------------------------------------------------------------------ values

    def value(self, schema: dict[str, Any] | None, depth: int = 0) -> str:
        schema = schema or {}
        if depth > MAX_DEPTH:
            return "value"

        if "enum" in schema and isinstance(schema["enum"], list):
            return self.rule(self._enum(schema["enum"]))

        kind = schema.get("type")
        if isinstance(kind, list):  # e.g. ["string", "null"]
            alts = [self.value({**schema, "type": k}, depth + 1) for k in kind]
            return self.rule(" | ".join(dict.fromkeys(alts)))

        if kind == "object":
            return self._object(schema, depth)
        if kind == "array":
            return self._array(schema, depth)
        if kind == "integer":
            return "integer"
        if kind == "number":
            return "number"
        if kind == "boolean":
            return "boolean"
        if kind == "string":
            return "string"
        return "value"

    def _enum(self, values: list[Any]) -> str:
        alts: list[str] = []
        for value in values:
            if isinstance(value, bool):
                alts.append("true" if value else "false")
            elif isinstance(value, (int, float)):
                alts.append(_lit(str(value)))
            elif value is None:
                alts.append("null")
            else:
                alts.append(_jstr(str(value)))
        return " | ".join(dict.fromkeys(alts)) or "null"

    def _object(self, schema: dict[str, Any], depth: int) -> str:
        props = schema.get("properties")
        if not isinstance(props, dict) or not props:
            return "object"

        fields = [self.rule(f"{_jstr(key)} ws \":\" ws {self.value(sub, depth + 1)}")
                  for key, sub in props.items()]
        field = self.rule(" | ".join(fields))
        body = self.rule(f'{field} (ws "," ws {field})*')
        return self.rule(f'"{{" ws ({body})? ws "}}"')

    def _array(self, schema: dict[str, Any], depth: int) -> str:
        item = self.value(schema.get("items") if isinstance(schema.get("items"), dict) else None,
                          depth + 1)
        return self.rule(f'"[" ws ({item} (ws "," ws {item})*)? ws "]"')

    def finish(self, root: str) -> str:
        return "\n".join([f"root ::= {root}", *_HEADER.splitlines(), *self.rules]) + "\n"


def from_schema(schema: dict[str, Any] | None) -> str:
    """A grammar whose language is (a superset of) the schema's JSON objects."""
    builder = _Builder()
    return builder.finish(builder.value(schema or {"type": "object"}, 0))


def decision_schema(tools: list[str]) -> dict[str, Any]:
    """JSON Schema for one planner step, for servers that take ``response_format``."""
    return {
        "type": "object",
        "properties": {
            "thought": {"type": "string"},
            "tool": {"type": "string", "enum": ["done", *tools]},
            "final": {"type": "string"},
        },
        "required": ["thought", "tool", "final"],
        "additionalProperties": False,
    }


def decision(tools: list[str]) -> str:
    """Grammar for one planner step: ``{thought, tool, final}``."""
    tool_alts = " | ".join(dict.fromkeys([_jstr("done"), *(_jstr(t) for t in tools)]))
    return (
        f'root ::= "{{" ws {_jstr("thought")} ws ":" ws str ws "," ws '
        f'{_jstr("tool")} ws ":" ws tool ws "," ws '
        f'{_jstr("final")} ws ":" ws str ws "}}"\n'
        f"tool ::= {tool_alts}\n"
        'str ::= "\\"" ( [^"\\\\] | "\\\\" . )* "\\""\n'
        "ws ::= [ \\t\\n\\r]*\n"
    )
