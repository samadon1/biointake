"""Getting the request to the person who can answer it.

Until this existed, "sent" meant an audit line and a row in a demo outbox. Nothing left the
building: a coordinator had to open the outbox and hand the site its link by some other means. An
audit trail that records a message as sent when it was never sent is worse than one that records
nothing, so delivery is now attempted for real and its outcome is what gets written down.

Both implementations are honest about which they are. The recorded one says the message was filed
rather than sent; the SES one says where it went and carries the provider's message id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Delivery:
    """What happened when we tried. `delivered` false means the recipient does not have it."""

    delivered: bool
    channel: str
    detail: str
    message_id: str = ""

    def audit_metadata(self) -> dict[str, Any]:
        return {
            "delivery_channel": self.channel,
            "delivered": self.delivered,
            "delivery_detail": self.detail,
            "provider_message_id": self.message_id,
        }


class MessageDelivery(Protocol):
    def send(self, *, to: str, subject: str, body: str) -> Delivery: ...


class RecordedDelivery:
    """Files the message without sending it. The default, and what the demo runs on.

    A lab evaluating BioIntake should not have it emailing real sites, and the console's outbox is
    a faithful view of what would have gone out. It reports `delivered=False` so nothing downstream
    can mistake a filed message for a sent one.
    """

    channel = "recorded"

    def send(self, *, to: str, subject: str, body: str) -> Delivery:
        return Delivery(
            delivered=False,
            channel=self.channel,
            detail=f"recorded for {to}; this deployment does not send mail",
        )


class SesDelivery:
    """Amazon SES. Configured with the address the lab sends from and verified in the SES console."""

    channel = "ses"

    def __init__(self, sender: str, session: Any, configuration_set: str = "") -> None:
        self._sender = sender
        self._client = session.client("ses")
        self._configuration_set = configuration_set

    def send(self, *, to: str, subject: str, body: str) -> Delivery:
        kwargs: dict[str, Any] = {
            "Source": self._sender,
            "Destination": {"ToAddresses": [to]},
            "Message": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            },
        }
        if self._configuration_set:
            kwargs["ConfigurationSetName"] = self._configuration_set
        try:
            response = self._client.send_email(**kwargs)
        except Exception as e:  # noqa: BLE001; the failure is data, not a crash: the case must survive it
            return Delivery(delivered=False, channel=self.channel, detail=f"SES refused it: {e}")
        return Delivery(
            delivered=True,
            channel=self.channel,
            detail=f"sent to {to}",
            message_id=str(response.get("MessageId", "")),
        )
