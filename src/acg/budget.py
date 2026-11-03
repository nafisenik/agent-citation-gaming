"""A hard ceiling on live model requests, shared across a whole study.

The cap is reserved *before* a request goes out, not counted after it returns, so an
interrupted or failed request still consumes its slot. A budget that only counted successes
would quietly spend more than it was given.
"""

from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    """Raised before a request that would exceed the configured cap."""


@dataclass
class Meter:
    max_requests: int
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    ledger: list[dict] = field(default_factory=list)

    def reserve(self, label: str) -> None:
        if self.requests >= self.max_requests:
            raise BudgetExceeded(
                f"request cap {self.max_requests} reached before '{label}'"
            )
        self.requests += 1

    def record(
        self,
        label: str,
        reported_model: str,
        input_tokens: int,
        output_tokens: int,
        seconds: float,
    ) -> None:
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0
        self.ledger.append(
            {
                "label": label,
                "reported_model": reported_model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "seconds": round(seconds, 2),
            }
        )

    def summary(self) -> dict:
        return {
            "requests_used": self.requests,
            "max_requests": self.max_requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }
