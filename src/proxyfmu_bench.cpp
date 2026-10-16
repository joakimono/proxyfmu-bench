// Measures the per-step cost of a co-simulation with in-process or proxyfmu slaves.
//
// Usage: proxyfmu_bench <OspSystemStructure.xml> <steps> [--observer]
//
// Runs one step to initialise, then times <steps> more and prints a line
//   RESULT config=... observer=... steps=... total_ms=... us_per_step=... [checksum=...]
// With --observer, a last_value_observer is attached and the checksum (sum of
// all real variables after the last step) is printed, so that results can be
// compared between modes.
//
// Besides `file` and `proxyfmu` URIs, model sources may use the scheme
// `percallproxyfmu://`, which resolves like `proxyfmu://` but wraps each slave
// so that only the per-type get/set functions are used. This reproduces the
// call pattern before batching (one round trip per type and per step) with
// the same binaries.
#include <cosim/algorithm/fixed_step_algorithm.hpp>
#include <cosim/execution.hpp>
#include <cosim/log/simple.hpp>
#include <cosim/observer/last_value_observer.hpp>
#include <cosim/orchestration.hpp>
#include <cosim/osp_config_parser.hpp>

#include <chrono>
#include <cstring>
#include <iomanip>
#include <iostream>

namespace
{

// Forwards everything to `inner_`, but does not override the batched
// `get_variables()`/`set_variables()`/`set_variables_do_step_get_variables()`,
// so their default per-type implementations are used.
class per_call_slave : public cosim::slave
{
public:
    explicit per_call_slave(std::shared_ptr<cosim::slave> inner)
        : inner_(std::move(inner))
    { }

    cosim::model_description model_description() const override { return inner_->model_description(); }
    void setup(cosim::time_point s, std::optional<cosim::time_point> e, std::optional<double> t) override { inner_->setup(s, e, t); }
    void start_simulation() override { inner_->start_simulation(); }
    void end_simulation() override { inner_->end_simulation(); }
    cosim::step_result do_step(cosim::time_point t, cosim::duration dt) override { return inner_->do_step(t, dt); }
    void get_real_variables(gsl::span<const cosim::value_reference> v, gsl::span<double> x) const override { inner_->get_real_variables(v, x); }
    void get_integer_variables(gsl::span<const cosim::value_reference> v, gsl::span<int> x) const override { inner_->get_integer_variables(v, x); }
    void get_boolean_variables(gsl::span<const cosim::value_reference> v, gsl::span<bool> x) const override { inner_->get_boolean_variables(v, x); }
    void get_string_variables(gsl::span<const cosim::value_reference> v, gsl::span<std::string> x) const override { inner_->get_string_variables(v, x); }
    void set_real_variables(gsl::span<const cosim::value_reference> v, gsl::span<const double> x) override { inner_->set_real_variables(v, x); }
    void set_integer_variables(gsl::span<const cosim::value_reference> v, gsl::span<const int> x) override { inner_->set_integer_variables(v, x); }
    void set_boolean_variables(gsl::span<const cosim::value_reference> v, gsl::span<const bool> x) override { inner_->set_boolean_variables(v, x); }
    void set_string_variables(gsl::span<const cosim::value_reference> v, gsl::span<const std::string> x) override { inner_->set_string_variables(v, x); }
    state_index save_state() override { return inner_->save_state(); }
    void save_state(state_index i) override { inner_->save_state(i); }
    void restore_state(state_index i) override { inner_->restore_state(i); }
    void release_state(state_index i) override { inner_->release_state(i); }
    cosim::serialization::node export_state(state_index i) const override { return inner_->export_state(i); }
    state_index import_state(const cosim::serialization::node& n) override { return inner_->import_state(n); }

private:
    std::shared_ptr<cosim::slave> inner_;
};

class per_call_model : public cosim::model
{
public:
    explicit per_call_model(std::shared_ptr<cosim::model> inner)
        : inner_(std::move(inner))
    { }
    std::shared_ptr<const cosim::model_description> description() const noexcept override { return inner_->description(); }
    std::shared_ptr<cosim::slave> instantiate(std::string_view name) override
    {
        return std::make_shared<per_call_slave>(inner_->instantiate(name));
    }

private:
    std::shared_ptr<cosim::model> inner_;
};

class per_call_proxy_resolver : public cosim::model_uri_sub_resolver
{
public:
    explicit per_call_proxy_resolver(std::shared_ptr<cosim::model_uri_resolver> inner)
        : inner_(std::move(inner))
    { }

