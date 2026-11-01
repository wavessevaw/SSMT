"""Autopilot: GFG Extreme's own adaptive mode (3.0.4).

Autopilot does not switch between Battery, Balanced and Quality. It is one mode that keeps
building itself around the situation, through the pilot (the game's base frame cap) and the
Governor (the TDP), both told the objective "autopilot":

  start     15 W, the game measured there (the pilot finds the point it holds)
  tolerate  dips of the game's frame rate up to 8-10 %: no watts for those, only a deeper fall
            gets watts back at once
  target    12 W is good, 9-10 W ideal: a point held only above 12 W for a minute steps down,
            and Autopilot climbs only while the TDP sits at the ideal
  x3.75     only on a low battery (below 20 %, back above 25 %) or with the "battery"
            preference, or when a game cannot hold x3 even at full power: x3.75 shows the most
            artefacts

This class only tracks the battery and heat and says what Autopilot is doing.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

LOW_BATTERY = 20.0
BATTERY_RECOVER = 25.0
HOT_C = 85.0
COOL_C = 80.0         # hot ends below this ...
COOL_S = 30.0         # ... held this long


class Autopilot:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.reset()

    def _heat(self, temp_c: Optional[float]) -> bool:
        """Hot from 85 C until 30 s below 80 C, so 84.9/85.1 readings do not flip the reason."""
        now = self.clock()
        if temp_c is not None and temp_c >= HOT_C:
            self.hot, self.cool_since = True, None
        elif self.hot and temp_c is not None and temp_c < COOL_C:
            self.cool_since = now if self.cool_since is None else self.cool_since
            if now - self.cool_since >= COOL_S:
                self.hot, self.cool_since = False, None
        else:
            self.cool_since = None
        return self.hot

    def decide(self, preference: str, power: Dict[str, Any], temp_c: Optional[float]) -> Dict[str, Any]:
        pct = power.get("percent")
        if pct is not None:
            if pct < LOW_BATTERY:
                self.low_battery = True
            elif pct > BATTERY_RECOVER:
                self.low_battery = False
        if self._heat(temp_c):
            reason = f"APU at {temp_c:.0f} C: the Governor lowers the limit" if temp_c is not None else "APU hot"
        elif self.low_battery:
            reason = (f"battery below {LOW_BATTERY:.0f} %: following the game within 12 W"
                      if preference == "smoothness" else
                      f"battery below {LOW_BATTERY:.0f} %: conserving power at the permitted low point")
        elif power.get("external_power"):
            reason = "on external power: adapting, target 12 W"
        else:
            reason = "adapting: target 12 W, ideal 9-10 W"
        self.reason = reason
        return {"objective": "autopilot", "reason": reason}

    def reset(self) -> None:
        self.reason = ""
        self.low_battery = False
        self.hot = False
        self.cool_since: Optional[float] = None
