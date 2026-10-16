#!/usr/bin/env python3
"""Runs the proxyfmu benchmark matrix and prints Markdown result tables.

Generates OspSystemStructure files from libcosim's test data (with paths
relative to the generated files, so they work on any platform), runs
proxyfmu_bench for every combination of system, mode and observer setting,
and writes results/<timestamp>.md and results/<timestamp>.json.

Modes:
  in-process  FMUs loaded directly by libcosim.
  per-call    proxyfmu, with one call per variable type and per step, as
              before batching (percallproxyfmu:// URIs).
  batched     proxyfmu, with one call per step (proxyfmu:// URIs).

See README.md for how to build the benchmark first.
"""

import argparse
import datetime
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODES = ["in-process", "per-call", "batched"]


def source_uri(mode, rel_path):
    if mode == "in-process":
        return rel_path
    scheme = "proxyfmu" if mode == "batched" else "percallproxyfmu"
    return f"{scheme}://localhost?file={rel_path}"


def rel_posix(target, start):
    try:
        rel = os.path.relpath(target, start)
    except ValueError:
        sys.exit(f"Error: {target} and {start} must be on the same drive.")
    return rel.replace(os.sep, "/")


def replace_sources(text, mode, fmu_dir, config_dir):
    def sub(m):
        src = re.sub(r"^proxyfmu://localhost\?file=", "", m.group(1))
        fmu = (fmu_dir / src).resolve()
        if not fmu.exists():
            sys.exit(f"Error: FMU not found: {fmu}")
        return f'source="{source_uri(mode, rel_posix(fmu, config_dir))}"'

    return re.sub(r'source="([^"]+)"', sub, text)


def replicate(text, copies):
    """Makes `copies` copies of every simulator and connection, with an index appended to the names."""
    sims = re.search(r"<Simulators>(.*?)</Simulators>", text, re.S).group(1)
    cons = re.search(r"<Connections>(.*?)</Connections>", text, re.S).group(1)
    new_sims = "".join(re.sub(r'name="([^"]+)"', rf'name="\g<1>{i}"', sims) for i in range(copies))
    new_cons = "".join(re.sub(r'simulator="([^"]+)"', rf'simulator="\g<1>{i}"', cons) for i in range(copies))
    return text.replace(sims, new_sims).replace(cons, new_cons)


def generate_configs(libcosim_dir, work_dir, sizes):
    """Writes the configs to work_dir and returns {system: (simulator count, {mode: path})}."""
    data = libcosim_dir / "tests" / "data"
    if not data.is_dir():
        sys.exit(f"Error: {data} not found. Point --libcosim-dir to a libcosim checkout.")
    if " " in str(data) or " " in str(work_dir):
        sys.exit("Error: paths with spaces are not supported by the proxyfmu URI handling. "
                 "Use a libcosim checkout and work directory without spaces.")
    work_dir.mkdir(parents=True, exist_ok=True)

    msmi = data / "msmi"
    qt_dir = data / "fmi2" / "quarter_truck"
    for f in list(msmi.glob("*_OspModelDescription.xml")) + list(qt_dir.glob("*_OspModelDescription.xml")):
        shutil.copy(f, work_dir)

    crane = (msmi / "OspSystemStructure_proxyfmu.xml").read_text()
    qt = (qt_dir / "OspSystemStructure.xml").read_text()
    qt = qt.replace("<Algorithm>ecco</Algorithm>", "<Algorithm>fixedStep</Algorithm>")
    qt = re.sub(r"<EccoConfiguration>.*?</EccoConfiguration>", "", qt, flags=re.S)
    qt = qt.replace(' powerBond="wheelchassis"', "")

    systems = {"crane": (4, crane, msmi)}
    for n in sizes:
        systems[f"quartertruck_x{n}"] = (2 * n, replicate(qt, n), qt_dir)
        # The OspModelDescription files are looked up by simulator name.
        for i in range(n):
            for name in ("chassis", "wheel"):
                shutil.copy(qt_dir / f"{name}_OspModelDescription.xml", work_dir / f"{name}{i}_OspModelDescription.xml")

    configs = {}
    for system, (count, text, fmu_dir) in systems.items():
        paths = {}
        for mode in MODES:
            path = work_dir / f"{system}_{mode}.xml"
            path.write_text(replace_sources(text, mode, fmu_dir, work_dir))
            paths[mode] = path
        configs[system] = (count, paths)
    return configs


def find_exe(build_dir):
    names = ["proxyfmu_bench.exe", "proxyfmu_bench"]
    for p in sorted(build_dir.rglob("proxyfmu_bench*")):
        if p.name in names and p.is_file():
            return p
    sys.exit(f"Error: proxyfmu_bench not found under {build_dir}. Build it first (see README.md).")


def run_once(exe, config, steps, observer, timeout):
    env = dict(os.environ)
    bindir = str(exe.parent)
    # The Conan recipe puts all shared libraries next to the executable.
    var = "PATH" if os.name == "nt" else "LD_LIBRARY_PATH"
    env[var] = bindir + os.pathsep + env.get(var, "")
    cmd = [str(exe), str(config), str(steps)] + (["--observer"] if observer else [])
    try:
        proc = subprocess.run(cmd, cwd=bindir, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout} s"
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return dict(re.findall(r"(\w+)=(\S+)", line)), None
    tail = "\n".join((proc.stderr + proc.stdout).strip().splitlines()[-5:])
    return None, f"exit code {proc.returncode}: {tail}"


def fmt(x):
    return "–" if x is None else f"{x:.0f}"


def ratio(a, b):
    return "–" if a is None or b is None or b == 0 else f"{a / b:.1f}×"


