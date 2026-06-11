"""Tests for the package dimension agents (W3). The LLM is mocked at
the Agent.run_sync boundary; what's pinned: batching, caching,
vocabulary/hallucination filtering, and the load-bearing policy."""

from __future__ import annotations

from datetime import UTC, datetime

from autokernel.world import agent_world as aw
from autokernel.world.models import (
    BaseRelease,
    Dimension,
    GlobalFlags,
    PackageDecision,
    Ring,
    WorldEntry,
    WorldManifest,
)


def _manifest(sources: list[str]) -> WorldManifest:
    return WorldManifest(
        created_at=datetime.now(UTC),
        host="testhost",
        base=BaseRelease(
            distro_id="ubuntu", suite="resolute", mirror="http://x", components=["main"]
        ),
        ring=Ring.REQUIRED,
        flags=GlobalFlags(),
        world=[
            WorldEntry(
                binary=f"{s}-bin",
                source=s,
                source_version="1",
                priority="required",
                installed_kb=100,
            )
            for s in sources
        ],
    )


class _FakeResult:
    def __init__(self, output):
        self.output = output


def _fake_agent(batch_factory, calls):
    class _A:
        def run_sync(self, prompt):
            calls.append(prompt)
            return _FakeResult(batch_factory(prompt))

    return _A()


def test_decide_dimension_filters_and_caches(tmp_path, monkeypatch):
    calls: list[str] = []

    def factory(prompt):
        return aw._DecisionBatch(
            decisions=[
                aw._DecisionDraft(
                    source="zlib", decision="keep", reason="core", confidence=0.9
                ),
                aw._DecisionDraft(  # hallucinated package → dropped
                    source="ghost", decision="keep", reason="?", confidence=0.9
                ),
                aw._DecisionDraft(  # invalid vocabulary → dropped
                    source="foo", decision="yolo", reason="?", confidence=0.9
                ),
                aw._DecisionDraft(
                    source="foo", decision="trim", reason="redundant", confidence=0.8
                ),
            ]
        )

    monkeypatch.setattr(aw, "_get_agent", lambda m, d: _fake_agent(factory, calls))
    m = _manifest(["zlib", "foo"])

    first = aw.decide_dimension(m, Dimension.NECESSITY, tmp_path)
    assert {(d.source, d.decision) for d in first} == {
        ("zlib", "keep"),
        ("foo", "trim"),
    }
    assert len(calls) == 1

    # Second run: cache hit, no new LLM call, same decisions.
    second = aw.decide_dimension(m, Dimension.NECESSITY, tmp_path)
    assert len(calls) == 1
    assert second == first


def test_decide_dimension_batches(tmp_path, monkeypatch):
    calls: list[str] = []
    factory = lambda prompt: aw._DecisionBatch()  # noqa: E731
    monkeypatch.setattr(aw, "_get_agent", lambda m, d: _fake_agent(factory, calls))
    m = _manifest([f"pkg{i:02d}" for i in range(10)])
    aw.decide_dimension(m, Dimension.RISK, tmp_path, batch_size=4)
    assert len(calls) == 3  # 10 units / 4 per batch


def test_flags_dimension_carries_params(tmp_path, monkeypatch):
    def factory(prompt):
        return aw._DecisionBatch(
            decisions=[
                aw._DecisionDraft(
                    source="zlib",
                    decision="override",
                    reason="lto hostile",
                    confidence=0.7,
                    strip=["-flto=auto"],
                    add=["-O2"],
                )
            ]
        )

    monkeypatch.setattr(aw, "_get_agent", lambda m, d: _fake_agent(factory, []))
    out = aw.decide_dimension(_manifest(["zlib"]), Dimension.FLAGS, tmp_path)
    assert out[0].params == {"strip": ["-flto=auto"], "add": ["-O2"]}


def test_load_bearing_policy_forces_keep():
    decisions = [
        PackageDecision(
            source="systemd",
            dimension=Dimension.NECESSITY,
            decision="trim",
            reason="big",
            confidence=0.99,
        ),
        PackageDecision(
            source="fortune-mod",
            dimension=Dimension.NECESSITY,
            decision="trim",
            reason="toy",
            confidence=0.9,
        ),
        PackageDecision(
            source="systemd",
            dimension=Dimension.RISK,
            decision="boot-critical",
            reason="init",
            confidence=0.99,
        ),
    ]
    out = aw.apply_package_policy(decisions)
    by = {(d.source, d.dimension): d for d in out}
    assert by[("systemd", Dimension.NECESSITY)].decision == "keep"
    assert "load-bearing" in by[("systemd", Dimension.NECESSITY)].reason
    assert by[("fortune-mod", Dimension.NECESSITY)].decision == "trim"
    assert by[("systemd", Dimension.RISK)].decision == "boot-critical"  # untouched
