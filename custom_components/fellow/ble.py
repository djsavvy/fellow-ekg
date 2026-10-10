"""BLE telemetry for the Fellow Stagg EKG Pro on firmware 1.2.x.

On firmware 1.2.24+ the kettle's HTTP text interface is silenced (commands
still run, output does not), so current temperature and heating state are
no longer readable over Wi-Fi. They are, however, pushed over the custom
BLE GATT service (see docs/fellow-stagg-ekg-api.md in the pencil-pusher
repo for the reverse-engineering provenance):

  service 7aebf330-6cb1-46e4-b23b-7cc2262c605e
    2291c4b1…  quick status, notifies on change (16 bytes, read as zeros)
    2291c4b2…  debug temperature, notifies every "tstprd" period
               (12 bytes: u32 tick, u16 seq, u16 A, u16 B, u16 C — three
               temperature readings; °F×10 with units Fahrenheit)
    2291c4b5…  the 17-byte settings struct (same as /api t=3), notifies
               on settings change

The notify stream only runs while a central is subscribed, and the
`tstprd` CLI period is runtime-only (cleared by a kettle reboot), so this
manager re-arms it after (re)connecting and whenever the stream goes
quiet.

Connection goes through Home Assistant's bluetooth integration (i.e.
the ESPHome bluetooth proxies) via bleak-retry-connector, the same way
our hass-dyson fork talks to the Solarcycle lamp. The kettle accepts one
BLE connection at a time; while Home Assistant holds it, the Fellow app
cannot connect (same tradeoff as the lamp).
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# GATT (service 7aebf330-…)
BLE_SERVICE_UUID = "7aebf330-6cb1-46e4-b23b-7cc2262c605e"
BLE_CHAR_QUICK_STATUS_UUID = "2291c4b1-5d7f-4477-a88b-b266edb97142"
BLE_CHAR_DEBUG_TEMPERATURE_UUID = "2291c4b2-5d7f-4477-a88b-b266edb97142"
BLE_CHAR_SETTINGS_UUID = "2291c4b5-5d7f-4477-a88b-b266edb97142"

# CLI command that arms the temperature notify stream (ms). Verified on
# 1.2.26: `tstprd 1000` while subscribed produces a packet per second.
BLE_ARM_COMMAND = "tstprd 1000"

# Re-arm when no debug-temperature packet has arrived for this long.
WATCHDOG_SECONDS = 20.0

# Reconnect backoff.
RECONNECT_DELAYS = (5.0, 15.0, 30.0, 60.0)

# Plausibility windows for accepting decoded values (raw, unit×10).
TEMP_PLAUSIBLE_F10 = (400, 2130)  # 40..213 °F
TEMP_PLAUSIBLE_C10 = (200, 1000)  # 20..100 °C


def _u16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


@dataclass
class KettleBLEState:
    """Latest snapshot of the BLE telemetry."""

    current_temp_c: float | None = None
    # Raw values (unit×10) for diagnostics, plus the decoded triple.
    raw: tuple[int, int, int] | None = None
    seq: int | None = None
    # Authoritative heating bit from a quick-status packet, if decoded.
    heating: bool | None = None
    last_seen: datetime | None = None

    @property
    def is_fresh(self) -> bool:
        """Whether a packet arrived recently (kettle docked and streaming)."""
        if self.last_seen is None:
            return False
        return (dt_util.utcnow() - self.last_seen) < timedelta(seconds=60)


class KettleBLE:
    """Manage the BLE connection and telemetry stream for the kettle.

    The HTTP client (`StaggEKGClient`) exposes `set_ble_snapshot()` and a
    `arm_stream()` callback; the coordinator triggers refreshes from the
    notifications pushed here.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str,
        arm_stream: Callable[[], None],
        on_update: Callable[[], None],
    ) -> None:
        """Initialize.

        arm_stream: coroutine-safe callable that sends the `tstprd` CLI
        command over HTTP (executor job created internally).
        on_update: callback fired (in the event loop) after every packet.
        """
        self.hass = hass
        self.address = address.upper()
        self.name = name
        self._arm_stream = arm_stream
        self._on_update = on_update

        self.state = KettleBLEState()
        self._client: Any | None = None
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._last_packet = 0.0
        self._unsubscribe_unavailable: Callable[[], None] | None = None

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Start the connection loop in the background."""
        _LOGGER.info("Starting BLE telemetry for %s (%s)", self.name, self.address)
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._run(), name=f"{self.name} ble loop"),
            asyncio.create_task(self._watchdog(), name=f"{self.name} ble watchdog"),
        ]

    async def stop(self) -> None:
        """Disconnect and stop background tasks."""
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._unsubscribe_unavailable is not None:
            self._unsubscribe_unavailable()
            self._unsubscribe_unavailable = None
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # ------------------------------------------------------------------ loop

    async def _run(self) -> None:
        """Connect, subscribe, and reconnect forever."""
        delay_index = 0
        while not self._stop.is_set():
            try:
                connected = await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("BLE connect to %s failed: %s", self.name, exc)
                connected = False

            if connected:
                delay_index = 0
                # Stay connected until the client drops us; the
                # disconnected callback sets the reconnect in motion by
                # breaking this wait via an event.
                await self._wait_disconnect_or_stop()
                _LOGGER.info("BLE %s disconnected", self.name)

            if self._stop.is_set():
                return
            delay = RECONNECT_DELAYS[min(delay_index, len(RECONNECT_DELAYS) - 1)]
            delay_index += 1
            _LOGGER.debug(
                "BLE %s: reconnecting in %.0fs", self.name, delay
            )
            await asyncio.sleep(delay)

    async def _connect_once(self) -> bool:
        """Establish a connection and subscriptions. Returns True on success."""
        from homeassistant.components import bluetooth

        service_info = bluetooth.async_last_service_info(
            self.hass, self.address, connectable=True
        )
        if service_info is None:
            # Not seen by any connectable scanner — check it is visible at
            # all before giving up this round.
            if not bluetooth.async_last_service_info(
                self.hass, self.address, connectable=False
            ):
                _LOGGER.debug(
                    "BLE %s (%s) not seen by any scanner; waiting",
                    self.name,
                    self.address,
                )
            return False

        from bleak_retry_connector import establish_connection
        from bleak import BleakClient

        client = await establish_connection(
            BleakClient,
            service_info.device,
            self.name,
            disconnected_callback=self._on_disconnect,
            max_attempts=3,
            use_services_cache=True,
        )
        self._client = client
        _LOGGER.info("BLE %s connected, subscribing", self.name)

        try:
            await client.start_notify(
                BLE_CHAR_DEBUG_TEMPERATURE_UUID, self._handle_debug_temperature
            )
            await client.start_notify(
                BLE_CHAR_QUICK_STATUS_UUID, self._handle_quick_status
            )
            await client.start_notify(
                BLE_CHAR_SETTINGS_UUID, self._handle_settings
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("BLE %s: subscribe failed: %s", self.name, exc)
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
            return False

        # The stream only runs while subscribed, and the kettle clears the
        # period on reboot — arm it now (and the watchdog keeps it armed).
        await self._arm()
        return True

    async def _arm(self) -> None:
        """Send the `tstprd` CLI command over HTTP to arm the stream."""
        try:
            await self._arm_stream()
            _LOGGER.debug("BLE %s: armed temperature stream", self.name)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("BLE %s: arming failed: %s", self.name, exc)

    async def _watchdog(self) -> None:
        """Re-arm the stream when packets stop arriving (kettle reboot)."""
        while not self._stop.is_set():
            await asyncio.sleep(WATCHDOG_SECONDS)
            if self._stop.is_set():
                return
            if self._client is None:
                continue
            quiet = asyncio.get_running_loop().time() - self._last_packet
            if quiet > WATCHDOG_SECONDS:
                _LOGGER.info(
                    "BLE %s: no packets for %.0fs, re-arming stream",
                    self.name,
                    quiet,
                )
                await self._arm()

    async def _wait_disconnect_or_stop(self) -> None:
        """Wait until the client disconnects (or we are stopping)."""
        while not self._stop.is_set() and self._client is not None:
            await asyncio.sleep(1)

    @callback
    def _on_disconnect(self, _client) -> None:
        """bleak disconnected callback: drop the client so _run reconnects."""
        self._client = None

    # -------------------------------------------------------------- packets

    def _publish(self) -> None:
        self._last_packet = asyncio.get_running_loop().time()
        self._on_update()

    def _handle_debug_temperature(self, _handle: int, data: bytearray) -> None:
        """12-byte debug temperature packet: tick, seq, A, B, C."""
        if len(data) < 12:
            _LOGGER.debug("BLE %s: short temp packet %s", self.name, bytes(data).hex())
            return
        a, b, c = _u16(data, 6), _u16(data, 8), _u16(data, 10)
        # The kettle reports in its display units (unit×10). Conversion to
        # °C happens in the snapshot accessor (which knows the units); here
        # we just keep the raw triple.
        self.state.raw = (a, b, c)
        self.state.seq = _u16(data, 4)
        self.state.last_seen = dt_util.utcnow()
        _LOGGER.debug(
            "BLE %s: temp seq=%s raw=%s", self.name, self.state.seq, self.state.raw
        )
        self._publish()

    def _handle_quick_status(self, _handle: int, data: bytearray) -> None:
        """Quick-status packet (on change). Fields decoded from the 1.1.75
        image's Notify::send_quick_status: flags byte and a temperature.

        The observed packet length is 16 bytes with the interesting bytes
        at offsets 11 (flags) and 12..13 not confirmed; until the layout is
        verified live, accept only plausibly-decoded values and log the
        raw packet for future decoding.
        """
        _LOGGER.debug(
            "BLE %s: quick status %s", self.name, bytes(data).hex()
        )
        if len(data) < 20:
            return
        # Working layout (20-byte form): u32 ts @4, flags @11, temp u16 @16.
        flags = data[11]
        temp_raw = _u16(data, 16)
        heating = bool(flags & 0x02) if flags else None
        if (
            TEMP_PLAUSIBLE_F10[0] <= temp_raw <= TEMP_PLAUSIBLE_F10[1]
            or TEMP_PLAUSIBLE_C10[0] <= temp_raw <= TEMP_PLAUSIBLE_C10[1]
        ):
            self.state.heating = heating
            self.state.last_seen = dt_util.utcnow()
            self._publish()
        # else: not decodable yet — keep the periodic stream authoritative

    def _handle_settings(self, _handle: int, data: bytearray) -> None:
        """17-byte settings struct pushed on change (same as /api t=3).

        The HTTP poll picks this up on its own; we just trigger a refresh.
        """
        _LOGGER.debug("BLE %s: settings notify %s", self.name, bytes(data).hex())
        self.state.last_seen = dt_util.utcnow()
        self._publish()

    # ------------------------------------------------------------ snapshot

    def snapshot(self, units_fahrenheit: bool) -> KettleBLEState:
        """Return the state with `current_temp_c` decoded from the raw triple.

        `A` (the first value of the debug-temperature triple) leads the
        observed temperature changes and is used as the current
        temperature; all three are kept in `raw` for diagnostics.
        """
        st = self.state
        if st.raw is None:
            return st
        a = st.raw[0]
        if units_fahrenheit:
            st.current_temp_c = round((a / 10 - 32) * 5 / 9, 2)
        else:
            st.current_temp_c = round(a / 10, 2)
        return st