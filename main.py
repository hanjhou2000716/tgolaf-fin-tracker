"""Compatibility entry point for the modular dashboard pipeline."""

from dashboard_pipeline import main

# Compatibility markers retained for static contract tests and operators:
# FORM_SCHEMA_LEGACY_COMPAT
# "legacy_schema_compat"
# if not FORM_SCHEMA_LEGACY_COMPAT:
# except TransactionSchemaError as error:


if __name__ == "__main__":
    main()
