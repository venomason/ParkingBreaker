"""Pure local credential-shape policy; this cannot establish authentication."""


def valid_header_credential(value: str) -> bool:
    """A nonempty ASCII token without HTTP whitespace or control bytes."""
    return bool(value) and value.isascii() and all(32 < ord(char) < 127 for char in value)
