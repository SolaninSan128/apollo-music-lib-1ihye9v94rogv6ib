"""Unified single-writer guard for Harmonia library and publish boundaries.

This package is a deployable artifact. Importing it does not open production
lock files, does not publish, and does not change MODE.
"""

CONTRACT_VERSION = 1

__all__ = ["CONTRACT_VERSION"]
