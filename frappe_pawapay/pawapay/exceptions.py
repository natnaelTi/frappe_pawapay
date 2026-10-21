class PawaPayError(Exception):
	pass


class PawaPayTransportError(PawaPayError):
	pass


class PawaPayAuthenticationError(PawaPayError):
	pass


class PawaPayValidationError(PawaPayError):
	pass


class PawaPayUncertainError(PawaPayError):
	pass


class PawaPayContractError(PawaPayError):
	pass
