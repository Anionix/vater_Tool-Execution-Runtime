"""Shared pre-parse JSON nesting bound for local byte inputs."""


class WireInputRejected(ValueError):
    pass


def check_json_depth(text: str, limit: int) -> None:
    """Count JSON structure nesting without confusing delimiters in strings."""
    depth = 0
    inside = False
    escape = False
    for char in text:
        if inside:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                inside = False
            continue
        if char == '"':
            inside = True
        elif char in "{[":
            depth += 1
            if depth > limit:
                raise WireInputRejected("JSON nesting too deep")
        elif char in "}]":
            depth -= 1
            if depth < 0:
                raise WireInputRejected("unexpected JSON delimiter")
    if depth != 0 or inside:
        raise WireInputRejected("incomplete JSON structure")
