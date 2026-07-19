"""Shared polyfactory factories for domain dataclasses used across tests."""

from __future__ import annotations

from polyfactory.factories import DataclassFactory

from pre_commit_hooks.ast_checks._base import Violation


class ViolationFactory(DataclassFactory[Violation]):
    __model__ = Violation
