import math

from tqsdk import TqApi, TqAuth, TqBacktest, TqSim

from ak_trading.config import Settings

_SIM_ACCOUNT_BOUNDARY_MESSAGE = (
    "sim_account must be a local TqSim instance; "
    "real, remote, Quick and multi-account objects are not supported"
)


def create_api(
    settings: Settings,
    *,
    sim_balance: float = 1_000_000.0,
    backtest: TqBacktest | None = None,
    sim_account: TqSim | None = None,
) -> TqApi:
    """Create a TqApi bound to the local simulation account surface only.

    ``sim_account``, when given, must be an exact local ``TqSim`` instance.
    The check below is deliberately an exact-type check (not ``isinstance``):
    duck objects, remote OTG accounts (TqKq "Quick" / TqAccount real), any
    sequence of accounts, and ``TqSim`` subclasses are all rejected before
    ``TqAuth``/``TqApi`` are ever constructed. Exact typing also means a
    ``TqSim`` subclass cannot smuggle a wrapped real/remote account through
    this parameter.
    """
    if not math.isfinite(sim_balance) or sim_balance <= 0:
        raise ValueError("sim_balance must be a finite positive number")
    if sim_account is not None and type(sim_account) is not TqSim:
        raise ValueError(_SIM_ACCOUNT_BOUNDARY_MESSAGE)
    account = sim_account if sim_account is not None else TqSim(init_balance=sim_balance)
    api_options = {
        "account": account,
        "auth": TqAuth(settings.tq_user, settings.tq_pass),
    }
    if backtest is not None:
        api_options["backtest"] = backtest
    return TqApi(**api_options)
