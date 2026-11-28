"""Optional prototypes (task.optional_*): not part of the release.

pyproject.toml packages only verified_execution and verified_execution_contracts, so nothing
here is installed by the release, and the base lock (requirements-py313.lock) has none of the
packages these modules import. They run only with requirements-optional-py313.lock, in the
optional-prototypes CI job. See docs/OPTIONAL_PROTOTYPES.md.
"""
