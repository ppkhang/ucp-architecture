import httpx

from ucp.adapters.base import PlatformAdapter
from ucp.adapters.mock.client import MockPlatformAdapter
from ucp.common.config import get_settings
from ucp.common.errors import ErrorCode, UCPError

PLATFORMS = {"mock"}


def get_adapter(platform_id: str, http: httpx.Client) -> PlatformAdapter:
    if platform_id not in PLATFORMS:
        raise UCPError(ErrorCode.NOT_FOUND, f"unknown platform {platform_id}")
    return MockPlatformAdapter(http, get_settings().mock_base_url)
