"""Injectable bearer verification. These fixed tokens are for local preview only."""

from dataclasses import dataclass
from typing import Protocol

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class Customer:
    customer_id: str


class BearerVerifier(Protocol):
    def verify(self, token: str) -> Customer | None: ...


class FakeBearerVerifier:
    def verify(self, token: str) -> Customer | None:
        customers = {"fake-alice": "alice", "fake-bob": "bob"}
        customer_id = customers.get(token)
        return Customer(customer_id) if customer_id else None


def bearer_token(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token or " " in token:
        raise HTTPException(401, "authentication_required", headers={"WWW-Authenticate": "Bearer"})
    return token
