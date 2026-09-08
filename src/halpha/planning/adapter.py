"""The shared Halpha Strategy adapter and lifecycle wrapper."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import QueryOrder
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.enums import OrderSide, TimeInForce, TriggerType
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, PositionId
from nautilus_trader.model.objects import Price, Quantity

from halpha.planning.strategies.one_shot import (
    ActivationStrategyState,
    EntryEvaluationInput,
    OneShotDonchianAtrLogic,
    StrategyProposal,
)
_LIMIT_TIME_IN_FORCE = {
    "GTC": TimeInForce.GTC,
    "GTD": TimeInForce.GTD,
    "IOC": TimeInForce.IOC,
    "FOK": TimeInForce.FOK,
}
_BINANCE_PRICE_MATCH_VALUES = frozenset(
    {
        "OPPONENT",
        "OPPONENT_5",
        "OPPONENT_10",
        "OPPONENT_20",
        "QUEUE",
        "QUEUE_5",
        "QUEUE_10",
        "QUEUE_20",
    }
)
_CONDITIONAL_TRIGGER_TYPES = {
    "LAST_PRICE": TriggerType.LAST_PRICE,
    "MARK_PRICE": TriggerType.MARK_PRICE,
}


class ProposalSink(Protocol):
    def __call__(self, proposal: StrategyProposal) -> None: ...


class ExecutionEventSink(Protocol):
    def __call__(self, event: object) -> None: ...


class BarEventSink(Protocol):
    def __call__(self, bar: object) -> None: ...


class BarFailureSink(Protocol):
    def __call__(self, bar: object, exception: Exception) -> None: ...


class HistoryWarmupFailureSink(Protocol):
    def __call__(
        self, stage: str, item: object | None, exception: Exception
    ) -> None: ...


class QuoteEventSink(Protocol):
    def __call__(self, tick: object) -> None: ...


class MarkPriceEventSink(Protocol):
    def __call__(self, mark_price: object) -> None: ...


class HistoryCacheProvider(Protocol):
    def __call__(self, bar_type: BarType) -> Iterable[Bar]: ...


class BarEvaluatorPort(Protocol):
    """Minimal bar warmup/evaluation surface shared by strategies and facts."""

    @property
    def subscribed_bar_types(self) -> tuple[BarType, ...]: ...

    @property
    def warmup_complete(self) -> bool: ...

    @property
    def target_bar_type(self) -> BarType: ...

    @property
    def target_history_count(self) -> int: ...

    def try_warm_from_cached_bars(self, *, target_bars: Iterable[Bar]) -> bool: ...

    def accept_historical(self, bar: Bar) -> None: ...

    def complete_live_warmup(self) -> None: ...

    def accept(self, bar: Bar) -> EntryEvaluationInput | None: ...


class ControllerPort(Protocol):
    def create_strategy(self, strategy: Strategy, start: bool = True) -> None: ...

    def stop_strategy(self, strategy: Strategy) -> None: ...

    def remove_strategy(self, strategy: Strategy) -> None: ...


def strategy_id_for_activation(activation_id: str) -> str:
    stable = uuid5(NAMESPACE_URL, f"urn:halpha:activation:{activation_id}")
    return f"HALPHA-{stable.hex.upper()}"


def order_id_tag_for_activation(activation_id: str) -> str:
    """Return the stable per-activation tag required by one Nautilus trader."""

    return uuid5(
        NAMESPACE_URL,
        f"urn:halpha:activation-order-tag:{activation_id}",
    ).hex


def strategy_config_for_activation(activation_id: str) -> StrategyConfig:
    return StrategyConfig(
        strategy_id=strategy_id_for_activation(activation_id),
        order_id_tag=order_id_tag_for_activation(activation_id),
        external_order_claims=None,
        manage_contingent_orders=False,
        manage_gtd_expiry=False,
        manage_stop=False,
    )


class HalphaStrategyAdapter(Strategy):
    """Final-style proposal adapter shared by live and BacktestEngine paths.

    The planning surface exposes no venue operation from this class. Execution may add only the private
    persisted-action gate allowed by ALP/EXE after its own qualification.
    """

    def __init__(
        self,
        *,
        activation_id: str,
        logic: OneShotDonchianAtrLogic | None = None,
        state_provider: Callable[[], ActivationStrategyState] | None = None,
        proposal_sink: ProposalSink | None = None,
        instrument_ref: str | None = None,
        market_data_required: bool = True,
        persisted_action_capability: object | None = None,
        execution_event_sink: ExecutionEventSink | None = None,
        bar_evaluator: BarEvaluatorPort | None = None,
        bar_event_sink: BarEventSink | None = None,
        bar_failure_sink: BarFailureSink | None = None,
        history_warmup_failure_sink: HistoryWarmupFailureSink | None = None,
        quote_event_sink: QuoteEventSink | None = None,
        mark_price_event_sink: MarkPriceEventSink | None = None,
        live_history_warmup: bool = False,
        current_time_provider: Callable[[], datetime] | None = None,
        history_cache_provider: HistoryCacheProvider | None = None,
    ) -> None:
        super().__init__(config=strategy_config_for_activation(activation_id))
        strategy_parts = (logic, state_provider, proposal_sink)
        if any(item is None for item in strategy_parts) and not all(
            item is None for item in strategy_parts
        ):
            raise ValueError("STRATEGY_ADAPTER_CONFIGURATION_INCOMPLETE")
        self._activation_id = activation_id
        self._logic = logic
        self._state_provider = state_provider
        self._proposal_sink = proposal_sink
        self._instrument_id = (
            InstrumentId.from_str(f"{instrument_ref}.BINANCE")
            if instrument_ref is not None
            else None
        )
        self._market_data_required = market_data_required
        self._persisted_action_capability = persisted_action_capability
        self._execution_event_sink = execution_event_sink
        self._bar_evaluator = bar_evaluator
        self._bar_event_sink = bar_event_sink
        self._bar_failure_sink = bar_failure_sink
        self._history_warmup_failure_sink = history_warmup_failure_sink
        self._quote_event_sink = quote_event_sink
        self._mark_price_event_sink = mark_price_event_sink
        self._live_history_warmup = live_history_warmup
        self._current_time_provider = current_time_provider
        self._history_cache_provider = history_cache_provider
        self._history_request_id: str | None = None
        self._live_bar_subscriptions_started = False
        self._quote_ticks_subscribed = False
        self._stopping = False
        self._persisted_orders: dict[str, Any] = {}

    @property
    def activation_id(self) -> str:
        return self._activation_id

    @property
    def live_history_ready(self) -> bool:
        """Return whether the configured live history handoff is complete."""

        if not self._live_history_warmup:
            return True
        return bool(
            self._bar_evaluator is not None
            and self._bar_evaluator.warmup_complete
            and self._live_bar_subscriptions_started
        )

    @property
    def market_data_instrument_id(self) -> InstrumentId | None:
        """Return the instrument only when this responsibility consumes its feeds."""

        if (
            self._instrument_id is None
            or not self._market_data_required
        ):
            return None
        return self._instrument_id

    def evaluate_normalized_entry(self, evaluation: EntryEvaluationInput) -> None:
        if (
            self._logic is None
            or self._state_provider is None
            or self._proposal_sink is None
        ):
            raise RuntimeError("STRATEGY_EVALUATION_NOT_CONFIGURED")
        result = self._logic.evaluate_entry(evaluation, self._state_provider())
        if result.proposal is not None:
            self._proposal_sink(result.proposal)

    def on_start(self) -> None:
        if self._bar_evaluator is not None and not self._live_history_warmup:
            for bar_type in self._bar_evaluator.subscribed_bar_types:
                self.subscribe_bars(bar_type)
            self._live_bar_subscriptions_started = True
        if self._instrument_id is not None and self._market_data_required:
            self.subscribe_mark_prices(self._instrument_id)
            self.subscribe_quote_ticks(self._instrument_id)
            self._quote_ticks_subscribed = True
        if self._bar_evaluator is not None and self._live_history_warmup:
            try:
                if self._try_live_history_cache_warmup():
                    self._start_live_bar_subscriptions()
                else:
                    self._begin_live_history_warmup()
            except Exception as exc:
                self._report_history_warmup_failure("REQUEST", None, exc)
                raise

    def _try_live_history_cache_warmup(self) -> bool:
        if self._bar_evaluator is None:
            raise RuntimeError("LIVE_WARMUP_STATE_INVALID")
        provider = self._history_cache_provider
        if provider is None:
            provider = self.cache.bars
        return self._bar_evaluator.try_warm_from_cached_bars(
            target_bars=provider(self._bar_evaluator.target_bar_type),
        )

    def _start_live_bar_subscriptions(self) -> None:
        if self._bar_evaluator is None or self._live_bar_subscriptions_started:
            raise RuntimeError("LIVE_WARMUP_STATE_INVALID")
        for bar_type in self._bar_evaluator.subscribed_bar_types:
            self.subscribe_bars(bar_type)
        self._live_bar_subscriptions_started = True

    def _begin_live_history_warmup(self) -> None:
        if self._bar_evaluator is None or self._history_request_id is not None:
            raise RuntimeError("LIVE_WARMUP_STATE_INVALID")
        current_time = (
            self._current_time_provider()
            if self._current_time_provider is not None
            else self.clock.utc_now()
        )
        if current_time.tzinfo is None:
            raise RuntimeError("LIVE_WARMUP_TIMEZONE_REQUIRED")
        minute_floor = current_time.astimezone(UTC).replace(second=0, microsecond=0)
        warmup_end = minute_floor - timedelta(milliseconds=1)
        fifteen_minute_floor = minute_floor.replace(
            minute=(minute_floor.minute // 15) * 15,
            second=0,
            microsecond=0,
        )
        target_count = self._bar_evaluator.target_history_count
        warmup_start = fifteen_minute_floor - timedelta(
            minutes=15 * (target_count + 1)
        )
        request_id = self.request_bars(
            self._bar_evaluator.target_bar_type,
            start=warmup_start,
            end=warmup_end,
            limit=target_count + 2,
            callback=self._on_live_history_complete,
            update_catalog=False,
        )
        self._history_request_id = str(request_id)

    def on_historical_data(self, data: object) -> None:
        if (
            self._live_history_warmup
            and self._bar_evaluator is not None
            and isinstance(data, Bar)
        ):
            try:
                self._bar_evaluator.accept_historical(data)
            except Exception as exc:
                self._report_history_warmup_failure("DATA", data, exc)
                raise

    def _on_live_history_complete(self, request_id: object) -> None:
        if self._stopping:
            return
        try:
            if self._history_request_id != str(request_id):
                raise RuntimeError("LIVE_WARMUP_REQUEST_ID_MISMATCH")
            if self._bar_evaluator is None:
                raise RuntimeError("LIVE_WARMUP_STATE_INVALID")
            self._bar_evaluator.complete_live_warmup()
            self._start_live_bar_subscriptions()
        except Exception as exc:
            self._report_history_warmup_failure("COMPLETE", request_id, exc)
            raise

    def _report_history_warmup_failure(
        self, stage: str, item: object | None, exception: Exception
    ) -> None:
        if self._history_warmup_failure_sink is not None:
            self._history_warmup_failure_sink(stage, item, exception)

    def on_stop(self) -> None:
        self._stopping = True
        if self._bar_evaluator is not None and self._live_bar_subscriptions_started:
            for bar_type in reversed(self._bar_evaluator.subscribed_bar_types):
                self.unsubscribe_bars(bar_type)
        if self._instrument_id is not None and self._quote_ticks_subscribed:
            self.unsubscribe_quote_ticks(self._instrument_id)
            self._quote_ticks_subscribed = False
        # The fixed Binance data client does not implement mark-price
        # unsubscription. The single node disconnect owns that stream teardown.
        self._persisted_orders.clear()

    def on_bar(self, bar: object) -> None:
        try:
            if self._bar_event_sink is not None:
                self._bar_event_sink(bar)
            if self._bar_evaluator is None:
                return
            # Once this one-shot activation owns an entry responsibility, no
            # later bar can create another entry. Avoid building an evaluation
            # (and therefore avoid the live pre-submit fact read) after that
            # permanent product decision. The state is checked again below for
            # the still-open path so a concurrent stop remains fail-closed.
            strategy_configured = self._logic is not None
            if strategy_configured:
                if self._state_provider is None:
                    raise RuntimeError("STRATEGY_STATE_PROVIDER_NOT_CONFIGURED")
                if self._state_provider().entry_opportunity_consumed:
                    return
            evaluation = self._bar_evaluator.accept(bar)
            if evaluation is not None:
                if not strategy_configured:
                    raise RuntimeError("STRATEGY_EVALUATION_NOT_CONFIGURED")
                self.evaluate_normalized_entry(evaluation)
        except Exception as exc:
            if self._bar_failure_sink is not None:
                self._bar_failure_sink(bar, exc)
            raise

    def on_quote_tick(self, tick: object) -> None:
        if self._quote_event_sink is not None:
            self._quote_event_sink(tick)

    def on_mark_price(self, mark_price: object) -> None:
        if self._mark_price_event_sink is not None:
            self._mark_price_event_sink(mark_price)

    def _submit_persisted_order(
        self,
        capability: object,
        *,
        profile: str,
        instrument_ref: str,
        direction: str,
        quantity: str,
        price: str | None,
        trigger_price: str | None,
        reduce_only: bool,
        client_order_id: str,
        time_in_force: str | None = None,
        post_only: bool = False,
        price_match: str | None = None,
        expire_at: datetime | None = None,
        trigger_source: str | None = None,
        position_side: str | None = None,
    ) -> object:
        """EXE-only final write hop after a committed SUBMITTING action."""

        self._require_persisted_action_capability(capability)
        if client_order_id in self._persisted_orders:
            raise RuntimeError("DUPLICATE_IDENTITY_CONFLICT")
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("ACTION_DIRECTION_INVALID")
        if type(reduce_only) is not bool:
            raise ValueError("ACTION_REDUCE_ONLY_INVALID")
        entry_profiles = {
            "ENTRY_MARKET",
            "ENTRY_LIMIT",
            "ENTRY_STOP_MARKET",
        }
        reduction_profiles = {
            "REDUCE_OR_CLOSE_MARKET",
            "PROTECTIVE_STOP_REDUCE_ONLY",
            "TAKE_PROFIT_1",
            "TAKE_PROFIT_2",
        }
        if (profile in entry_profiles and reduce_only) or (
            profile in reduction_profiles and not reduce_only
        ):
            raise ValueError("ACTION_REDUCE_ONLY_PROFILE_MISMATCH")
        if type(post_only) is not bool:
            raise ValueError("VENUE_ORDER_POLICY_INVALID")
        if price_match is not None and not isinstance(price_match, str):
            raise ValueError("VENUE_ORDER_POLICY_INVALID")
        if expire_at is not None and not isinstance(expire_at, datetime):
            raise ValueError("VENUE_ORDER_POLICY_INVALID")
        if trigger_source is not None and not isinstance(trigger_source, str):
            raise ValueError("VENUE_TRIGGER_SOURCE_INVALID")
        if profile == "REDUCE_OR_CLOSE_MARKET":
            if position_side not in {"BOTH", "LONG", "SHORT"}:
                raise ValueError("ACTION_POSITION_SIDE_INVALID")
        elif position_side is not None:
            raise ValueError("ACTION_POSITION_SIDE_INVALID")
        instrument_id = InstrumentId.from_str(f"{instrument_ref}.BINANCE")
        if self._instrument_id != instrument_id:
            raise RuntimeError("AUTHORIZATION_MISMATCH")
        if self.cache.instrument(instrument_id) is None:
            raise RuntimeError("INSTRUMENT_NOT_IN_CACHE")
        order_side = (
            OrderSide.BUY
            if (direction == "LONG") is not reduce_only
            else OrderSide.SELL
        )
        common = {
            "instrument_id": instrument_id,
            "order_side": order_side,
            "quantity": Quantity.from_str(quantity),
            "client_order_id": ClientOrderId(client_order_id),
        }
        if profile == "ENTRY_MARKET" or profile == "REDUCE_OR_CLOSE_MARKET":
            if (
                time_in_force is not None
                or post_only
                or price_match is not None
                or expire_at is not None
                or trigger_source is not None
            ):
                raise ValueError("VENUE_ORDER_POLICY_CONFLICT")
            order = self.order_factory.market(**common, reduce_only=reduce_only)
        elif profile == "ENTRY_LIMIT":
            if price is None:
                raise ValueError("ACTION_PROFILE_MISMATCH")
            if trigger_source is not None:
                raise ValueError("VENUE_TRIGGER_SOURCE_CONFLICT")
            if time_in_force is None:
                if post_only or price_match is not None or expire_at is not None:
                    raise ValueError("VENUE_ORDER_POLICY_CONFLICT")
                normalized_time_in_force = TimeInForce.GTC
            else:
                try:
                    normalized_time_in_force = _LIMIT_TIME_IN_FORCE[time_in_force]
                except (KeyError, TypeError):
                    raise ValueError("VENUE_ORDER_POLICY_INVALID") from None
            if (
                price_match is not None
                and price_match not in _BINANCE_PRICE_MATCH_VALUES
            ):
                raise ValueError("VENUE_ORDER_POLICY_INVALID")
            if post_only and (
                normalized_time_in_force is not TimeInForce.GTC
                or price_match is not None
            ):
                raise ValueError("VENUE_ORDER_POLICY_CONFLICT")
            if (normalized_time_in_force is TimeInForce.GTD) != (expire_at is not None):
                raise ValueError("VENUE_ORDER_POLICY_CONFLICT")
            if expire_at is not None and expire_at.utcoffset() is None:
                raise ValueError("VENUE_ORDER_POLICY_INVALID")
            order = self.order_factory.limit(
                **common,
                price=Price.from_str(price),
                time_in_force=normalized_time_in_force,
                expire_time=expire_at,
                post_only=post_only,
            )
        elif profile in {
            "ENTRY_STOP_MARKET",
            "PROTECTIVE_STOP_REDUCE_ONLY",
        }:
            if (
                time_in_force is not None
                or post_only
                or price_match is not None
                or expire_at is not None
            ):
                raise ValueError("VENUE_ORDER_POLICY_CONFLICT")
            if trigger_price is None:
                raise ValueError("ACTION_PROFILE_MISMATCH")
            try:
                normalized_trigger_type = (
                    TriggerType.LAST_PRICE
                    if trigger_source is None
                    else _CONDITIONAL_TRIGGER_TYPES[trigger_source]
                )
            except KeyError:
                raise ValueError("VENUE_TRIGGER_SOURCE_INVALID") from None
            order = self.order_factory.stop_market(
                **common,
                trigger_price=Price.from_str(trigger_price),
                trigger_type=normalized_trigger_type,
                reduce_only=reduce_only,
            )
        elif profile in {"TAKE_PROFIT_1", "TAKE_PROFIT_2"}:
            if (
                time_in_force is not None
                or post_only
                or price_match is not None
                or expire_at is not None
            ):
                raise ValueError("VENUE_ORDER_POLICY_CONFLICT")
            if trigger_price is None:
                raise ValueError("ACTION_PROFILE_MISMATCH")
            try:
                normalized_trigger_type = (
                    TriggerType.LAST_PRICE
                    if trigger_source is None
                    else _CONDITIONAL_TRIGGER_TYPES[trigger_source]
                )
            except KeyError:
                raise ValueError("VENUE_TRIGGER_SOURCE_INVALID") from None
            order = self.order_factory.market_if_touched(
                **common,
                trigger_price=Price.from_str(trigger_price),
                trigger_type=normalized_trigger_type,
                reduce_only=True,
            )
        else:
            raise ValueError("ACTION_PROFILE_UNQUALIFIED")
        self._persisted_orders[client_order_id] = order
        position_id = (
            PositionId(f"{instrument_id}-{position_side}")
            if position_side in {"LONG", "SHORT"}
            else None
        )
        if price_match is None:
            if position_id is None:
                self.submit_order(order)
            else:
                # Nautilus maps a position id ending in LONG/SHORT to Binance's
                # mandatory Hedge Mode positionSide and omits reduceOnly on wire.
                self.submit_order(order, position_id=position_id)
        else:
            if position_id is not None:
                raise ValueError("VENUE_ORDER_POLICY_CONFLICT")
            self.submit_order(order, params={"price_match": price_match})
        return order

    def _query_persisted_order(
        self,
        capability: object,
        client_order_id: str,
        *,
        instrument_ref: str,
    ) -> object:
        self._require_persisted_action_capability(capability)
        existing = self._persisted_orders.get(client_order_id)
        if existing is None:
            existing = self.cache.order(ClientOrderId(client_order_id))
        if existing is not None and getattr(existing, "venue_order_id", None) is not None:
            self._persisted_orders[client_order_id] = existing
            self.query_order(existing)
            return existing

        # A crash can occur after Halpha commits SUBMITTING but before Strategy
        # creates an Order. A locally rejected/ambiguous Order can likewise be
        # present in cache without a venue order id; Strategy.query_order cannot
        # route that object. Query the immutable client identity directly
        # through Nautilus instead of fabricating a cache Order or using a
        # second SDK.
        if existing is not None:
            self._persisted_orders[client_order_id] = existing
        instrument_id = InstrumentId.from_str(f"{instrument_ref}.BINANCE")
        if self._instrument_id != instrument_id:
            raise RuntimeError("AUTHORIZATION_MISMATCH")
        if self.trader_id is None or self.msgbus is None:
            raise RuntimeError("STRATEGY_ADAPTER_NOT_READY")
        if "ExecEngine.execute" not in self.msgbus.endpoints():
            raise RuntimeError("EXECUTION_QUERY_ENDPOINT_UNAVAILABLE")
        command = QueryOrder(
            trader_id=self.trader_id,
            strategy_id=self.id,
            instrument_id=instrument_id,
            client_order_id=ClientOrderId(client_order_id),
            venue_order_id=None,
            command_id=UUID4(),
            ts_init=self.clock.timestamp_ns(),
        )
        self.msgbus.send(endpoint="ExecEngine.execute", msg=command)
        return command

    def _cancel_persisted_order(self, capability: object, client_order_id: str) -> object:
        self._require_persisted_action_capability(capability)
        order = self._persisted_order(client_order_id)
        if order.is_open:
            self.cancel_order(order)
        else:
            self.query_order(order)
        return order

    def _persisted_order(self, client_order_id: str) -> object:
        existing = self._persisted_orders.get(client_order_id)
        if existing is not None:
            return existing
        recovered = self.cache.order(ClientOrderId(client_order_id))
        if recovered is None:
            raise RuntimeError("POSITION_UNKNOWN")
        self._persisted_orders[client_order_id] = recovered
        return recovered

    def _require_persisted_action_capability(self, capability: object) -> None:
        if (
            self._persisted_action_capability is None
            or capability is not self._persisted_action_capability
        ):
            raise RuntimeError("AUTHORIZATION_MISMATCH")

    def _forward_execution_event(self, event: object) -> None:
        if self._execution_event_sink is not None:
            self._execution_event_sink(event)

    def _release_terminal_persisted_order(self, event: object) -> None:
        client_order_id = getattr(event, "client_order_id", None)
        if client_order_id is None:
            return
        self._persisted_orders.pop(str(client_order_id), None)

    def on_order_submitted(self, event: object) -> None:
        self._forward_execution_event(event)

    def on_order_accepted(self, event: object) -> None:
        self._forward_execution_event(event)

    def on_order_rejected(self, event: object) -> None:
        self._forward_execution_event(event)
        self._release_terminal_persisted_order(event)

    def on_order_denied(self, event: object) -> None:
        self._forward_execution_event(event)
        self._release_terminal_persisted_order(event)

    def on_order_updated(self, event: object) -> None:
        self._forward_execution_event(event)

    def on_order_filled(self, event: object) -> None:
        self._forward_execution_event(event)
        client_order_id = getattr(event, "client_order_id", None)
        if client_order_id is None:
            return
        order = self._persisted_orders.get(str(client_order_id))
        if order is not None and bool(getattr(order, "is_closed", False)):
            self._release_terminal_persisted_order(event)

    def on_order_canceled(self, event: object) -> None:
        self._forward_execution_event(event)
        self._release_terminal_persisted_order(event)

    def on_order_expired(self, event: object) -> None:
        self._forward_execution_event(event)
        self._release_terminal_persisted_order(event)

    def on_order_cancel_rejected(self, event: object) -> None:
        self._forward_execution_event(event)


@dataclass(frozen=True)
class ActivationAdapterSpec:
    activation_id: str
    factory: Callable[[], HalphaStrategyAdapter]


class ActivationAdapterLifecycle:
    """One adapter per runtime responsibility activation, owned by Controller."""

    def __init__(self, controller: ControllerPort) -> None:
        self._controller = controller
        self._adapters: dict[str, HalphaStrategyAdapter] = {}

    @property
    def activation_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def adapter_for_activation(self, activation_id: str) -> HalphaStrategyAdapter:
        try:
            return self._adapters[activation_id]
        except KeyError:
            raise RuntimeError("ACTIVATION_ADAPTER_NOT_READY") from None

    def start(self, spec: ActivationAdapterSpec) -> HalphaStrategyAdapter:
        existing = self._adapters.get(spec.activation_id)
        if existing is not None:
            return existing
        adapter = spec.factory()
        if adapter.activation_id != spec.activation_id:
            raise ValueError("ADAPTER_ACTIVATION_MISMATCH")
        self._controller.create_strategy(adapter, start=True)
        self._adapters[spec.activation_id] = adapter
        return adapter

    def stop_and_remove(self, activation_id: str) -> None:
        adapter = self._adapters.pop(activation_id, None)
        if adapter is None:
            return
        self._controller.stop_strategy(adapter)
        self._controller.remove_strategy(adapter)

    def stop_all(self) -> None:
        for activation_id in tuple(self._adapters):
            self.stop_and_remove(activation_id)
