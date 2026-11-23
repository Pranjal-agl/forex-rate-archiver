"""Exception hierarchy. The split matters: retries help a FetchError but never a SchemaDriftError."""


class ArchiverError(Exception):
    """Base class for all archiver errors."""


class FetchError(ArchiverError):
    """Network-level failure: timeout, 5xx, bot wall, empty body. Retryable."""


class SchemaDriftError(ArchiverError):
    """The page/PDF was fetched but no longer looks like we expect.

    Raised instead of guessing, so a layout change produces a loud failure and
    an alert rather than a plausible-looking wrong number in the archive.
    Not retryable: the same bytes will fail the same way.
    """


class StaleDataError(ArchiverError):
    """The document parsed fine but its own published date is too old.

    This is the failure that silently froze HDFC at 94.24 for 36 days: a
    hard-coded URL kept serving an old sheet, which parsed perfectly.
    """
