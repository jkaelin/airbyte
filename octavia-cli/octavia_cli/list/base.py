#
# Copyright (c) 2021 Airbyte, Inc., all rights reserved.
#
import abc
from typing import Callable, List

import airbyte_api_client
import octavia_cli.list.formatting as formatting


class BaseListing(abc.ABC):
    COMMON_API_CALL_KWARGS = {"_check_return_type": False}

    @property
    @abc.abstractmethod
    def api(
        self,
    ):  # pragma: no cover
        pass

    @property
    @abc.abstractmethod
    def request_body(self) -> dict:  # pragma: no cover
        pass

    @property
    @abc.abstractmethod
    def fields_to_display(
        self,
    ) -> List[str]:  # pragma: no cover
        pass

    @property
    @abc.abstractmethod
    def list_field_in_response(
        self,
    ) -> str:  # pragma: no cover
        pass

    @abc.abstractmethod
    def get_list_fn(
        self,
    ) -> Callable:  # pragma: no cover
        pass

    def __init__(self, api_client: airbyte_api_client.ApiClient):
        self.api_instance = self.api(api_client)

    def _parse_response(self, api_response) -> List[List[str]]:
        items = [[item[field] for field in self.fields_to_display] for item in api_response[self.list_field_in_response]]
        return items

    def get_listing(self) -> List[List[str]]:
        list_fn = self.get_list_fn()
        api_response = list_fn(self.api_instance, self.request_body, **self.COMMON_API_CALL_KWARGS)
        return self._parse_response(api_response)

    def __repr__(self):
        items = [formatting.format_column_names(self.fields_to_display)] + self.get_listing()
        return formatting.display_as_table(items)