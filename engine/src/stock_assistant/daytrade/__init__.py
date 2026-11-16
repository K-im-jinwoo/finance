"""Isolated observation and synthetic-replay tools; never a brokerage client."""

MODE = "OBSERVE_ONLY"
POLICY_VERSION = "daytrade-stage1-offline-2026-11-13.1"

from .scanner import ScannerConfig, ScanResult, scan_rankings
from .strategy import StrategyConfig, StrategyEngine, StrategySignal
from .bot import BotConfig, run_paper_session
from .report import build_trading_report, render_markdown_report
from .stream import (STAGE4_COST_PROFILE_STATUS, LiveSessionBridge, provisional_costs,
                     provisional_quality, provisional_scanner_config, provisional_strategy_config,
                     stream_to_dataset, run_observed_stream)

__all__ = ["MODE", "POLICY_VERSION", "ScannerConfig", "ScanResult", "scan_rankings",
           "StrategyConfig", "StrategyEngine", "StrategySignal", "BotConfig", "run_paper_session",
           "build_trading_report", "render_markdown_report", "STAGE4_COST_PROFILE_STATUS",
           "LiveSessionBridge", "provisional_costs", "provisional_quality", "provisional_scanner_config",
           "provisional_strategy_config", "stream_to_dataset", "run_observed_stream"]
