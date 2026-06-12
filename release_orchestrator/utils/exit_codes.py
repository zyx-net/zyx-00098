"""Exit code definitions for the release orchestrator.

All exit codes used by the tool are defined here for consistency and
documentation purposes. Each exit code has a unique number and a
descriptive meaning.
"""
from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class ExitCode:
    code: int
    name: str
    description: str


EXIT_OK = ExitCode(0, "EXIT_OK", "Command executed successfully without any errors.")
EXIT_CIRCULAR_DEPENDENCY = ExitCode(
    2, "EXIT_CIRCULAR_DEPENDENCY",
    "Circular dependency detected among release components."
)
EXIT_VERSION_DOWNGRADE = ExitCode(
    3, "EXIT_VERSION_DOWNGRADE",
    "Version downgrade detected - target version is lower than currently deployed."
)
EXIT_CHECKSUM_MISMATCH = ExitCode(
    4, "EXIT_CHECKSUM_MISMATCH",
    "Package checksum does not match the declared checksum."
)
EXIT_APPROVAL_MISSING = ExitCode(
    5, "EXIT_APPROVAL_MISSING",
    "Production environment release is missing required approval records."
)
EXIT_CONFIG_ERROR = ExitCode(
    10, "EXIT_CONFIG_ERROR",
    "Configuration or manifest file is invalid or cannot be parsed."
)
EXIT_FILE_NOT_FOUND = ExitCode(
    11, "EXIT_FILE_NOT_FOUND",
    "Required file (manifest, config, archive, etc.) not found."
)
EXIT_VALIDATION_FAILED = ExitCode(
    12, "EXIT_VALIDATION_FAILED",
    "General validation failure that does not fall into more specific categories."
)
EXIT_PLAN_ERROR = ExitCode(
    13, "EXIT_PLAN_ERROR",
    "Failed to generate a valid release or rollback plan."
)
EXIT_EXPORT_ERROR = ExitCode(
    14, "EXIT_EXPORT_ERROR",
    "Failed to export or package the release archive."
)
EXIT_HISTORY_ERROR = ExitCode(
    15, "EXIT_HISTORY_ERROR",
    "Failed to read or query execution history."
)
EXIT_DRYRUN_FAILED = ExitCode(
    16, "EXIT_DRYRUN_FAILED",
    "Dry-run simulation detected issues during simulated execution."
)
EXIT_SCHEDULE_ERROR = ExitCode(
    17, "EXIT_SCHEDULE_ERROR",
    "Failed to schedule components into release windows."
)
EXIT_WINDOW_LOCKED = ExitCode(
    18, "EXIT_WINDOW_LOCKED",
    "Cannot schedule into a locked release window."
)
EXIT_WINDOW_FROZEN = ExitCode(
    19, "EXIT_WINDOW_FROZEN",
    "Target date falls within a freeze period."
)
EXIT_UNKNOWN_COMMAND = ExitCode(
    20, "EXIT_UNKNOWN_COMMAND",
    "Unknown or invalid command specified."
)
EXIT_SCHEME_ALREADY_EXISTS = ExitCode(
    21, "EXIT_SCHEME_ALREADY_EXISTS",
    "A scheme with the same name already exists. Use --force to overwrite."
)
EXIT_SCHEME_NOT_FOUND = ExitCode(
    22, "EXIT_SCHEME_NOT_FOUND",
    "The specified scheme name does not exist."
)
EXIT_SCHEME_VALIDATION_FAILED = ExitCode(
    23, "EXIT_SCHEME_VALIDATION_FAILED",
    "Scheme validation failed: missing fields, bad JSON, window conflicts, or locked window reuse."
)
EXIT_SCHEME_IO_ERROR = ExitCode(
    24, "EXIT_SCHEME_IO_ERROR",
    "Failed to read or write scheme files to disk."
)
EXIT_LOCK_ALREADY_EXISTS = ExitCode(
    30, "EXIT_LOCK_ALREADY_EXISTS",
    "A lock with the same scope already exists. Use --force to overwrite."
)
EXIT_LOCK_NOT_FOUND = ExitCode(
    31, "EXIT_LOCK_NOT_FOUND",
    "The specified lock does not exist."
)
EXIT_LOCK_VALIDATION_FAILED = ExitCode(
    32, "EXIT_LOCK_VALIDATION_FAILED",
    "Lock validation failed: invalid environment, overlapping times, expired lock, or bad scope."
)
EXIT_LOCK_IO_ERROR = ExitCode(
    33, "EXIT_LOCK_IO_ERROR",
    "Failed to read or write lock files to disk."
)
EXIT_LOCK_PERMISSION_DENIED = ExitCode(
    34, "EXIT_LOCK_PERMISSION_DENIED",
    "Insufficient permissions to remove or overwrite the lock."
)
EXIT_LOCK_BLOCKED_OPERATION = ExitCode(
    35, "EXIT_LOCK_BLOCKED_OPERATION",
    "Operation blocked: the target environment, service, or time window is covered by an active lock."
)
EXIT_INTERNAL_ERROR = ExitCode(
    99, "EXIT_INTERNAL_ERROR",
    "Unexpected internal error occurred during execution."
)


ALL_EXIT_CODES = [
    EXIT_OK,
    EXIT_CIRCULAR_DEPENDENCY,
    EXIT_VERSION_DOWNGRADE,
    EXIT_CHECKSUM_MISMATCH,
    EXIT_APPROVAL_MISSING,
    EXIT_CONFIG_ERROR,
    EXIT_FILE_NOT_FOUND,
    EXIT_VALIDATION_FAILED,
    EXIT_PLAN_ERROR,
    EXIT_EXPORT_ERROR,
    EXIT_HISTORY_ERROR,
    EXIT_DRYRUN_FAILED,
    EXIT_SCHEDULE_ERROR,
    EXIT_WINDOW_LOCKED,
    EXIT_WINDOW_FROZEN,
    EXIT_UNKNOWN_COMMAND,
    EXIT_SCHEME_ALREADY_EXISTS,
    EXIT_SCHEME_NOT_FOUND,
    EXIT_SCHEME_VALIDATION_FAILED,
    EXIT_SCHEME_IO_ERROR,
    EXIT_LOCK_ALREADY_EXISTS,
    EXIT_LOCK_NOT_FOUND,
    EXIT_LOCK_VALIDATION_FAILED,
    EXIT_LOCK_IO_ERROR,
    EXIT_LOCK_PERMISSION_DENIED,
    EXIT_LOCK_BLOCKED_OPERATION,
    EXIT_INTERNAL_ERROR,
]


def get_exit_code_by_code(code: int) -> ExitCode:
    """Return the ExitCode object for a given numeric code.

    Args:
        code: The numeric exit code.

    Returns:
        The matching ExitCode, or EXIT_INTERNAL_ERROR if not found.
    """
    for ec in ALL_EXIT_CODES:
        if ec.code == code:
            return ec
    return EXIT_INTERNAL_ERROR


def exit_codes_as_dict() -> Dict[int, Dict[str, str]]:
    """Return all exit codes as a dictionary suitable for serialization.

    Returns:
        Dict mapping numeric code -> {name, description}.
    """
    return {
        ec.code: {
            "name": ec.name,
            "description": ec.description,
        }
        for ec in ALL_EXIT_CODES
    }
