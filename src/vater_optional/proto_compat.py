"""Protocol Buffers schema evolution check (task.optional_more_protocols.005).

The compatibility test a future gRPC interface must pass before a new .proto version ships:
compared with the previous version, no existing field may be broken. Standard library only,
for flat proto3 messages (a nested message, oneof, map, enum, import or proto2 'required'
is refused as unsupported rather than skipped, so nothing is checked silently).

Breaking, per field number of the old version:
  - the message or the field disappears without its number AND name being reserved;
  - the field's type or cardinality (singular, optional, repeated) changes;
  - the field is renamed (the binary format survives, JSON and text formats do not);
  - a new field takes a number or a name the old version reserved.
Adding a field with a new number is compatible: old readers skip it.
"""
from __future__ import annotations

import re

_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_MESSAGE = re.compile(r"message\s+([A-Za-z_]\w*)\s*\{([^{}]*)\}", re.S)
_FIELD = re.compile(r"^(optional|repeated)?\s*([A-Za-z_][\w.]*)\s+([A-Za-z_]\w*)\s*=\s*(\d+)\s*(\[[^\]]*\])?$")
_RESERVED = re.compile(r"^reserved\s+(.+)$")
_UNSUPPORTED = re.compile(r"\b(oneof|map\s*<|enum|import|required|extend|group)\b")


class Unsupported(ValueError):
    pass


def parse(text: str) -> dict[str, dict]:
    source = _COMMENT.sub("", text)
    if _UNSUPPORTED.search(source):
        raise Unsupported("only flat proto3 messages with scalar or message fields are checked")
    remainder = _MESSAGE.sub("", source)
    if "{" in remainder or "}" in remainder:
        raise Unsupported("nested messages are not checked")
    messages: dict[str, dict] = {}
    for name, body in _MESSAGE.findall(source):
        fields, reserved_numbers, reserved_names = {}, set(), set()
        for statement in (part.strip() for part in body.split(";")):
            if not statement:
                continue
            reserved = _RESERVED.match(statement)
            if reserved:
                for item in (piece.strip() for piece in reserved.group(1).split(",")):
                    if item.startswith('"'):
                        reserved_names.add(item.strip('"'))
                    elif " to " in item:
                        low, high = (int(value) for value in item.split(" to "))
                        reserved_numbers.update(range(low, high + 1))
                    else:
                        reserved_numbers.add(int(item))
                continue
            field = _FIELD.match(statement)
            if field is None:
                raise Unsupported(f"unrecognized statement in {name}: {statement!r}")
            label, kind, field_name, number = field.group(1) or "singular", field.group(2), field.group(3), int(field.group(4))
            if number in fields:
                raise Unsupported(f"{name}: field number {number} used twice")
            fields[number] = {"label": label, "type": kind, "name": field_name}
        messages[name] = {"fields": fields, "reserved_numbers": reserved_numbers, "reserved_names": reserved_names}
    return messages


def breaking_changes(old_text: str, new_text: str) -> list[str]:
    old, new = parse(old_text), parse(new_text)
    problems = []
    for message, before in old.items():
        after = new.get(message)
        if after is None:
            problems.append(f"{message}: the message was removed")
            continue
        for number, field in before["fields"].items():
            current = after["fields"].get(number)
            where = f"{message}.{field['name']} = {number}"
            if current is None:
                if number not in after["reserved_numbers"] or field["name"] not in after["reserved_names"]:
                    problems.append(f"{where}: removed without reserving its number and name")
                continue
            if current["type"] != field["type"]:
                problems.append(f"{where}: type changed from {field['type']} to {current['type']}")
            if current["label"] != field["label"]:
                problems.append(f"{where}: cardinality changed from {field['label']} to {current['label']}")
            if current["name"] != field["name"]:
                problems.append(f"{where}: renamed to {current['name']}")
        for number, field in after["fields"].items():
            if number in before["reserved_numbers"]:
                problems.append(f"{message}.{field['name']} = {number}: reuses a reserved number")
            if field["name"] in before["reserved_names"]:
                problems.append(f"{message}.{field['name']} = {number}: reuses a reserved name")
    return problems
