"""Stagg EKG+ Kettle API Client"""

import logging
import time
import requests
import re
import struct
from typing import Optional
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

# Dial rotation constants
STEP_DELAY = 0.05        # 50ms between individual dial step commands
BATCH_SIZE = 20           # steps per batch before re-reading state
SETTLE_DELAY = 0.3        # 300ms pause before verification read
MAX_TOTAL_STEPS = 150     # safety cap (full range is ~120 steps, plus margin)

# Guide mode presets (Fahrenheit values, in dial order left-to-right)
GUIDE_PRESETS_F = (180, 195, 200, 205, 212)


class KettleSettings:
    """The 17-byte RT_Settings struct from `/api?…t=3` (see the repo docs:
    docs/fellow-stagg-ekg-api.md). Verified against firmware 1.2.26.

    The query string is fixed-order `i=,p=,d=,t=,s=` — the kettle's parser
    walks it with strtol and rejects anything else, so it is built as a
    literal string rather than params.
    """

    __slots__ = ("mask", "target_f", "clock", "counter", "units_fahrenheit")

    def __init__(self, blob: bytes) -> None:
        self.mask = struct.unpack_from("<H", blob, 0)[0]
        self.target_f: Optional[float] = None
        self.clock = "00:00"
        self.counter = blob[16]
        # Byte 1 of the presence mask distinguishes the display units in
        # every observation on this kettle: 0x15 = °F, 0x17 = °C.
        self.units_fahrenheit = (blob[1] & 0x02) == 0
        target_raw = struct.unpack_from("<H", blob, 6)[0]
        if self.units_fahrenheit:
            # Plain degrees Fahrenheit (observed: 205 -> 0x00cd).
            self.target_f = float(target_raw)
        elif target_raw < 0x8000:
            # The °C encoding was observed once as 0x80c0 for 90.5 °C and
            # is not decoded; anything else here is treated as unusable.
            self.target_f = None
        hour, minute = blob[11], blob[10]
        if hour < 24 and minute < 60:
            self.clock = f"{hour:02d}:{minute:02d}"

    @property
    def target_c(self) -> Optional[float]:
        if self.target_f is None:
            return None
        return round((self.target_f - 32) * 5 / 9, 2)


class KettleTransientError(Exception):
    """Base class for transient kettle failures the caller can ride out.

    The kettle briefly stops answering HTTP (or returns truncated data)
    when it transitions to Off and at other moments. Callers can treat
    any subclass as a non-fatal hiccup and keep their last known state.
    """


class KettleTimeoutError(KettleTransientError):
    """Raised when the kettle's HTTP server doesn't respond in time."""


class KettleResponseError(KettleTransientError):
    """Raised when the kettle's response can't be parsed (missing fields)."""


@dataclass
class KettleState:
    """Represents the current state of the kettle"""
    mode: str
    current_temp_c: Optional[float]
    set_temp_c: float
    units: int  # 0=Fahrenheit, 1=Celsius (kettle's internal representation)
    clock: str
    ble_connected: bool
    heating: bool
    warming: bool
    screen_name: Optional[str] = None  # Current screen showing

    @property
    def is_heating(self) -> bool:
        """Return if kettle is in a heating cycle (based on mode, not GPIO)."""
        return self.mode in ("S_Heat", "S_StartupToTempr", "S_Heat+menu")

    @property
    def is_off(self) -> bool:
        """Return if kettle is in Off mode."""
        return self.mode == "S_Off"

    @property
    def is_holding(self) -> bool:
        """Return if kettle is in Hold (keep-warm) mode."""
        return self.mode == "S_Hold"

    @property
    def is_powered_on(self) -> bool:
        """Return if kettle is powered on (not in Off mode)."""
        return not self.is_off

    @property
    def is_in_menu(self) -> bool:
        """Return if kettle is showing a menu screen."""
        return "menu" in self.mode.lower() if self.mode else False

    @property
    def is_docked(self) -> bool:
        """Whether the kettle is sitting on the base."""
        return self.current_temp_c is not None

    @property
    def may_have_no_water(self) -> bool:
        """Return if kettle may not have water (temp < 30C heuristic)."""
        if self.current_temp_c is None:
            return True
        return self.current_temp_c < 30

    @property
    def current_temperature(self) -> Optional[float]:
        """Return current temperature in Celsius."""
        return self.current_temp_c

    @property
    def target_temperature(self) -> float:
        """Return target temperature in Celsius."""
        return self.set_temp_c


