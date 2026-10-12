"""Fail-closed remote cost contracts and explicitly local accounting simulation.

Runtime objects are loaded lazily so the shared operation catalog can import
contract schemas while either modelbuilder.models or modelbuilder.store starts.
"""
from importlib import import_module

from .contracts import AccountBinding, BudgetPolicy, Money, Quote, SpendConsent

__all__ = ["AccountBinding", "BudgetPolicy", "Money", "Quote", "SpendConsent", "CostLedger",
           "LocalCostSimulator", "ProviderAdapter", "diagnostics", "RemoteCostService", "ModalProviderAdapter",
           "ModalRuntime", "ModalJournal", "ModalOperatorConfig", "NativeModalPlan"]

_RUNTIME_EXPORTS = {"CostLedger": "ledger", "LocalCostSimulator": "provider", "ProviderAdapter": "provider",
                    "diagnostics": "provider", "RemoteCostService": "service", "ModalProviderAdapter": "modal_provider",
                    "ModalRuntime": "modal_runtime", "ModalJournal": "modal_journal",
                    "ModalOperatorConfig": "modal_contract", "NativeModalPlan": "modal_contract"}


def __getattr__(name):
    if name not in _RUNTIME_EXPORTS:
        raise AttributeError(name)
    return getattr(import_module(f"{__name__}.{_RUNTIME_EXPORTS[name]}"), name)