    std::shared_ptr<cosim::model> lookup_model(const cosim::uri& baseUri, const cosim::uri& ref) override
    {
        if (ref.scheme() != "percallproxyfmu") return nullptr;
        const auto proxyUri = cosim::uri("proxyfmu", ref.authority(), ref.path(), ref.query(), ref.fragment());
        return std::make_shared<per_call_model>(inner_->lookup_model(baseUri, proxyUri));
    }

    std::shared_ptr<cosim::model> lookup_model(const cosim::uri&) override { return nullptr; }

private:
    std::shared_ptr<cosim::model_uri_resolver> inner_;
};

} // namespace

int main(int argc, char** argv)
{
    if (argc < 3) {
        std::cerr << "Usage: " << argv[0] << " <OspSystemStructure.xml> <steps> [--observer]\n";
        return 1;
    }
    const auto configPath = cosim::filesystem::path(argv[1]);
    const auto steps = std::stol(argv[2]);
    const bool observer = argc > 3 && std::strcmp(argv[3], "--observer") == 0;

    cosim::log::setup_simple_console_logging();
    cosim::log::set_global_output_level(cosim::log::warning);

    try {
        auto resolver = cosim::default_model_uri_resolver();
        resolver->add_sub_resolver(std::make_shared<per_call_proxy_resolver>(cosim::default_model_uri_resolver()));

        const auto config = cosim::load_osp_config(configPath, *resolver);
        const auto& params = std::get<cosim::fixed_step_algorithm_params>(config.algorithm_configuration);
        auto execution = cosim::execution(config.start_time, std::make_shared<cosim::fixed_step_algorithm>(params));
        const auto entityMaps = cosim::inject_system_structure(execution, config.system_structure, config.initial_values);
        auto lastValues = std::make_shared<cosim::last_value_observer>();
        if (observer) execution.add_observer(lastValues);

        // Initialisation (and the first step) is excluded from the measurement.
        execution.step();

        const auto t0 = std::chrono::steady_clock::now();
        for (long i = 0; i < steps; ++i) {
            execution.step();
        }
        const auto t1 = std::chrono::steady_clock::now();

        const auto us = std::chrono::duration<double, std::micro>(t1 - t0).count();
        std::cout << "RESULT config=" << configPath.filename().string()
                  << " observer=" << (observer ? "yes" : "no")
                  << " steps=" << steps
                  << " total_ms=" << us / 1000.0
                  << " us_per_step=" << us / steps;
        if (observer) {
            // Sum of all real variable values, to check that all modes compute the same thing.
            double checksum = 0.0;
            for (const auto& e : config.system_structure.entities()) {
                const auto model = std::get_if<std::shared_ptr<cosim::model>>(&e.type);
                if (!model) continue;
                for (const auto& vd : (*model)->description()->variables) {
                    if (vd.type != cosim::variable_type::real) continue;
                    double value = 0.0;
                    lastValues->get_real(entityMaps.simulators.at(e.name), gsl::make_span(&vd.reference, 1), gsl::make_span(&value, 1));
                    checksum += value;
                }
            }
            std::cout << " checksum=" << std::setprecision(17) << checksum;
        }
        std::cout << std::endl;
    } catch (const std::exception& e) {
        std::cerr << "Error: " << e.what() << std::endl;
        return 1;
    }
    return 0;
}
