"""Wildberries detail response models; unknown upstream fields are ignored.

Models are frozen because they represent the response at parse time. The client
selects products by ID and the minimum price among sizes with a price.
"""

from typing import Annotated

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class WbPrice(BaseModel):
    """The product price is in kopecks, before personal discounts."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    product: Annotated[int, Field(strict=True)]


class WbSize(BaseModel):
    """A size without stock can omit its price."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    price: WbPrice | None = None


class WbProduct(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: Annotated[int, Field(strict=True)]
    name: str
    sizes: list[WbSize]


class WbDetailResponse(BaseModel):
    """An empty products list is valid; its meaning is decided by the client."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    products: list[WbProduct]
