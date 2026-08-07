"""Typed failures used to stop unsafe pipeline transitions."""


class CrawlerError(Exception):
    """Base class for crawler failures."""


class IntegrityError(CrawlerError):
    """Stored bytes or a declared digest failed verification."""


class InvalidTransition(CrawlerError):
    """A state transition violated the snapshot or assertion lifecycle."""


class IncompleteSnapshot(CrawlerError):
    """Projection was attempted without explicit completeness evidence."""


class QuarantineRequired(CrawlerError):
    """A safety-relevant validation finding requires quarantine."""


class DuplicateRecordKey(QuarantineRequired):
    """One snapshot emitted conflicting records under the same native key."""


class ReleaseValidationError(CrawlerError):
    """A staged release did not satisfy the published bundle contract."""


class FetchError(CrawlerError):
    """A live fetch could not be completed after exhausting retries."""

