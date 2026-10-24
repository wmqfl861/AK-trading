"""NE1 dispatch/outbox façade over the N3 SQLite journal (C1-frozen signatures).

The transaction and projection owner stays the Ledger; transport stages are
audit records only and never replace OrderState/CancelState business terminals.
"""
from __future__ import annotations

from . import contracts as c
from .ledger import Ledger, LedgerError, SubmitResult


class DispatchJournal:
    """Frozen C1 API: begin_dispatch / record_receipt / pending_commands."""

    def __init__(self, ledger: Ledger, *, evidence_registry=None):
        if type(ledger) is not Ledger:
            raise TypeError("Ledger required")
        self.ledger = ledger
        self.evidence_registry = evidence_registry

    def prepare_native_submission(self, intent: c.OrderIntent, context: c.ExecutionContext, *,
                                  expected_control_revision: int, expected_context: c.ExecutionContext,
                                  resources: tuple = ()):
        """C1.5 AP-01 facade: forwards into the single N3 preparation transaction."""
        return self.ledger.prepare_native_submission(
            intent, context, expected_control_revision=expected_control_revision,
            expected_context=expected_context, resources=resources,
            evidence_registry=self.evidence_registry)

    def record_offer(self, offer, context: c.ExecutionContext, *, first_issued_at, kind,
                     expected_control_revision: int, expected_context: c.ExecutionContext,
                     cancel_id: str | None = None):
        """C1.4 OFD-02/03 facade: one first-record transaction; forwards only."""
        return self.ledger.record_offer(offer, context, first_issued_at=first_issued_at, kind=kind,
                                        expected_control_revision=expected_control_revision,
                                        expected_context=expected_context, cancel_id=cancel_id,
                                        evidence_registry=self.evidence_registry)

    def get_offer(self, offer_id: str):
        return self.ledger.get_offer(offer_id)

    def begin_dispatch(self, offer, context: c.ExecutionContext, *, expected_control_revision: int,
                       expected_context=None):
        """C1.2 seam: forwards expected_context into the single N3 transaction."""
        return self.ledger.begin_dispatch(offer, context,
                                          expected_control_revision=expected_control_revision,
                                          evidence_registry=self.evidence_registry,
                                          expected_context=expected_context)

    def record_receipt(self, receipt) -> SubmitResult:
        return self.ledger.record_dispatch_receipt(receipt)

    def pending_commands(self, binding: c.Binding) -> tuple:
        return self.ledger.pending_dispatch_commands(binding)
