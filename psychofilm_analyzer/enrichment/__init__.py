"""Phase A: multi-source enrichment profiles (gather only, no scoring)."""

from .profile import EnrichmentProfile, EvidenceBag, build_enrichment_profile
from .export import write_profiles

__all__ = [
    "EnrichmentProfile",
    "EvidenceBag",
    "build_enrichment_profile",
    "write_profiles",
]
