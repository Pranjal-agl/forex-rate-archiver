"""Source registry. To add a bank: subclass Source, then list it here."""

from .base import ParsedSheet, Source
from .hdfc import HDFC
from .icici import ICICI
from .sbi import SBINewYork

REGISTRY: dict[str, type[Source]] = {cls.name: cls for cls in (SBINewYork, HDFC, ICICI)}

__all__ = ["REGISTRY", "Source", "ParsedSheet", "SBINewYork", "HDFC", "ICICI"]
