"""Shared API schemas."""

from typing import Any

from pydantic import BaseModel


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


def paginate[T](rows: list[T], limit: int, offset: int) -> Page[T]:
    return Page[T](items=rows[offset : offset + limit], total=len(rows), limit=limit, offset=offset)
