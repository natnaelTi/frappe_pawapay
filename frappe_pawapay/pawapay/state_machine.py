from __future__ import annotations

FINAL_STATES = {"COMPLETED", "FAILED", "EXPIRED", "CANCELLED"}
STATE_RANK = {
	"": 0,
	"CREATED": 1,
	"WAITING_PAYMENT": 2,
	"ACCEPTED": 2,
	"ENQUEUED": 2,
	"PROCESSING": 3,
	"IN_RECONCILIATION": 4,
	"COMPLETED": 10,
	"FAILED": 10,
	"EXPIRED": 10,
	"CANCELLED": 10,
}


def effective_lifecycle_state(current: str | None, incoming: str | None) -> tuple[str, bool]:
	current = current or ""
	incoming = incoming or ""
	if current in FINAL_STATES:
		return current, False
	if incoming in FINAL_STATES:
		return incoming, incoming != current
	if STATE_RANK.get(incoming, 0) >= STATE_RANK.get(current, 0):
		return incoming, incoming != current
	return current, False