class StaggEKGClient:
    """Client for interacting with Stagg EKG+ kettle"""

    def __init__(
        self,
        host: str = "10.1.1.177",
        port: int = 80,
        temp_method: str = "direct",
    ):
        """Initialize the client.

        temp_method controls how `set_temperature` reaches the target:
          - "direct": single `setsetting settempr <F>` firmware call (fast)
          - "dial":   emulate physical dial (works on older/quirky firmwares)
        """
        self.base_url = f"http://{host}:{port}"
        self.temp_method = temp_method
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'StaggEKG-HA/1.0'})

        # BLE telemetry (set by the integration's KettleBLE manager):
        # latest KettleBLEState, or None when BLE is not running.
        self.ble_state = None
        # Whether we commanded the heater on (ss S_Heat) — the HTTP path
        # cannot read the mode on 1.2.24+, so until a quick-status packet
        # decodes authoritatively this is the fallback source of truth.
        self.commanded_heat: Optional[bool] = None
        self.last_settings: Optional[KettleSettings] = None

    def _send_command(self, cmd: str, retries: int = 1, timeout: float = 5.0) -> str:
        """Send a CLI command to the kettle, retrying on timeout.

        The kettle's HTTP server goes unresponsive for several seconds
        during mode transitions (especially when turning off), so a single
        read timeout is not a reliable signal that anything is wrong.
        Worst-case wall time with the defaults is ~10.5s (2 × 5s + 0.5s
        backoff) — short enough that the coordinator's 5s poll won't pile
        up too far behind. If the kettle is still unresponsive after the
        final attempt, raise KettleTimeoutError so callers can distinguish
        it from other request failures.

        On firmware 1.2.24+ commands execute blind (empty reply), which is
        fine — `get_state` verifies through /api and BLE instead.
        """
        url = f"{self.base_url}/cli"
        params = {"cmd": cmd}

        last_timeout: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=timeout)
                response.raise_for_status()
                return response.text
            except requests.exceptions.Timeout as e:
                last_timeout = e
                if attempt < retries:
                    _LOGGER.debug(
                        "Kettle timeout on '%s' (attempt %d/%d), retrying",
                        cmd, attempt + 1, retries + 1,
                    )
                    time.sleep(0.5)
            except requests.exceptions.RequestException as e:
                raise Exception(f"Failed to send command '{cmd}': {e}")

        raise KettleTimeoutError(
            f"Kettle did not respond to '{cmd}' after {retries + 1} attempts: {last_timeout}"
        )

    # ------------------------------------------------------------- /api read

    def get_settings(self) -> Optional[KettleSettings]:
        """GET the 17-byte RT_Settings struct from /api (t=3).

        Returns None if the endpoint is missing/unusable, so callers can
        fall back to the 1.1.x text interface.
        """
        url = f"{self.base_url}/api?i=0,p=0,d=0,t=3,s=1"
        try:
            response = self.session.get(url, timeout=5.0)
        except requests.exceptions.RequestException:
            return None
        if response.status_code != 200 or len(response.content) != 17:
            return None
        settings = KettleSettings(response.content)
        self.last_settings = settings
        return settings

    def build_state(self, refresh_settings: bool = True) -> KettleState:
        """Build a KettleState from /api settings plus BLE telemetry.

        With `refresh_settings=False` the last fetched settings are used,
        so the BLE notify path can rebuild state without an HTTP request.

        Raises KettleResponseError if neither source has ever produced data.
        """
        settings = self.get_settings() if refresh_settings else None
        ble = self.ble_state
        if settings is None:
            settings = self.last_settings
        if settings is None and ble is None:
            raise KettleResponseError("no /api settings and no BLE telemetry")

        units = 0 if (settings and settings.units_fahrenheit) else 1

        current_temp_c: Optional[float] = None
        heating: Optional[bool] = None
        if ble is not None and ble.raw is not None and ble.is_fresh:
            # Decode the raw triple in the kettle's display units.
            a = ble.raw[0]
            current_temp_c = (
                round((a / 10 - 32) * 5 / 9, 2)
                if settings and settings.units_fahrenheit
                else round(a / 10, 2)
            )
            # Authoritative heating bit from a quick-status packet, else
            # our own commanded state.
            heating = ble.heating if ble.heating is not None else self.commanded_heat
        elif self.commanded_heat is not None:
            heating = self.commanded_heat

        if heating is None:
            mode = "unknown"
        elif heating:
            mode = "S_Heat"
        else:
            mode = "S_Off"

        return KettleState(
            mode=mode,
            current_temp_c=current_temp_c,
            set_temp_c=(
                settings.target_c
                if settings and settings.target_c is not None
                else 0.0
            ),
            units=units,
            clock=(settings.clock if settings else "00:00"),
            ble_connected=bool(ble and ble.is_fresh),
            heating=bool(heating) if heating is not None else False,
            warming=False,
        )

    def get_state(self) -> KettleState:
        """Get the current state of the kettle.

        Firmware 1.2.24+ silences the CLI text output, so the structured
        /api endpoint plus BLE telemetry is the read path; the text parse
        below only runs when /api answers nothing (older firmwares).
        """
        try:
            state = self.build_state()
            # `unknown` (no BLE telemetry, nothing commanded yet) is still
            # a real state on 1.2.26 — the text reply there is empty, so
            # falling through would only raise. Only fall back when /api
            # itself is unusable (build_state raised).
            return state
        except KettleResponseError:
            pass

        # Fall back to the 1.1.x text interface.
        return self._get_state_text()

    def _get_state_text(self) -> KettleState:
        """Read state from the 1.1.x `/cli?cmd=state` text output."""
        response = self._send_command("state")

        # Parse the response
        mode_match = re.search(r'mode=(\S+)', response)
        if not mode_match:
            # Treat a malformed response the same as a transient timeout —
            # the kettle occasionally returns truncated data and there's no
            # safe default for `mode` (returning "Unknown" silently flips
            # is_off/is_heating to False, which downstream entities then
            # show as a real state change).
            raise KettleResponseError(
                f"Kettle response missing 'mode' field: {response!r}"
            )

        screen_match = re.search(r'scrname=(.+?)(?:\n|$)', response)
        # tempr = current water temperature
        # temprT = target temperature (set via dial)
        temp_c_match = re.search(r'tempr=([\d.]+)\s*C', response)
        temp_set_c_match = re.search(r'temprT=([\d.]+)\s*C', response)
        units_match = re.search(r'units=(\d+)', response)
        clock_match = re.search(r'clock=(\d+:\d+)', response)
        ble_match = re.search(r'ble conn=(\d+)', response)
        heating_match = re.search(r'ho\s+(\d+)', response)
        warming_match = re.search(r'wd\s+(\d+)', response)

        return KettleState(
            mode=mode_match.group(1),
            screen_name=screen_match.group(1).strip() if screen_match else None,
            current_temp_c=float(temp_c_match.group(1)) if temp_c_match else None,
            set_temp_c=float(temp_set_c_match.group(1)) if temp_set_c_match else 0,
            units=int(units_match.group(1)) if units_match else 1,
            clock=clock_match.group(1) if clock_match else "00:00",
            ble_connected=bool(int(ble_match.group(1))) if ble_match else False,
            heating=bool(int(heating_match.group(1))) if heating_match else False,
            warming=bool(int(warming_match.group(1))) if warming_match else False,
        )

    def heat_on(self) -> str:
        """Turn heating element on (direct GPIO)"""
        return self._send_command("heaton")

    def heat_off(self) -> str:
        """Turn heating element off (direct GPIO)"""
        return self._send_command("heatoff")

    def warm_on(self) -> str:
        """Turn warming on"""
        return self._send_command("warmon")

    def warm_off(self) -> str:
        """Turn warming off"""
        return self._send_command("warmoff")

    def press_button_1(self) -> str:
        """Simulate pressing button 1 (base button - opens menus)"""
        return self._send_command("1")

    def press_button_2(self) -> str:
        """Simulate pressing button 2 (dial press - wakes & starts heating)"""
        return self._send_command("2")

    def start_heating(self) -> str:
        """Start a heating cycle via direct state command.

        `ss S_Heat` tells the firmware to enter the Heat state from any
        starting state, so we don't need to wake the kettle first or worry
        about the button-2 toggle behavior. We still skip the command if
        the kettle is already heating to avoid unnecessary chatter.
        """
        state = self.get_state()
        if state.is_heating:
            _LOGGER.debug("Kettle already heating, skipping start command")
            return "already heating"
        self._send_command("ss S_Heat")
        self.commanded_heat = True
        return "ok"

    def stop_heating(self, max_attempts: int = 3) -> str:
        """Stop heating or holding via direct state command.

        `ss S_Off` puts the firmware in the Off state directly. On
        1.2.24+ we track the commanded state ourselves (the text reply
        is silenced); the retry loop still runs for 1.1.x where the
        mode is readable.
        """
        self._send_command("ss S_Off")
        self.commanded_heat = False
        for attempt in range(max_attempts):
            state = self.get_state()
            if not state.is_heating and not state.is_holding:
                return "stopped"

            _LOGGER.debug(
                "Kettle not stopped yet (attempt %d/%d, mode=%s)",
                attempt + 1, max_attempts, state.mode,
            )
            self._send_command("ss S_Off")
            time.sleep(0.5)

        _LOGGER.warning("Kettle may not have stopped (mode=%s)", state.mode)
        return "stopped"

    def set_warming(self, on: bool) -> str:
        """Enable or disable keep-warm mode."""
        if on:
            return self.warm_on()
        else:
            return self.warm_off()

    def _ensure_awake(self) -> KettleState:
        """Wake the kettle if it's off. Returns current state.

        1.1.x path only (pre-wake needed for dial rotation): button 2 is
        the only clean wake path, and it also starts heating as a side
        effect. On 1.2.26 the mode is often "unknown" (unreadable), and
        pressing button 2 on a merely-unknown mode would start a real
        heat cycle, so this method is now called ONLY from the dial
        methods with a text-confirmed Off mode, never on unknown.
        """
        state = self.get_state()
        if state.mode == "unknown" or not state.is_off:
            return state

        _LOGGER.debug("Kettle is off — waking with button 2")
        self._send_command("2")
        for _ in range(10):  # up to 5 seconds
            time.sleep(0.5)
            state = self.get_state()
            if not state.is_off:
                return state

        _LOGGER.warning("Kettle did not wake after button 2 (mode=%s)", state.mode)
        return state

    def get_guide_mode(self) -> bool:
        """Check if guide mode is enabled."""
        response = self._send_command("prtsettings")
        match = re.search(r'guide=(\d+)', response)
        return bool(int(match.group(1))) if match else False

    def set_guide_mode(self, enabled: bool) -> str:
        """Enable or disable guide mode."""
        return self._send_command(f"setsetting guide {1 if enabled else 0}")

    def set_temperature(self, target_celsius: float) -> float:
        """Set the target temperature.

        On firmware 1.2.24+ (and any firmware where /api answers) this is a
        single `setsetting settempr <F>` command with a patient read-back
        through /api. The 1.1.x dial-rotation fallback is DANGEROUS on
        1.2.26: its wake path presses button 2, which starts heating as a
        side effect, and its `left`/`right` bursts load the kettle's HTTP
        server hard enough to wedge its Wi-Fi stack (verified the hard way).
        It now only runs when /api is unreachable (a pre-1.2 kettle with a
        silent CLI), never after a mere read-back mismatch.

        Args:
            target_celsius: Desired target temperature in Celsius (40-100).

        Returns:
            The actual target temperature (Celsius) achieved on the kettle.
        """
        target_celsius = max(40.0, min(100.0, target_celsius))
        target_f = round(target_celsius * 9 / 5 + 32)

        return self._set_temperature_direct(target_f)

    def _set_temperature_direct(self, target_f: int) -> float:
        """Set target temperature via `setsetting settempr <F>` with retries.

        The kettle applies the setting from its main loop, not the HTTP
        thread, so the /api read-back can briefly show the previous target
        (observed well past the old 0.3s settle). Retry the command and
        read back patiently before giving up.

        Never falls back to dial rotation on a mere read-back mismatch:
        see set_temperature(). The dial path (which presses button 2 —
        starting a heat cycle — and bursts `left`/`right` commands) is
        reachable only via temp_method="dial", for pre-1.2 kettles.
        """
        for attempt in range(3):
            self._send_command(f"setsetting settempr {target_f}")
            for _ in range(8):  # up to ~4s per attempt
                time.sleep(0.5)
                settings = self.get_settings()
                if settings is not None and settings.target_f is not None:
                    actual_f = round(settings.target_f)
                    if abs(actual_f - target_f) <= 1:
                        _LOGGER.debug(
                            "settempr -> %dF confirmed (attempt %d)",
                            actual_f, attempt + 1,
                        )
                        return settings.target_c
        _LOGGER.warning(
            "setsetting settempr %dF not confirmed by /api read-back; "
            "giving up (no dial fallback on this firmware).",
            target_f,
        )
        settings = self.last_settings
        return settings.target_c if settings and settings.target_c else (target_f - 32) * 5 / 9

    def _set_temperature_preset(self, preset_index: int, target_f: int) -> float:
        """Navigate to a guide mode preset by index."""
        self._ensure_awake()

        # Reset to leftmost preset
        for _ in range(len(GUIDE_PRESETS_F)):
            self._send_command("left")
            time.sleep(STEP_DELAY)
        time.sleep(SETTLE_DELAY)

        # Navigate to target preset
        for _ in range(preset_index):
            self._send_command("right")
            time.sleep(STEP_DELAY)
        time.sleep(SETTLE_DELAY)

        state = self.get_state()
        _LOGGER.debug(
            "Preset selected: %dF (index %d), actual=%.1fC",
            target_f, preset_index, state.set_temp_c,
        )
        return state.set_temp_c

    def _set_temperature_dial(self, target_celsius: float, guide_on: bool) -> float:
        """Set temperature using batched dial rotation with verification."""
        if guide_on:
            _LOGGER.debug("Target doesn't match a preset — temporarily disabling guide mode")
            self.set_guide_mode(False)
            time.sleep(0.3)

        try:
            self._ensure_awake()

            total_steps_sent = 0

            while total_steps_sent < MAX_TOTAL_STEPS:
                state = self.get_state()
                current_target_c = state.set_temp_c

                if state.units == 0:
                    # Fahrenheit mode: each dial step is 1F
                    target_f = round(target_celsius * 9 / 5 + 32)
                    current_f = round(current_target_c * 9 / 5 + 32)
                    steps_needed = target_f - current_f
                else:
                    # Celsius mode: each dial step is 0.5C
                    steps_needed = int(round((target_celsius - current_target_c) * 2))

                if steps_needed == 0:
                    _LOGGER.debug(
                        "Temperature set complete: target=%.1fC, actual=%.1fC, total_steps=%d",
                        target_celsius, current_target_c, total_steps_sent,
                    )
                    return current_target_c

                # Send up to BATCH_SIZE steps
                batch = min(abs(steps_needed), BATCH_SIZE)
                cmd = "right" if steps_needed > 0 else "left"

                _LOGGER.debug(
                    "Sending %d dial steps (%s): current=%.1fC, target=%.1fC, remaining=%d",
                    batch, cmd, current_target_c, target_celsius, abs(steps_needed),
                )

                for _ in range(batch):
                    self._send_command(cmd)
                    time.sleep(STEP_DELAY)

                total_steps_sent += batch
                time.sleep(SETTLE_DELAY)

            # Exhausted step budget - return whatever we achieved
            final_state = self.get_state()
            _LOGGER.warning(
                "Temperature set exhausted step budget (%d steps): target=%.1fC, actual=%.1fC",
                total_steps_sent, target_celsius, final_state.set_temp_c,
            )
            return final_state.set_temp_c
        finally:
            if guide_on:
                _LOGGER.debug("Restoring guide mode")
                self.set_guide_mode(True)

    def rotate_dial_left(self, steps: int = 1) -> str:
        """Rotate dial left (decrease temperature)"""
        result = ""
        for _ in range(steps):
            result = self._send_command("left")
            if steps > 1:
                time.sleep(STEP_DELAY)
        return result

    def rotate_dial_right(self, steps: int = 1) -> str:
        """Rotate dial right (increase temperature)"""
        result = ""
        for _ in range(steps):
            result = self._send_command("right")
            if steps > 1:
                time.sleep(STEP_DELAY)
        return result

    def set_units_fahrenheit(self) -> str:
        """Set temperature units to Fahrenheit"""
        return self._send_command("setunitsf")

    def set_units_celsius(self) -> str:
        """Set temperature units to Celsius"""
        return self._send_command("setunitsc")
