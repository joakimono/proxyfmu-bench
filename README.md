# proxyfmu-bench

Measures the per-step cost of [libcosim](https://github.com/open-simulation-platform/libcosim) co-simulations with FMUs loaded in-process compared with FMUs running in separate processes through [proxyfmu](https://github.com/open-simulation-platform/proxy-fmu). It was written to evaluate batching of proxyfmu calls, which reduces the round trips per slave and step from up to nine to one (see [libcosim#722](https://github.com/open-simulation-platform/libcosim/issues/722)).

## What it measures

The benchmark runs these systems from libcosim's test data with the fixed-step algorithm:

- **Crane:** `tests/data/msmi`, 4 slaves (FMI 1.0 and 2.0).
- **Quarter truck:** `tests/data/fmi2/quarter_truck`, 2 slaves, converted from ECCO to fixed-step.
- **Quarter truck ×N:** N independent copies of the quarter truck (2N slaves), to see how the cost scales.

Each system runs in three modes, all with the same binaries:

| Mode | Model URIs | Calls per slave and step |
|---|---|---|
| In-process | `file` | none (direct FMI calls) |
| Per-call | `percallproxyfmu://localhost?file=…` | One per non-empty variable type for setting, one for the step, and one per type for getting. This is the call pattern before batching. |
| Batched | `proxyfmu://localhost?file=…` | One |

The per-call mode wraps each proxy slave in a forwarder that only implements libcosim's per-type `get_<type>_variables()`/`set_<type>_variables()` functions. The batched code path is never used, so the RPC sequence is the same as in libcosim before batching.

Each run initialises the system and performs one step. It then times the given number of further steps and reports µs per base step. With `last_value_observer` attached, which reads every variable every step, it also reports a checksum (the sum of all real variables). The per-call and batched checksums should be identical.

### Transport

Locally spawned proxyfmu instances use a Unix domain socket on Linux and macOS, and TCP on Windows. This applies to both proxy modes, so:

- **On Windows**, the per-call column corresponds to libcosim and proxyfmu before the changes under test.
- **On Linux and macOS**, both proxy columns benefit from Unix sockets. The per-call column is then *not* the old behaviour: before these changes, TCP was used everywhere.

## Building

**Requirements:**
- Conan 2
- CMake ≥ 3.19
- Python ≥ 3.8
- Git
- A C++17 compiler (Visual Studio 2022 on Windows)

**Get the source code.** The branches below are the ones under test. Use paths without spaces, because the proxyfmu URI handling doesn't support them.

```sh
git clone -b unix-domain-sockets https://github.com/open-simulation-platform/proxy-fmu.git
git clone -b batched-step-exchange https://github.com/open-simulation-platform/libcosim.git
git clone https://github.com/joakimono/proxyfmu-bench.git
```

**Create local Conan packages** for both, under the `local/bench` user and channel. The libcosim branch requires `proxyfmu/0.5.0@local/bench`.

```sh
conan create proxy-fmu --user local --channel bench --build=missing
conan create libcosim --user local --channel bench -o "libcosim/*:proxyfmu=True" --build=missing
```

**Build the benchmark.** This also copies all shared libraries and the `proxyfmu` executable into the benchmark's `bin` directory.

```sh
cd proxyfmu-bench
conan build . --build=missing
```

On Windows, deep Conan cache paths can exceed `MAX_PATH` while building the dependencies. If that happens, enable long paths in Windows, or set a short cache path with the `CONAN_HOME` environment variable (for example `C:\c2`).

## Running

Check first that everything works. This takes a minute or two:

```sh
python run_benchmark.py --libcosim-dir ../libcosim --quick
```

Then run the full matrix. It takes roughly 10–15 times as long as `--quick`: about 8 minutes on a 24-core Linux machine, and probably 10–20 minutes on Windows, where proxy processes start more slowly and TCP is used:

```sh
python run_benchmark.py --libcosim-dir ../libcosim
```

The results are printed as Markdown tables and saved to `results/<timestamp>.md`, with the raw data for every run in `results/<timestamp>.json`. The exit code is non-zero if any run failed or the per-call and batched checksums differ. Run `python run_benchmark.py --help` for the options (steps, repetitions, scaling sizes, timeout).

For stable numbers:
- Use a power plan or governor set to maximum performance.
- Keep the machine on mains power.
- Close other heavy programs.
- Run the full matrix rather than comparing single runs; the spread between runs can be large, especially for the larger systems.

## Troubleshooting

- **Proxy runs fail or hang at startup on Windows.** This may be [libcosim#739](https://github.com/open-simulation-platform/libcosim/issues/739) ("proxyfmu URI with `localhost` does not work on Windows"). Look for the cause before trusting any numbers. One unverified guess: the client connects to `localhost`, which can resolve to IPv6 `::1`, while the server listens on IPv4.
- **`Unable to bind to external proxy process`.** The `proxyfmu` executable did not start, or did not open its socket. Check that `proxyfmu` (or `proxyfmu.exe`) is in the `bin` directory next to `proxyfmu_bench`, and that the temporary directory is writable.
- **CMake cannot find target `libcbor::libcbor`.** libcosim's package config file does not find libcbor itself. `CMakeLists.txt` works around this by calling `find_package(libcbor)` first.
- **Absolute paths in proxyfmu URIs.** The generated configurations use paths relative to the configuration file, because `proxyfmu://localhost?file=file:///…` is broken on Linux in libcosim 0.11.
