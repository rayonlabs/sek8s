"""Tests for `image manifest` — the generated set must be directly consumable.

The build writes each image set into its own directory (qcow2 + direct-boot sidecars +
manifest). The manifest readers (`verify`, `download`, `guest launch`) only ever look for
`manifest.json`, so generating one must default to exactly that name beside the qcow2 —
otherwise a freshly built set can't be copied to /var/lib/chutes/base-images/<variant>/ as-is.
"""

import json

from chutes_cvm.guest import image_set


def _make_set(tmp_path, name="1.4.0-debug"):
    """Write a complete four-artifact image set into ``tmp_path``; return the qcow2 path."""
    qcow2 = tmp_path / f"{name}.qcow2"
    qcow2.write_bytes(b"qcow2-bytes")
    for role in ("vmlinuz", "initrd", "cmdline"):
        (tmp_path / f"{name}.{role}").write_bytes(role.encode())
    return qcow2


def test_manifest_defaults_to_manifest_json_beside_the_qcow2(tmp_path):
    qcow2 = _make_set(tmp_path)

    assert (
        image_set.main(["manifest", str(qcow2), "--version", "1.4.0", "--debug"]) == 0
    )

    manifest = tmp_path / "manifest.json"
    assert manifest.exists()
    assert not (tmp_path / "1.4.0-debug.manifest.json").exists()
    data = json.loads(manifest.read_text())
    assert data["version"] == "1.4.0"
    assert data["debug"] is True
    assert set(data["artifacts"]) == set(image_set.ROLES)


def test_generated_set_verifies_in_place(tmp_path):
    """The set the build produces passes `verify --full` without any renaming."""
    qcow2 = _make_set(tmp_path, name="1.4.0")

    assert image_set.main(["manifest", str(qcow2), "--version", "1.4.0"]) == 0

    assert image_set.resolve(str(tmp_path), full=True)[0] == str(qcow2)


def test_manifest_output_override_still_honored(tmp_path):
    qcow2 = _make_set(tmp_path)
    out = tmp_path / "custom.manifest.json"

    assert image_set.main(["manifest", str(qcow2), "-o", str(out)]) == 0

    assert out.exists()
    assert not (tmp_path / "manifest.json").exists()
