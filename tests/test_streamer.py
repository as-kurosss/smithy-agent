"""LogStreamer must emit lowercase wire values (server LogLevel contract)."""

from __future__ import annotations

from smithy_agent.streamer import LogStreamer


def test_parse_structured_line_lowercases_level() -> None:
    entry = LogStreamer._parse_line("[INFO] hello", "stdout")
    assert entry["level"] == "info"
    assert entry["message"] == "hello"
    assert entry["source"] == "stdout"
    assert "timestamp" in entry


def test_parse_plain_line_defaults_to_info() -> None:
    entry = LogStreamer._parse_line("plain output", "stderr")
    assert entry["level"] == "info"
    assert entry["message"] == "plain output"
