"""Worker-owned framing filters shared by MPV windows and GPU textures."""
from __future__ import annotations

from math import gcd

from clip_transform import framing_filter, transform_is_identity
from project_settings import timeline_dimensions


def preview_dimensions(project):
    """Fit an even, exact-aspect preview canvas within 1280 x 720.

    Never upscale. Some unusual ratios have no smaller even representation
    within the cap; retain their original canvas rather than distort it.
    Project settings and export dimensions remain untouched.
    """
    width, height = timeline_dimensions(project, (1920, 1080))
    if width <= 1280 and height <= 720:
        return width, height
    divisor = gcd(width, height)
    unit_width, unit_height = 2 * width // divisor, 2 * height // divisor
    multiple = min(divisor // 2, 1280 // unit_width, 720 // unit_height)
    if multiple < 1:
        return width, height
    return unit_width * multiple, unit_height * multiple


class RealtimeTransform:
    """Replace only our labelled filters, and leave untouched players alone.

    Install before loadfile so a staging decoder's first frame is framed.
    MPV keeps vf across files; baked previews therefore explicitly remove it.
    Successful commands alone advance the per-controller cache.
    """

    LABEL = "okk_framing"
    INPUT_LABEL = "okk_framing_input"
    # Direct D3D11 frames otherwise make MPV silently bypass software lavfi
    # filters. Its native converter negotiates hardware download or software
    # input automatically. Keep full chroma and precision, including 10-bit
    # sources, before running the same framing graph as export.
    INPUT_FILTER = f"@{INPUT_LABEL}:format=fmt=yuv444p16:convert=yes"
    REMOVE = f"@{LABEL},@{INPUT_LABEL}"

    def __init__(self):
        self._states = {}

    def apply(self, controller, transform, width, height, *, bypass=False):
        graph = None if bypass or transform_is_identity(transform) else framing_filter(transform, width, height)
        previous = self._states.get(id(controller))
        if graph is None:
            if previous is None:
                return False
            controller.command(["vf", "remove", self.REMOVE])
            self._states.pop(id(controller), None)
        else:
            if previous is not None and previous[1] == graph:
                return False
            controller.command(["vf", "add", f"{self.INPUT_FILTER},@{self.LABEL}:lavfi=[{graph}]"])
            self._states[id(controller)] = (controller, graph)
        return True

    def forget(self, controller):
        self._states.pop(id(controller), None)

    def close(self):
        self._states.clear()
