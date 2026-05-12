import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def find_field_in_nested_structure(
    data: dict, field_name: str, current_path: str = ""
) -> list[str]:
    """
    Recursively search for all paths ending with field_name.

    Args:
        data: The dictionary to search
        field_name: The field name to find
        current_path: Current path being explored (used in recursion)

    Returns:
        List of dotted paths where the field was found
        (e.g., ["data.entry.previouslySent", "data.other.previouslySent"])
    """
    results = []

    for key, value in data.items():
        new_path = f"{current_path}.{key}" if current_path else key

        if key == field_name:
            results.append(new_path)

        if isinstance(value, dict):
            results.extend(
                find_field_in_nested_structure(value, field_name, new_path)
            )
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    results.extend(
                        find_field_in_nested_structure(
                            item, field_name, f"{new_path}[{i}]"
                        )
                    )

    return results


def get_nested_value(data: dict, field_path: str) -> tuple[Any, str | None]:
    """
    Navigate to field_path in data and extract the value.

    If the target is a dict with 'dataText' key, returns the dataText value
    and mode "dataText". Otherwise returns the value directly with mode None.

    Args:
        data: The dictionary to navigate
        field_path: Dotted path (e.g., "data.entry.previouslySent")
                   Can include array indices like "data.list[0].field"

    Returns:
        Tuple of (value, extraction_mode) where:
        - value: The extracted value (string from dataText or direct value)
        - extraction_mode: "dataText" if extracted from dataText, None otherwise

    Raises:
        KeyError: If field path not found
        TypeError: If trying to navigate through non-dict/non-list
    """
    current = data
    parts = _parse_path(field_path)

    for part in parts:
        if isinstance(part, int):
            # Array index
            if not isinstance(current, list):
                raise TypeError(
                    f"Expected list for array index {part} in path {field_path}"
                )
            if part >= len(current):
                raise KeyError(
                    f"Array index {part} out of range in path {field_path}"
                )
            current = current[part]
        else:
            # Dictionary key
            if not isinstance(current, dict):
                raise TypeError(
                    f"Cannot navigate through non-dict at '{part}' in path {field_path}"
                )
            if part not in current:
                raise KeyError(f"Field path {field_path} not found at '{part}'")
            current = current[part]

    # Check if current is a dict with dataText
    if isinstance(current, dict) and "dataText" in current:
        return (current["dataText"], "dataText")

    return (current, None)


def set_nested_value(
    data: dict, field_path: str, value: str, mode: str | None
) -> None:
    """
    Set translated value at field_path.

    If mode is "dataText", updates obj["dataText"].
    Otherwise replaces the field entirely.

    Args:
        data: The dictionary to modify (mutated in-place)
        field_path: Dotted path to the field
        value: The translated value to set
        mode: "dataText" to update only dataText property, None to replace entire value

    Raises:
        KeyError: If field path not found
        TypeError: If trying to navigate through non-dict/non-list
        ValueError: If mode is "dataText" but target is not a dict with dataText
    """
    parts = _parse_path(field_path)
    current = data

    # Navigate to parent
    for part in parts[:-1]:
        if isinstance(part, int):
            if not isinstance(current, list):
                raise TypeError(
                    f"Expected list for array index {part} in path {field_path}"
                )
            current = current[part]
        else:
            if not isinstance(current, dict):
                raise TypeError(
                    f"Cannot navigate through non-dict at '{part}' in path {field_path}"
                )
            current = current[part]

    # Set the final value
    final_key = parts[-1]

    if isinstance(final_key, int):
        # Setting in an array
        if not isinstance(current, list):
            raise TypeError(f"Expected list for array index {final_key}")

        if mode == "dataText":
            if not isinstance(current[final_key], dict) or "dataText" not in current[final_key]:
                raise ValueError(
                    f"Expected dict with dataText at array index {final_key} in {field_path}"
                )
            current[final_key]["dataText"] = value
        else:
            current[final_key] = value
    else:
        # Setting in a dict
        if not isinstance(current, dict):
            raise TypeError(f"Expected dict for key {final_key}")

        if mode == "dataText":
            if not isinstance(current[final_key], dict) or "dataText" not in current[final_key]:
                raise ValueError(
                    f"Expected dict with dataText at {field_path}"
                )
            current[final_key]["dataText"] = value
        else:
            current[final_key] = value


def _parse_path(path: str) -> list[str | int]:
    """
    Parse a dotted path with optional array indices into parts.

    Examples:
        "data.entry.name" -> ["data", "entry", "name"]
        "data.list[0].field" -> ["data", "list", 0, "field"]
        "items[2]" -> ["items", 2]

    Args:
        path: Dotted path string

    Returns:
        List of path parts (strings for keys, ints for array indices)
    """
    parts = []
    # Split by dots and process each segment
    for segment in path.split("."):
        # Check if segment contains array index
        match = re.match(r"^([^\[]+)\[(\d+)\]$", segment)
        if match:
            # Segment like "list[0]"
            key = match.group(1)
            index = int(match.group(2))
            parts.append(key)
            parts.append(index)
        else:
            # Regular key
            parts.append(segment)

    return parts
