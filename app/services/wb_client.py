"""Get product name and price from Wb Api."""

import httpx
from loguru import logger

from app.schemas.marketplace import MarketplaceProductData
from app.services.base_client import BaseMarketplaceClient


class WbClient(BaseMarketplaceClient):
    URL = "https://card.wb.ru/cards/v4/detail"
    TIMEOUT_SECONDS = 10.0

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client

    async def get_product_data(self, article: int) -> MarketplaceProductData | None:
        """Get product data every 10 seconds."""

        params: dict[str, str | int] = {
            "appType": 1,
            "curr": "rub",
            "dest": -1257786,
            "nm": str(article),
        }

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
            "Accept-Language": "ru-RU,ru;q=0.9",
        }

        try:
            r = await self._http.get(
                self.URL,
                params=params,
                headers=headers,
                timeout=self.TIMEOUT_SECONDS,
            )

            r.raise_for_status()

            products = r.json().get("products", [])
            if not products:
                logger.info("Product not found!")
                return None

            p = products[0]

            sizes = p.get("sizes", [])
            if not sizes:
                return None

            price_info = sizes[0].get("price", {})
            price = price_info.get("product")

            return MarketplaceProductData(
                name=p.get("name"),
                price=int(price),
            )

        except httpx.HTTPStatusError as e:
            logger.warning(
                f"Error HTTP {e.response.status_code} for article: {article}"
            )
        except httpx.HTTPError:
            logger.error(f"Couldn't get data for article {article}")

        return None
