"""One headless turn of a coding agent, run through the vendor's own SDK.

    from callva.harness_runner import Profile, run

    result = run(prompt, Profile.from_dict(config), cwd=".")
    if result.ok:
        print(result.answer)
    else:
        print(result.failure.kind, result.failure.message)

`DESIGN.md` in the repository is the contract.
"""

from .binaries import Probe, find_binary, probe
from .discovery import (
    ProfileFile,
    ProfileListing,
    ProfileNotFound,
    find_profile,
    find_profile_file,
    list_profiles,
    machine_folder,
)
from .discovery import (
    check_name as check_profile_name,
)
from .guard import TESTED_VERSIONS
from .profile import (
    HARNESSES,
    PROFILE_SCHEMA,
    SESSION_MARKERS,
    Profile,
    ProfileError,
    Session,
    load_profile_schema,
    validate_profile,
)
from .renamed import refuse_module_attribute
from .result import Failure, FailureKind, Result, Started, Tokens
from .run import run
from .store import (
    PROFILE_STORE_MAJOR,
    PROFILE_STORE_MINOR,
    PROFILE_STORE_OWNER,
    PROFILE_STORE_STEPS,
    ProfileConflict,
    ResolvedProfile,
    StoredProfile,
    active_profile,
    check_spec,
    profile_history,
    profile_text,
    profile_version,
    remove_profile,
    resolve_among,
    resolve_profile,
    resolve_profiles,
    save_profile,
    shipped_profile_names,
    stored_profiles,
)
from .version import __version__


def __getattr__(name: str):
    raise refuse_module_attribute(__name__, name)


__all__ = [
    "HARNESSES",
    "PROFILE_SCHEMA",
    "PROFILE_STORE_MAJOR",
    "PROFILE_STORE_MINOR",
    "PROFILE_STORE_OWNER",
    "PROFILE_STORE_STEPS",
    "SESSION_MARKERS",
    "TESTED_VERSIONS",
    "Failure",
    "FailureKind",
    "Probe",
    "Profile",
    "ProfileConflict",
    "ProfileError",
    "ProfileFile",
    "ProfileListing",
    "ProfileNotFound",
    "ResolvedProfile",
    "Result",
    "Session",
    "Started",
    "StoredProfile",
    "Tokens",
    "__version__",
    "active_profile",
    "check_profile_name",
    "check_spec",
    "find_binary",
    "find_profile",
    "find_profile_file",
    "list_profiles",
    "load_profile_schema",
    "machine_folder",
    "probe",
    "profile_history",
    "profile_text",
    "profile_version",
    "remove_profile",
    "resolve_among",
    "resolve_profile",
    "resolve_profiles",
    "run",
    "save_profile",
    "shipped_profile_names",
    "stored_profiles",
    "validate_profile",
]
