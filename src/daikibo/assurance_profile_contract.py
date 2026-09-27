"""Closed profile format capabilities shared by wire and read-only consumers.

None denotes an unselected legacy context, not an unknown profile format.
Profile v4 changes selector vocabulary only; its relation wire is registry v2.
"""
from .common import need
from .assurance_relations import REGISTRY_V1_DIGEST, REGISTRY_V2_DIGEST

PROFILE_V1_FORMAT = "assurance.profile.v1"
PROFILE_V2_FORMAT = "assurance.profile.v2"
PROFILE_V3_FORMAT = "assurance.profile.v3"
PROFILE_V4_FORMAT = "assurance.profile.v4"
PROFILE_V5_FORMAT = "assurance.profile.v5"
CANONICAL_PROFILE_FORMATS = frozenset({PROFILE_V2_FORMAT, PROFILE_V3_FORMAT, PROFILE_V4_FORMAT, PROFILE_V5_FORMAT})
_PROFILE_CAPABILITIES = {
    None: (REGISTRY_V1_DIGEST, False),
    PROFILE_V1_FORMAT: (REGISTRY_V1_DIGEST, False),
    PROFILE_V2_FORMAT: (REGISTRY_V1_DIGEST, False),
    PROFILE_V3_FORMAT: (REGISTRY_V2_DIGEST, True),
    PROFILE_V4_FORMAT: (REGISTRY_V2_DIGEST, True),
    PROFILE_V5_FORMAT: (REGISTRY_V2_DIGEST, True),
}


def profile_registry(profile_format):
    need(profile_format is None or type(profile_format) is str,
         "invalid_profile", "Profile format must be a string")
    need(profile_format in _PROFILE_CAPABILITIES,
         "invalid_profile", "Profile format is unsupported", profile_format)
    return _PROFILE_CAPABILITIES[profile_format][0]


def profile_has_outputs(profile_format):
    profile_registry(profile_format)
    return _PROFILE_CAPABILITIES[profile_format][1]
