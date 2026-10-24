from __future__ import annotations

from typing import Literal


TqSdkOperation = Literal["session", "history_download", "market_data"]


class TqSdkOperationError(RuntimeError):
    """An expected user, entitlement, or authentication failure from TqSdk."""


def expected_tqsdk_error(
    exc: BaseException, *, operation: TqSdkOperation
) -> TqSdkOperationError | None:
    """Type known plain-Exception failures without hiding programming defects."""
    if type(exc) is not Exception:
        return None
    message = str(exc)
    if message.startswith("用户权限认证失败"):
        return TqSdkOperationError(message)
    if operation == "history_download" and message.startswith(
        "您的账户不支持下载历史数据功能"
    ):
        return TqSdkOperationError(message)
    if (
        operation == "market_data"
        and message.startswith("您的账户不支持查看")
        and "行情数据" in message
        and "需要购买后才能使用" in message
    ):
        return TqSdkOperationError(message)
    return None
