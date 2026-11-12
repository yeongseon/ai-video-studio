"""constraints.txt pins exactly what the deployable runtime and CI install (#999).

`constraints.in` declares the inputs. `make lock-constraints` regenerates the file from them with
uv, and `make lock-constraints-check` (run by the lint job) proves that the committed file is what
that regeneration produces. These tests are the offline half: they read only tracked files, so a
stray or hand-edited pin, or an input that no consumer uses, fails before the network-based check
runs.
"""

import re
from pathlib import Path
from typing import Final

import yaml

ROOT: Final = Path(__file__).resolve().parents[3]
LOCAL_PACKAGES: Final = {"creator-domain", "creator-provider", "creator-service"}
REQUIREMENTS: Final = (
    "apps/api/requirements.txt",
    "apps/api/requirements-dev.txt",
    "apps/worker-orchestrator/requirements.txt",
    "apps/worker-orchestrator/requirements-dev.txt",
)
PIN: Final = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _read_constraints() -> tuple[list[tuple[str, str, list[str]]], list[str]]:
    pins: list[tuple[str, str, list[str]]] = []
    malformed: list[str] = []
    for line in (ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if line.startswith(" ") and line.strip().startswith("#") and pins:
            entry = line.strip().removeprefix("#").strip().removeprefix("via").strip()
            if entry:
                pins[-1][2].append(entry)
        elif match := PIN.fullmatch(line):
            pins.append((match[1], match[2], []))
        else:
            malformed.append(line)
    return pins, malformed


def test_constraints_are_exact_unique_pins() -> None:
    # Given every line of the committed constraints file.
    pins, malformed = _read_constraints()

    # When looking for anything that is not `name==version`, and for a package pinned twice.
    names = [_normalize(name) for name, _version, _via in pins]
    duplicates = sorted({name for name in names if names.count(name) > 1})

    # Then each package is pinned exactly once, to one exact version.
    assert pins, "constraints.txt must pin something"
    assert not malformed, malformed
    assert not duplicates, duplicates


def test_every_pin_says_why_it_is_there() -> None:
    # Given the pins and the annotation that uv writes under each of them.
    pins, _malformed = _read_constraints()
    pinned = {_normalize(name) for name, _version, _via in pins}

    # When resolving each annotation to a requirements file, a local package or another pin.
    orphans = []
    for name, _version, via in pins:
        if not via:
            orphans.append(f"{name}: no annotation")
        for entry in via:
            if entry.startswith("-r "):
                known = (ROOT / entry.removeprefix("-r ")).is_file()
            else:
                known = _normalize(entry) in pinned | LOCAL_PACKAGES
            if not known:
                orphans.append(f"{name}: via {entry}")

    # Then no pin is a leftover: each is reachable from a declared input.
    assert not orphans, orphans


def test_requirement_pins_agree_with_the_constraints() -> None:
    # Given the exact pins that the requirements files declare themselves.
    pins, _malformed = _read_constraints()
    constrained = {_normalize(name): version for name, version, _via in pins}
    declared = {}
    for path in REQUIREMENTS:
        for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
            if match := PIN.fullmatch(line.strip()):
                declared[_normalize(match[1])] = match[2]

    # When comparing them with the constraints.
    disagree = {
        name: (version, constrained.get(name))
        for name, version in declared.items()
        if constrained.get(name) != version
    }

    # Then a requirement cannot be bumped without its pin, or the other way round.
    assert declared, "the requirements files must declare exact pins"
    assert not disagree, disagree


def _consumers() -> tuple[set[str], set[str], set[str]]:
    texts = []
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        texts.extend(step["run"] for step in job.get("steps", []) if "run" in step)
    texts.append((ROOT / "Makefile").read_text(encoding="utf-8"))
    for dockerfile in sorted((ROOT / "apps").glob("*/Dockerfile")):
        text = dockerfile.read_text(encoding="utf-8")
        copied = re.search(r"COPY\s+(\S+requirements\S*\.txt)\s+\./requirements\.txt", text)
        texts.append(text.replace("-r requirements.txt", f"-r {copied[1]}") if copied else text)

    requirements: set[str] = set()
    packages: set[str] = set()
    names: set[str] = set()
    for text in texts:
        for line in text.replace("\\\n", " ").splitlines():
            if "-c constraints.txt" not in line or "pip install" not in line:
                continue
            tokens = line.split("pip install", 1)[1].split()
            takes_value = ""
            for token in tokens:
                if takes_value:
                    if takes_value == "-r":
                        requirements.add(token)
                    elif takes_value == "-e":
                        packages.add(token.rsplit("/", 1)[-1])
                    takes_value = ""
                elif token in {"-c", "-r", "-e"}:
                    takes_value = token
                elif token.startswith("-"):
                    continue
                elif "packages/" in token:
                    packages.add(token.rsplit("/", 1)[-1])
                else:
                    names.add(_normalize(token))
    return requirements, packages, names


def _inputs() -> tuple[set[str], set[str], set[str]]:
    requirements: set[str] = set()
    packages: set[str] = set()
    names: set[str] = set()
    for line in (ROOT / "constraints.in").read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("-r "):
            requirements.add(entry.removeprefix("-r "))
        elif entry.startswith("./packages/"):
            packages.add(entry.removeprefix("./packages/"))
        else:
            names.add(_normalize(entry))
    return requirements, packages, names


def test_inputs_declare_exactly_what_the_pip_install_consumers_use() -> None:
    # Given every `pip install -c constraints.txt` in CI, the Makefile and the Dockerfiles.
    used_requirements, used_packages, used_names = _consumers()
    pins, _malformed = _read_constraints()
    pinned = {_normalize(name) for name, _version, _via in pins}

    # When comparing them with what constraints.in declares.
    declared_requirements, declared_packages, declared_names = _inputs()

    # Then the file is not missing an input, declares none that nothing uses, and constrains
    # every package that a consumer installs by name.
    assert used_requirements == declared_requirements
    assert used_packages == declared_packages == LOCAL_PACKAGES
    assert declared_names <= used_names
    assert used_names <= pinned, sorted(used_names - pinned)


def test_the_lint_job_checks_that_the_constraints_file_is_reproducible() -> None:
    # Given the lint job of the CI workflow.
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    commands = [step.get("run", "") for step in workflow["jobs"]["lint"]["steps"]]

    # When looking for the reproducibility check.
    checked = any("make lock-constraints-check" in command for command in commands)

    # Then a hand edit that regeneration would not reproduce cannot be merged.
    assert checked
