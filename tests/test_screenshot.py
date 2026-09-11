"""Failure screenshot: graceful when deps are missing, correct when present."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

import smithcore_agent.screenshot as shot


def test_non_windows_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert shot.capture_screenshot() is None


def test_missing_deps_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in {"mss", "PIL"}:
            raise ImportError(f"no {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert shot.capture_screenshot() is None


def _install_fake_deps(monkeypatch: pytest.MonkeyPatch, big: bool = False) -> None:
    """Fake mss + PIL so the happy path runs without the real libs."""
    monkeypatch.setattr(sys, "platform", "win32")

    payload = b"\x89PNG" + b"x" * (shot.MAX_SCREENSHOT_BYTES + 10 if big else 10)

    class FakeRaw:
        size = (4, 2)
        rgb = bytes(24)

    class FakeMss:
        def __enter__(self) -> FakeMss:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def grab(self, monitor: Any) -> FakeRaw:
            return FakeRaw()

        monitors: list[Any] = [{}, {"left": 0, "top": 0, "width": 4, "height": 2}]

    class FakeImage:
        small = False
        width = 4
        height = 2

        def save(self, buf: Any, **kwargs: Any) -> None:
            buf.write(b"\x89PNG" + b"x" * 10 if self.small else payload)

        def resize(self, size: tuple[int, int]) -> FakeImage:
            shrunk = FakeImage()
            shrunk.small = True
            shrunk.width, shrunk.height = size
            return shrunk

    mss_mod = types.ModuleType("mss")
    mss_mod.mss = lambda: FakeMss()
    pil_mod = types.ModuleType("PIL")
    image_mod = types.ModuleType("PIL.Image")
    image_mod.Image = FakeImage
    image_mod.frombytes = lambda *args, **kwargs: FakeImage()
    pil_mod.Image = image_mod
    monkeypatch.setitem(sys.modules, "mss", mss_mod)
    monkeypatch.setitem(sys.modules, "PIL", pil_mod)
    monkeypatch.setitem(sys.modules, "PIL.Image", image_mod)


def test_capture_returns_png(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_deps(monkeypatch)
    result = shot.capture_screenshot()
    assert result is not None
    data, filename = result
    assert data.startswith(b"\x89PNG")
    assert filename.startswith("failure-")
    assert filename.endswith(".png")


def test_oversized_capture_is_downscaled(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_deps(monkeypatch, big=True)
    result = shot.capture_screenshot()
    assert result is not None
    data, _ = result
    assert len(data) <= shot.MAX_SCREENSHOT_BYTES
