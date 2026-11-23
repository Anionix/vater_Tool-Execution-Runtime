"""Python-first verification target. No network and no user-controlled authority."""
from .core import CalculationService, OperationError, LocalAuthority

__all__ = ["CalculationService", "OperationError", "LocalAuthority"]
