"""平台抓取器。"""
from .base import BaseFetcher, build_fetcher, describe_platform
from .bilibili import BilibiliFetcher
from .x import XFetcher

__all__ = ["BaseFetcher", "BilibiliFetcher", "XFetcher", "build_fetcher", "describe_platform"]
