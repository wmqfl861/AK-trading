import importlib


def test_package_imports():
    mod = importlib.import_module("ak_trading")
    assert mod.__version__
