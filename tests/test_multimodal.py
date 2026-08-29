"""Voice intake and the spoken briefing: content and fallback behaviour, no network."""

from attest_fleet import briefing


def _m(**kw):
    base = {"runs": 42, "silent_failures": 1, "silent_failure_rate": 0.0435,
            "by_status": {"verified": 21}}
    base.update(kw)
    return base


def test_briefing_leads_with_what_needs_a_decision():
    """Audio is linear and unskimmable, so a listener must hear the actionable thing first."""
    script = briefing.compose(_m(), pending=1, kill_switch=False,
                              worst={"subject": "Cancel subscription"})
    assert script.index("waiting for your approval") < script.index("silent failure")
    assert script.index("silent failure") < script.index("Across 42 runs")


def test_briefing_names_the_run_worth_looking_at():
    script = briefing.compose(_m(), pending=0, kill_switch=False,
                              worst={"subject": "Cancel subscription"})
    assert "Cancel subscription" in script


def test_briefing_says_so_when_nothing_is_wrong():
    script = briefing.compose(_m(silent_failures=0, silent_failure_rate=0.0),
                              pending=0, kill_switch=False)
    assert "No silent failures" in script


def test_kill_switch_is_the_first_thing_spoken():
    """If the fleet is halted, every other number is secondary to knowing that."""
    script = briefing.compose(_m(), pending=2, kill_switch=True)
    assert script.startswith("Heads up.") and "kill switch is engaged" in script


def test_briefing_only_states_measured_numbers():
    """Speech is a second channel onto verified state, never a second source of truth."""
    script = briefing.compose(_m(runs=42, silent_failures=1, silent_failure_rate=0.0435,
                                 by_status={"verified": 21}), pending=1, kill_switch=False)
    assert "42 runs" in script and "21 verified" in script and "4.3 percent" in script


def test_pcm_is_wrapped_as_playable_wav():
    import io, wave
    data = briefing._wav(b"\x00\x01" * 2400)
    with wave.open(io.BytesIO(data)) as w:
        assert w.getnchannels() == 1 and w.getframerate() == 24000 and w.getnframes() == 2400


def test_tts_and_gemma_stay_off_vertex():
    """Vertex serves neither Gemma nor the AUDIO response modality."""
    from attest_fleet import config
    assert config.on_vertex("gemini-2.5-flash-preview-tts") is False
    assert config.on_vertex("gemma-4-31b-it") is False


def test_inline_attachments_are_not_persisted_into_the_run_record():
    """Firestore caps a document at 1 MiB and a call recording clears that on its own. The
    bytes are input; what belongs in the record is what the reader extracted from them."""
    from attest_fleet.fleet import _persistable
    from attest_fleet.domain import Ticket
    big = "data:audio/wav;base64," + "A" * 900_000
    t = Ticket(customer_ref="c", subject="s", body="b", audio_url=big)
    kept = _persistable(t)
    assert kept.audio_url != big
    assert "stored=false" in kept.audio_url and len(kept.audio_url) < 200
    # a short URL is left alone
    small = Ticket(customer_ref="c", subject="s", body="b", image_url="https://x/y.png")
    assert _persistable(small).image_url == "https://x/y.png"
