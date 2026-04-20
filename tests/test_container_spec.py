"""Unit tests for ContainerSpec and devcontainer-preset generation."""

import pytest

from claude_rts import container_spec as cs


def test_container_spec_auto_generates_name():
    spec = cs.ContainerSpec(image="ubuntu:24.04")
    assert spec.name
    assert spec.name.startswith("cc-")


def test_container_spec_stamps_canvas_claude_label():
    """ABSOLUTE invariant: every created container carries created_by=canvas-claude."""
    spec = cs.ContainerSpec(image="ubuntu:24.04", name="foo")
    assert spec.labels["created_by"] == "canvas-claude"
    assert spec.labels["supreme-claudemander.managed"] == "true"


def test_devcontainer_preset_uses_named_volume_by_default():
    spec = cs.ContainerSpec(image="ubuntu:24.04", name="foo")
    dc = spec.devcontainer_preset()
    assert dc["image"] == "ubuntu:24.04"
    # Default mount uses a named volume (not a bind mount) → devcontainer-in-devcontainer safe.
    assert any("type=volume" in m and "source=foo-workspace" in m for m in dc["mounts"])
    # Every label flows through to runArgs as `--label k=v`.
    args = dc["runArgs"]
    label_pairs = [args[i + 1] for i, v in enumerate(args) if v == "--label"]
    assert "created_by=canvas-claude" in label_pairs


def test_devcontainer_preset_respects_custom_mounts():
    spec = cs.ContainerSpec(
        image="ubuntu:24.04",
        name="foo",
        mounts=["source=my-vol,target=/data,type=volume"],
    )
    dc = spec.devcontainer_preset()
    assert dc["mounts"] == ["source=my-vol,target=/data,type=volume"]


@pytest.mark.asyncio
async def test_create_invokes_devcontainer_up_async(monkeypatch):
    """STRONG invariant: creation uses asyncio.create_subprocess_exec only."""
    recorded = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"", b"")

    async def fake_exec(*argv, **kw):
        recorded["argv"] = list(argv)
        return FakeProc()

    monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", fake_exec)

    spec = cs.ContainerSpec(image="ubuntu:24.04", name="creation-test")
    result = await cs.create(spec)
    assert result["name"] == "creation-test"
    assert "--override-config" in recorded["argv"]
    assert "--id-label" in recorded["argv"]
    # id-label value is immediately after the flag.
    idx = recorded["argv"].index("--id-label")
    assert recorded["argv"][idx + 1] == "supreme-claudemander.container=creation-test"


@pytest.mark.asyncio
async def test_create_raises_on_nonzero_return(monkeypatch):
    class FakeProc:
        returncode = 2

        async def communicate(self):
            return (b"", b"boom: permission denied")

    async def fake_exec(*argv, **kw):
        return FakeProc()

    monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", fake_exec)
    spec = cs.ContainerSpec(image="ubuntu:24.04", name="fail-test")
    with pytest.raises(RuntimeError, match="permission denied"):
        await cs.create(spec)


# ── Resource caps (#204) ────────────────────────────────────────────


def test_container_spec_has_default_resource_caps():
    """#204 STRONG invariant: every spec carries v1 resource caps by default."""
    spec = cs.ContainerSpec(image="ubuntu:24.04", name="foo")
    assert spec.cpu_limit == 2.0
    assert spec.memory_limit == "8g"
    assert spec.disk_limit == "10g"
    assert spec.pids_limit == 1024


def test_devcontainer_preset_emits_resource_cap_runargs():
    """#204 acceptance: --cpus / --memory / --pids-limit present in runArgs."""
    spec = cs.ContainerSpec(image="ubuntu:24.04", name="foo")
    dc = spec.devcontainer_preset()
    assert "--cpus=2.0" in dc["runArgs"]
    assert "--memory=8g" in dc["runArgs"]
    assert "--pids-limit=1024" in dc["runArgs"]


def test_devcontainer_preset_respects_custom_resource_caps():
    """Overrides flow through to Docker flags verbatim."""
    spec = cs.ContainerSpec(
        image="ubuntu:24.04",
        name="foo",
        cpu_limit=4.0,
        memory_limit="16g",
        pids_limit=2048,
    )
    dc = spec.devcontainer_preset()
    assert "--cpus=4.0" in dc["runArgs"]
    assert "--memory=16g" in dc["runArgs"]
    assert "--pids-limit=2048" in dc["runArgs"]


def test_resource_caps_preserved_alongside_labels():
    """Labels and resource caps coexist in runArgs without collision."""
    spec = cs.ContainerSpec(image="ubuntu:24.04", name="foo")
    args = spec.devcontainer_preset()["runArgs"]
    label_pairs = [args[i + 1] for i, v in enumerate(args) if v == "--label"]
    assert "created_by=canvas-claude" in label_pairs
    assert any(a.startswith("--cpus=") for a in args)
    assert any(a.startswith("--memory=") for a in args)


def test_create_does_not_shell_out_synchronously():
    """Regression guard: ensure container_spec does not import subprocess.run."""
    import inspect

    src = inspect.getsource(cs)
    # The module must not call subprocess.run / subprocess.check_call (would block loop).
    assert "subprocess.run(" not in src
    assert "subprocess.check_call(" not in src
    assert "subprocess.check_output(" not in src
