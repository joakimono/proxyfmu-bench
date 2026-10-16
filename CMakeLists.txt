cmake_minimum_required(VERSION 3.19)
project(proxyfmu_bench CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# libcosim's installed package config does not find libcbor itself, so it has
# to be found first. See the README.
find_package(libcbor REQUIRED)
find_package(libcosim REQUIRED)

add_executable(proxyfmu_bench "src/proxyfmu_bench.cpp")
target_link_libraries(proxyfmu_bench PRIVATE libcosim::cosim)

# Put the executable in <build>/bin for all generators (the generator
# expression stops multi-config generators from adding a per-config
# subdirectory). The Conan recipe copies the runtime dependencies and the
# proxyfmu executable there.
set_target_properties(proxyfmu_bench PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "$<1:${CMAKE_BINARY_DIR}/bin>")
