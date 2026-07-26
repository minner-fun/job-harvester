from .abetterweb3 import AbetterWeb3Source
from .ats import AtsSource
from .base import Source
from .cryptocurrencyjobs import CryptocurrencyJobsSource
from .cryptojobslist import CryptoJobsListSource
from .dejob import DejobSource
from .himalayas import HimalayasSource
from .jobicy import JobicySource
from .remoteok import RemoteOkSource
from .web3career import Web3CareerSource

SOURCES: dict[str, type[Source]] = {
    AbetterWeb3Source.name: AbetterWeb3Source,
    AtsSource.name: AtsSource,
    CryptocurrencyJobsSource.name: CryptocurrencyJobsSource,
    CryptoJobsListSource.name: CryptoJobsListSource,
    DejobSource.name: DejobSource,
    HimalayasSource.name: HimalayasSource,
    JobicySource.name: JobicySource,
    RemoteOkSource.name: RemoteOkSource,
    Web3CareerSource.name: Web3CareerSource,
}

__all__ = [
    "Source", "SOURCES",
    "AbetterWeb3Source", "AtsSource", "CryptocurrencyJobsSource",
    "CryptoJobsListSource", "DejobSource",
    "HimalayasSource", "JobicySource", "RemoteOkSource", "Web3CareerSource",
]
