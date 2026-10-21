from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


SANDBOX_BASE_URL = "https://api.sandbox.pawapay.io"
PRODUCTION_BASE_URL = "https://api.pawapay.io"

CHECKOUT_REDIRECT_HOSTS = {
	"checkout.pawapay.io",
	"checkout.sandbox.pawapay.io",
	"pay.pawapay.io",
	"pay.sandbox.pawapay.io",
}

FINAL_LIFECYCLE_STATES = {"COMPLETED", "FAILED", "EXPIRED", "CANCELLED"}
NON_FINAL_LIFECYCLE_STATES = {"ACCEPTED", "PROCESSING", "ENQUEUED", "IN_RECONCILIATION", "WAITING_PAYMENT"}


@dataclass(frozen=True)
class Money:
	amount: Decimal
	currency: str

	def as_api_string(self) -> str:
		normalized = self.amount.normalize()
		if normalized == normalized.to_integral():
			return str(normalized.quantize(Decimal("1")))
		return format(self.amount, "f")


def get_base_url(environment: str) -> str:
	return PRODUCTION_BASE_URL if environment == "Production" else SANDBOX_BASE_URL
