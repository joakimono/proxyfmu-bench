import os

from conan import ConanFile
from conan.tools.cmake import CMake, CMakeDeps, CMakeToolchain, cmake_layout
from conan.tools.files import copy


class ProxyfmuBenchConan(ConanFile):
    name = "proxyfmu-bench"
    settings = "os", "compiler", "build_type", "arch"
    default_options = {
        "libcosim/*:proxyfmu": True,
        "*:shared": True,
    }

    def requirements(self):
        # The libcosim and proxyfmu branches under test, created locally with
        # `--user local --channel bench` (see the README).
        self.requires("libcosim/[>=0.11]@local/bench")

    def layout(self):
        cmake_layout(self)

    def generate(self):
        CMakeToolchain(self).generate()
        CMakeDeps(self).generate()

        # Put all shared libraries and the proxyfmu executable next to the
        # benchmark executable, so it runs without any environment setup.
        bindir = os.path.join(self.build_folder, "bin")
        for dep in self.dependencies.host.values():
            for d in dep.cpp_info.bindirs:
                copy(self, "*.dll", d, bindir, keep_path=False)
                copy(self, "proxyfmu", d, bindir, keep_path=False)
                copy(self, "proxyfmu.exe", d, bindir, keep_path=False)
            for d in dep.cpp_info.libdirs:
                copy(self, "*.so*", d, bindir, keep_path=False)
                copy(self, "*.dylib", d, bindir, keep_path=False)

    def build(self):
        cmake = CMake(self)
        cmake.configure()
        cmake.build()
