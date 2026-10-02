#include <libscuff.h>
#include "RWGPorts.h"
#include <chrono>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>

using namespace scuff;
using Clock = std::chrono::steady_clock;
static double elapsed(Clock::time_point t) {
  return std::chrono::duration<double>(Clock::now() - t).count();
}
static std::ofstream output(const std::string &path) {
  std::ofstream f;
  f.exceptions(std::ios::failbit | std::ios::badbit);
  f.open(path);
  f << std::scientific << std::setprecision(17);
  return f;
}
static void vector_csv(HVector *v, const std::string &path) {
  auto f = output(path);
  for (int i = 0; i < v->N; ++i) {
    cdouble z = v->GetEntry(i);
    f << real(z) << ',' << imag(z) << '\n';
  }
  f.close();
}
int main(int argc, char **argv) {
  try {
    if (argc != 5) throw std::runtime_error("Usage: export_system geometry ports frequency_GHz out_dir");
    double frequency = std::stod(argv[3]);
    if (!std::isfinite(frequency) || frequency <= 0) throw std::runtime_error("Invalid frequency");
    const std::string out = argv[4];
    SetLogFileName((out + "/scuff-helper.log").c_str());
    const auto total_start = Clock::now();
    RWGGeometry::UseHRWGFunctions = false;
    RWGGeometry g(argv[1]);
    if (g.NumSurfaces != 1 || !g.Surfaces[0]->IsPEC || g.LDim != 0)
      throw std::runtime_error("Expected one isolated PEC surface");
    HMatrix *m = g.AllocateBEMMatrix();
    HVector *kn = g.AllocateRHSVector();
    int num_ports = 0;
    RWGPort **ports = ParsePortFile(&g, argv[2], &num_ports);
    if (num_ports != 1 || ports[0]->NumPEdges == 0 || ports[0]->NumMEdges == 0)
      throw std::runtime_error("Expected one differential port");
    // Match scuff-rf's mm/GHz units and its historical c=3e8 m/s.
    const cdouble omega = (2.0 * M_PI / 300.0) * frequency;
    cdouble current[1] = {cdouble(1.0, 0.0)}, voltage[1];
    auto t = Clock::now();
    g.AssembleBEMMatrix(omega, m);
    const double assembly_s = elapsed(t);
    t = Clock::now();
    kn->Zero();
    AddPortContributionsToRHS(&g, ports, num_ports, current, omega, kn);
    const double rhs_s = elapsed(t);
    // Export by logical row/column before the in-place LAPACK operations.
    t = Clock::now();
    auto fr = output(out + "/M_real.csv");
    auto fi = output(out + "/M_imag.csv");
    for (int i = 0; i < m->NR; ++i) {
      for (int j = 0; j < m->NC; ++j) {
        if (j) { fr << ','; fi << ','; }
        const cdouble z = m->GetEntry(i, j);
        fr << real(z); fi << imag(z);
      }
      fr << '\n'; fi << '\n';
    }
    fr.close(); fi.close();
    vector_csv(kn, out + "/b.csv");
    double export_s = elapsed(t);
    t = Clock::now();
    const int lu_info = m->LUFactorize();
    const double lu_s = elapsed(t);
    if (lu_info != 0) throw std::runtime_error("LUFactorize info=" + std::to_string(lu_info));
    t = Clock::now();
    const int solve_info = m->LUSolve(kn);
    const double solve_s = elapsed(t);
    if (solve_info != 0) throw std::runtime_error("LUSolve info=" + std::to_string(solve_info));
    t = Clock::now();
    vector_csv(kn, out + "/x.csv");
    export_s += elapsed(t);
    t = Clock::now();
    GetPortVoltages(&g, kn, ports, num_ports, current, omega, voltage);
    const double voltage_s = elapsed(t);
    // SCUFF fields use exp(-iwt); RF circuit impedance uses exp(+iwt).
    const cdouble zin = conj(voltage[0] / current[0]);
    const cdouble s11 = (zin - 50.0) / (zin + 50.0);
    auto meta = output(out + "/helper.json");
    meta << "{\n\"frequency_ghz\":" << frequency
         << ",\n\"omega\":" << real(omega)
         << ",\n\"triangles\":" << g.TotalPanels
         << ",\n\"unknowns\":" << kn->N
         << ",\n\"ports\":" << num_ports
         << ",\n\"surfaces\":" << g.NumSurfaces
         << ",\n\"port_positive_edges\":" << ports[0]->NumPEdges
         << ",\n\"port_negative_edges\":" << ports[0]->NumMEdges
         << ",\n\"lu_info\":" << lu_info << ",\n\"solve_info\":" << solve_info
         << ",\n\"Z_in\":[" << real(zin) << ',' << imag(zin) << ']'
         << ",\n\"S11\":[" << real(s11) << ',' << imag(s11) << ']'
         << ",\n\"timings_s\":{\"assembly\":" << assembly_s
         << ",\"rhs\":" << rhs_s << ",\"lu\":" << lu_s
         << ",\"solve\":" << solve_s << ",\"port_voltage\":" << voltage_s
         << ",\"csv_export\":" << export_s
         << ",\"helper_total\":" << elapsed(total_start) << "}\n}\n";
    meta.close();
    std::cout << std::setprecision(17) << "N=" << kn->N << " Z_in=" << zin
              << " S11=" << s11 << "\n";
    delete m;
    delete kn;
    return 0;
  } catch (const std::exception &e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
