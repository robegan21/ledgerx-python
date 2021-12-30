from ledgerx.http_client import HttpClient
from typing import List, Dict
from ledgerx.util import gen_url
from ledgerx import DEFAULT_LIMIT
from ledgerx.generic_resource import GenericResource

import logging

logger = logging.getLogger(__name__)

class Transactions:
    default_list_params = dict(limit=DEFAULT_LIMIT)

    @classmethod
    def list(cls, params: Dict = {}) -> List[Dict]:
        """Returns a list of all debits and credits to your accounts.

        https://docs.ledgerx.com/reference#gettransactions

        Args:
            params (Dict, optional): [description]. Defaults to {}.
                asset:str filter by asset
                limit:int maximum number to return
                offset:int initial index from which to return

        Returns:
            List[Dict]: [description]
        """
        include_api_key = True
        url = gen_url("/funds/transactions")
        qps = {**cls.default_list_params, **params}
        logger.info(f"Getting transactions {url} {qps}")
        res = HttpClient.get(url, qps, include_api_key)
        return res.json()

    ### helper methods specific to this API client

    @classmethod
    def list_all(cls, params: Dict = {}) -> List[Dict]:
        include_api_key = True
        url = gen_url("/funds/transactions")
        qps = {**cls.default_list_params, **params}
        logger.info(f"Getting all transactions {url} {qps}")
        return GenericResource.list_all(url, qps, include_api_key)

    @classmethod
    async def async_list_all(cls, params: Dict = {}) -> List[Dict]:
        include_api_key = True
        url = gen_url("/funds/transactions")
        qps = {**cls.default_list_params, **params}
        logger.info(f"Getting all transactions {url} {qps}")
        return await GenericResource.async_list_all(url, qps, include_api_key)
    