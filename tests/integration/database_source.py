"""Secret-safe environment values used by subprocess-based tests."""

from collections.abc import Iterator
from collections.abc import Mapping


class SubprocessEnvironment(Mapping[str, str]):
    """Immutable environment whose representation conceals keys and values."""

    __slots__ = ("_data",)

    def __init__(self, *layers: Mapping[str, str]) -> None:
        data: dict[str, str] = {}
        for layer in layers:
            data.update(layer)
        self._data = data

    def __getitem__(self, key: str) -> str:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"<SubprocessEnvironment: {len(self._data)} variables, values hidden>"