def main():
    # The tables use non-ASCII characters, which the Windows console may not support by default.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--libcosim-dir", type=Path, required=True, help="libcosim checkout (for tests/data)")
    ap.add_argument("--build-dir", type=Path, default=HERE / "build", help="where to look for proxyfmu_bench")
    ap.add_argument("--work-dir", type=Path, default=HERE / "work", help="where to write generated configs")
    ap.add_argument("--steps", type=int, default=5000, help="steps per run for crane and the single quarter truck")
    ap.add_argument("--reps", type=int, default=5, help="runs per cell for crane and the single quarter truck")
    ap.add_argument("--scaling-sizes", default="4,12,24", help="quarter truck copies for the scaling table (2 slaves each)")
    ap.add_argument("--scaling-steps", type=int, default=2000)
    ap.add_argument("--scaling-reps", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=1800, help="seconds per run")
    ap.add_argument("--quick", action="store_true", help="few steps and one run per cell, to check that everything works")
    args = ap.parse_args()
    if args.quick:
        args.steps, args.reps, args.scaling_steps, args.scaling_reps = 200, 1, 100, 1

    scaling = [int(s) for s in args.scaling_sizes.split(",") if s]
    exe = find_exe(args.build_dir.resolve())
    configs = generate_configs(args.libcosim_dir.resolve(), args.work_dir.resolve(), sorted({1, *scaling}))

    plan = []
    for observer in (False, True):
        for system in ("crane", "quartertruck_x1"):
            plan.append((system, observer, args.steps, args.reps))
    for n in scaling:
        plan.append((f"quartertruck_x{n}", True, args.scaling_steps, args.scaling_reps))

    runs = []
    for system, observer, steps, reps in plan:
        count, paths = configs[system]
        for rep in range(reps):
            for mode in MODES:
                result, error = run_once(exe, paths[mode], steps, observer, args.timeout)
                label = f"{system} observer={'yes' if observer else 'no'} {mode} run {rep + 1}/{reps}"
                if error:
                    print(f"FAILED {label}: {error}", flush=True)
                else:
                    print(f"{label}: {float(result['us_per_step']):.0f} us/step", flush=True)
                runs.append({
                    "system": system, "simulators": count, "observer": observer, "mode": mode,
                    "steps": steps, "rep": rep,
                    "us_per_step": float(result["us_per_step"]) if result else None,
                    "checksum": result.get("checksum") if result else None,
                    "error": error,
                })

    def median(system, observer, mode):
        xs = [r["us_per_step"] for r in runs if r["system"] == system and r["observer"] == observer
              and r["mode"] == mode and r["us_per_step"] is not None]
        return statistics.median(xs) if xs else None

    def checksums(system, mode):
        return {r["checksum"] for r in runs if r["system"] == system and r["mode"] == mode and r["checksum"]}

    env = {
        "date": datetime.datetime.now().isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
    }
    md = ["## proxyfmu benchmark", ""]
    md.append(f"{env['platform']}, {env['cpu_count']} logical CPUs, {env['processor'] or env['machine']}. "
              f"µs per base step, median of {args.reps} runs of {args.steps} steps.")
    md += ["", "| System | Observer | In-process | Per-call | Batched | Speedup | Batched / in-process |",
           "|---|---|---|---|---|---|---|"]
    for system, label in (("crane", "Crane (4 slaves)"), ("quartertruck_x1", "Quarter truck (2 slaves)")):
        for observer in (False, True):
            m = {mode: median(system, observer, mode) for mode in MODES}
            md.append(f"| {label} | {'`last_value_observer`' if observer else 'no'} | {fmt(m['in-process'])} | "
                      f"{fmt(m['per-call'])} | {fmt(m['batched'])} | {ratio(m['per-call'], m['batched'])} | "
                      f"{ratio(m['batched'], m['in-process'])} |")
    if scaling:
        md += ["", f"Scaling: N copies of the quarter truck with `last_value_observer`, "
                   f"median of {args.scaling_reps} runs of {args.scaling_steps} steps.", "",
               "| Proxy slaves | In-process | Per-call | Batched | Speedup | Batched / in-process |",
               "|---|---|---|---|---|---|"]
        for n in scaling:
            system = f"quartertruck_x{n}"
            m = {mode: median(system, True, mode) for mode in MODES}
            md.append(f"| {2 * n} | {fmt(m['in-process'])} | {fmt(m['per-call'])} | {fmt(m['batched'])} | "
                      f"{ratio(m['per-call'], m['batched'])} | {ratio(m['batched'], m['in-process'])} |")

    md += ["", "Checksums (sum of all real variables after the last step, observer runs):", ""]
    all_match = True
    for system in configs:
        per_call, batched, direct = checksums(system, "per-call"), checksums(system, "batched"), checksums(system, "in-process")
        if not (per_call or batched):
            continue
        same = len(per_call | batched) == 1
        all_match &= same
        md.append(f"- {system}: per-call and batched {'identical' if same else 'DIFFER'} "
                  f"({', '.join(sorted(per_call | batched))}); in-process {', '.join(sorted(direct)) or '–'}")
    failures = [r for r in runs if r["error"]]
    if failures:
        md += ["", f"**{len(failures)} run(s) failed**, e.g. {failures[0]['system']} {failures[0]['mode']}: "
                   f"{failures[0]['error'].splitlines()[0]}"]

    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"{stamp}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (out_dir / f"{stamp}.json").write_text(json.dumps({"environment": env, "args": {k: str(v) for k, v in vars(args).items()},
                                                      "runs": runs}, indent=2), encoding="utf-8")
    print()
    print("\n".join(md))
    print(f"\nWritten to {out_dir / stamp}.md and .json")
    return 0 if all_match and not failures else 1


if __name__ == "__main__":
    sys.exit(main())
