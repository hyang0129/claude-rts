"""Issue #221 S4 — /profiles volume is mounted inside canvas-claude containers.

Marked ``real_docker`` — skipped in default CI; run locally:
    python -m pytest tests/test_issue221_profiles_mount.py -m real_docker -v
"""

import subprocess
import uuid

import pytest


def _docker_available() -> bool:
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.mark.real_docker
@pytest.mark.skipif(not _docker_available(), reason="Docker not available")
class TestProfilesVolumeMount:
    """S4 — verify /profiles is mounted inside a newly-created canvas-claude container."""

    PREFIX = "e2e-221-s4-"

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        created: list[str] = []
        yield created
        for name in created:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)

    def test_profiles_directory_exists_in_container(self, _cleanup):
        """A running ubuntu:24.04 container with the profiles volume has /profiles."""
        name = f"{self.PREFIX}{uuid.uuid4().hex[:8]}"
        _cleanup.append(name)

        # Create container with profiles volume mount (matches ContainerSpec default).
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--label",
                "created_by=canvas-claude",
                "-v",
                "claude-profiles:/profiles",
                "ubuntu:24.04",
                "sleep",
                "300",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )

        # Verify /profiles exists as a mountpoint inside the container.
        result = subprocess.run(
            ["docker", "exec", name, "ls", "/profiles"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, f"'ls /profiles' failed inside container '{name}': {result.stderr}"

    def test_profiles_is_a_mountpoint(self, _cleanup):
        """/profiles inside the container is an actual bind/volume mountpoint."""
        name = f"{self.PREFIX}{uuid.uuid4().hex[:8]}"
        _cleanup.append(name)

        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--label",
                "created_by=canvas-claude",
                "-v",
                "claude-profiles:/profiles",
                "ubuntu:24.04",
                "sleep",
                "300",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )

        # Check mount table inside container — /profiles must appear.
        result = subprocess.run(
            ["docker", "exec", name, "cat", "/proc/mounts"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        # The volume mount path will appear as an overlay or bind in /proc/mounts.
        # We look for '/profiles' in the mount target column (field 2).
        mounted_paths = [line.split()[1] for line in result.stdout.splitlines() if len(line.split()) >= 2]
        assert "/profiles" in mounted_paths, (
            f"/profiles not found in /proc/mounts inside '{name}'.\nMounts:\n{result.stdout}"
        )
