# Exit Codes - Release Orchestrator

This document describes every numeric exit code produced by the
`release-orchestrator` CLI tool. All codes are defined in
[release_orchestrator/utils/exit_codes.py](file:///d:/workSpace/AI__SPACE/zyx-00098/release_orchestrator/utils/exit_codes.py).

| Code | Name | Description |
|-----:|------|-------------|
| **0** | `EXIT_OK` | Command executed successfully without any errors. |
| **2** | `EXIT_CIRCULAR_DEPENDENCY` | Circular dependency detected among release components. |
| **3** | `EXIT_VERSION_DOWNGRADE` | Version downgrade detected - target version is lower than currently deployed. |
| **4** | `EXIT_CHECKSUM_MISMATCH` | Package checksum does not match the declared checksum. |
| **5** | `EXIT_APPROVAL_MISSING` | Production environment release is missing required approval records. |
| **10** | `EXIT_CONFIG_ERROR` | Configuration or manifest file is invalid or cannot be parsed. |
| **11** | `EXIT_FILE_NOT_FOUND` | Required file (manifest, config, archive, etc.) not found. |
| **12** | `EXIT_VALIDATION_FAILED` | General validation failure that does not fall into more specific categories. |
| **13** | `EXIT_PLAN_ERROR` | Failed to generate a valid release or rollback plan. |
| **14** | `EXIT_EXPORT_ERROR` | Failed to export or package the release archive. |
| **15** | `EXIT_HISTORY_ERROR` | Failed to read or query execution history. |
| **16** | `EXIT_DRYRUN_FAILED` | Dry-run simulation detected issues during simulated execution. |
| **20** | `EXIT_UNKNOWN_COMMAND` | Unknown or invalid command specified. |
| **99** | `EXIT_INTERNAL_ERROR` | Unexpected internal error occurred during execution. |

## Priority / Precedence

When multiple error conditions occur simultaneously the validator
returns the highest-priority code in this order:

1. `EXIT_CIRCULAR_DEPENDENCY` (2)
2. `EXIT_VERSION_DOWNGRADE` (3)
3. `EXIT_CHECKSUM_MISMATCH` (4)
4. `EXIT_APPROVAL_MISSING` (5)
5. `EXIT_VALIDATION_FAILED` (12)

## Usage

Print the latest table from the command line:

```bash
python -m release_orchestrator exit-codes
python -m release_orchestrator exit-codes --json
python -m release_orchestrator exit-codes -o docs/exit_codes.json
```

## Shell Integration

```bash
release-orchestrator validate -m manifest.json
ec=$?
case $ec in
  0) echo "OK" ;;
  2) echo "Fix circular deps first" ;;
  3) echo "Version downgrade blocked" ;;
  4) echo "Checksums do not match - reject packages" ;;
  5) echo "Production release requires approval" ;;
  *) echo "Generic failure: exit=$ec" ;;
esac
```
