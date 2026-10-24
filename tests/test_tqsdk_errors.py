from ak_trading.tqsdk_errors import TqSdkOperationError, expected_tqsdk_error


def test_tqsdk_plain_exception_adapter_only_accepts_known_messages():
    permission = expected_tqsdk_error(
        Exception("您的账户不支持下载历史数据功能，需要购买后才能使用。"),
        operation="history_download",
    )

    assert isinstance(permission, TqSdkOperationError)
    assert (
        expected_tqsdk_error(
            Exception("unrecognized third-party defect"),
            operation="history_download",
        )
        is None
    )
    assert (
        expected_tqsdk_error(
            AttributeError("programming defect"),
            operation="history_download",
        )
        is None
    )
