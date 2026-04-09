"""Wildberries schemas for api."""

from typing import Annotated

from pydantic import BaseModel
from pydantic import Field


class MarketplaceProductData(BaseModel):
    """Base marketplace validation shema"""

    name: Annotated[str, Field(min_length=2, max_length=150)]
    price: Annotated[int, Field(gt=0)]

    model_config = {
        "extra": "forbid",
    }
