"""Verified site-contact directory. The only source of communication destinations.

The model may *choose* among verified contacts (by id). It can never supply an address.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..domain.errors import RecipientNotVerifiedError
from ..domain.models import SiteContact
from ..repositories.interfaces import Repository


class ContactDirectory:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    def resolve(self, contact_id: str, shipment_id: str) -> SiteContact:
        contact = self._repo.get_contact(contact_id)
        if contact is None:
            raise RecipientNotVerifiedError(f"contact {contact_id} is not in the verified directory")
        if not contact.active:
            raise RecipientNotVerifiedError(f"contact {contact_id} is inactive")
        if shipment_id not in contact.shipment_ids:
            raise RecipientNotVerifiedError(
                f"contact {contact_id} is not associated with shipment {shipment_id}"
            )
        return contact

    def search(self, shipment_id: str, query: str = "") -> Sequence[SiteContact]:
        q = query.strip().lower()
        return [
            c
            for c in self._repo.list_contacts(shipment_id)
            if c.active and (not q or q in c.display_name.lower() or q in c.contact_id.lower())
        ]
