from dataclasses import asdict, dataclass
from enum import StrEnum


class DataStatus(StrEnum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    MISSING = "missing"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"
    FAILED = "failed"


@dataclass(frozen=True)
class DataResult:
    status: DataStatus
    reason: str = ""
    file: str | None = None
    sha256: str | None = None

    def __post_init__(self):
        usable = self.status in {
            DataStatus.AVAILABLE,
            DataStatus.PARTIAL,
        }

        if usable and not self.file:
            raise ValueError("Usable data must specify an input file")

        if not usable and self.file is not None:
            raise ValueError(
                "Unavailable data must not authorize an input file"
            )

        if not usable and not self.reason:
            raise ValueError(
                "Unavailable data must explain why it cannot be used"
            )

    def to_dict(self):
        return asdict(self)


class DataUnavailable(Exception):
    """An expected data problem, not a programming error."""

    def __init__(self, status: DataStatus, reason: str):
        if status not in {
            DataStatus.MISSING,
            DataStatus.UNSUPPORTED,
            DataStatus.UNVERIFIED,
            DataStatus.FAILED,
        }:
            raise ValueError("Invalid unavailable-data status")

        super().__init__(reason)
        self.status = status
        self.reason = reason