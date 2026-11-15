"""The JSON value type shared by every module that handles model or page data."""

from __future__ import annotations

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
