#include <scuffSolver.h>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using namespace scuff;
using Clock = std::chrono::steady_clock;

static double elapsed(Clock::time_point start) {
  return std::chrono::duration<double>(Clock::now() - start).count();
}

static std::ofstream output(const std::string &path, bool binary = false) {
  std::ofstream stream;
  stream.exceptions(std::ios::failbit | std::ios::badbit);
  stream.open(path, std::ios::out | (binary ? std::ios::binary : std::ios::openmode(0)));
  stream << std::setprecision(17);
  return stream;
}

static void write_complex(std::ofstream &stream, cdouble value) {
  double pair[2] = {real(value), imag(value)};
  stream.write(reinterpret_cast<const char *>(pair), sizeof(pair));
}

static void export_matrix(HMatrix *matrix, const std::string &path) {
  auto stream = output(path, true);
  for (int r = 0; r < matrix->NR; ++r)
    for (int c = 0; c < matrix->NC; ++c)
      write_complex(stream, matrix->GetEntry(r, c));
  stream.close();
}

static void export_vector(HVector *vector, const std::string &path) {
  auto stream = output(path, true);
  for (int i = 0; i < vector->N; ++i) write_complex(stream, vector->GetEntry(i));
  stream.close();
}

static void export_basis(RWGSurface *surface, const std::string &directory) {
  auto vertices = output(directory + "/vertices.csv");
  for (int i = 0; i < surface->NumVertices; ++i)
    vertices << surface->Vertices[3*i] << ',' << surface->Vertices[3*i+1]
             << ',' << surface->Vertices[3*i+2] << '\n';
  vertices.close();
  auto panels = output(directory + "/triangles.csv");
  for (int i = 0; i < surface->NumPanels; ++i) {
    RWGPanel *panel = surface->Panels[i];
    panels << panel->VI[0] << ',' << panel->VI[1] << ',' << panel->VI[2] << '\n';
  }
  panels.close();
  auto basis = output(directory + "/rwg.csv");
  for (int i = 0; i < surface->NumEdges; ++i) {
    RWGEdge *edge = surface->Edges[i];
    basis << edge->iV1 << ',' << edge->iV2 << ',' << edge->iQP << ',' << edge->iQM
          << ',' << edge->iPPanel << ',' << edge->iMPanel << '\n';
  }
  basis.close();
}

static bool read_gap(const char *path, double line[6]) {
  std::ifstream stream(path);
  std::string kind;
  stream >> kind;
  if (kind != "DELTA_GAP") return false;
  for (int i=0; i<6; ++i)
    if (!(stream >> line[i]) || !std::isfinite(line[i]))
      throw std::runtime_error("Invalid voltage-gap coordinates");
  if (line[0] != line[3] || line[2] != 0 || line[5] != 0 || line[4] <= line[1])
    throw std::runtime_error("Expected a planar voltage-gap line along +y");
  return true;
}

static std::vector<double> gap_weights(RWGSurface *surface, const double line[6], int &count) {
  std::vector<double> weights(surface->NumEdges, 0.0);
  double width = line[4]-line[1], length = 0, tol = width*1e-8;
  count = 0;
  for (int i=0; i<surface->NumEdges; ++i) {
    RWGEdge *edge = surface->Edges[i];
    double *a=surface->Vertices+3*edge->iV1, *b=surface->Vertices+3*edge->iV2;
    if (std::abs(a[0]-line[0]) > tol || std::abs(b[0]-line[0]) > tol ||
        a[1] < line[1]-tol || b[1] < line[1]-tol || a[1] > line[4]+tol || b[1] > line[4]+tol) continue;
    // RWG normal trace is +/-1. Integrating it along the gap gives +/-edge length.
    weights[i] = (surface->Vertices[3*edge->iQP] < line[0] ? 1 : -1)*edge->Length;
    length += edge->Length;
    ++count;
  }
  if (std::abs(length-width) > tol || count == 0)
    throw std::runtime_error("Voltage-gap line does not match the internal RWG mesh edges");
  return weights;
}

static cdouble gap_impedance(HVector *currents, const std::vector<double> &weights) {
  cdouble current=0;
  for (int i=0; i<currents->N; ++i) current += weights[i]*currents->GetEntry(i);
  if (std::abs(current) < 1e-30) throw std::runtime_error("Zero voltage-gap current");
  return 1.0/current;
}

static void initialize(scuffSolver &solver, char **argv, int max_unknowns, bool gap) {
  solver.SetGeometryFile(argv[1]);
  if (!gap) solver.SetPortFile(argv[2]);
  solver.SetSubstrateFile(argv[3]);
  solver.InitGeometry();
  if (solver.G->NumSurfaces != 1 || !solver.G->Surfaces[0]->IsPEC || solver.G->LDim != 0)
    throw std::runtime_error("Expected a single, nonperiodic PEC metal mesh");
  if (!gap && (solver.NumPorts != 1 || solver.PortList->Ports[0]->PortEdges[_PLUS].empty()
      || !solver.PortList->Ports[0]->PortEdges[_MINUS].empty()))
    throw std::runtime_error("Expected one positive point port referenced to implicit ground");
  if (solver.G->TotalBFs < 1 || solver.G->TotalBFs > max_unknowns)
    throw std::runtime_error("SCUFF unknown count is outside the configured limit");
  RWGSurface *surface = solver.G->Surfaces[0];
  for (int i = 0; i < surface->NumVertices; ++i)
    if (std::abs(surface->Vertices[3*i+2]) > 1e-10)
      throw std::runtime_error("All metal vertices must lie in z=0");
}

int main(int argc, char **argv) {
  try {
    if (argc != 8)
      throw std::runtime_error("Usage: export_planar geometry ports substrate GHz out max_unknowns verify");
    static_assert(sizeof(double) == 8, "Export requires 64-bit doubles");
    std::uint16_t endian = 1;
    if (*reinterpret_cast<unsigned char *>(&endian) != 1)
      throw std::runtime_error("Export requires little-endian storage");
    double frequency = std::stod(argv[4]);
    int max_unknowns = std::stoi(argv[6]);
    if (!std::isfinite(frequency) || frequency <= 0 || max_unknowns < 1)
      throw std::runtime_error("Invalid frequency or unknown limit");
    const std::string directory = argv[5];
    SetLogFileName((directory + "/scuff.log").c_str());
    scuffSolver solver("pixel_antenna");
    double line[6];
    bool gap = read_gap(argv[2], line);
    initialize(solver, argv, max_unknowns, gap);
    int port_edges=0;
    std::vector<double> weights;
    if (gap) weights = gap_weights(solver.G->Surfaces[0], line, port_edges);
    else port_edges = solver.PortList->Ports[0]->PortEdges[_PLUS].size();
    // The library's GHz macro rounds c to 300 mm/ns; match FEKO using exact SI c.
    const double omega = 2.0*M_PI*frequency/299.792458;
    solver.G->UpdateCachedEpsMuValues(omega);
    solver.M = solver.G->AllocateBEMMatrix();
    auto start = Clock::now();
    // Same assembly as AssembleSystemMatrix, exposed before its in-place LU.
    AssembleMOIMatrix(solver.G, omega, solver.M);
    double assembly_s = elapsed(start);
    solver.OmegaSIE = omega;
    start = Clock::now();
    if (!gap) solver.AssemblePortBFInteractionMatrix(omega);
    solver.KN = solver.G->AllocateRHSVector();
    // MOI M is the tested self-field divided by ZVAC; E_self = -E_impressed.
    // Both coordinates and RWG current density use the same native length unit.
    for (int i = 0; i < solver.KN->N; ++i)
      solver.KN->SetEntry(i, gap ? cdouble(-weights[i]/ZVAC, 0) : solver.PBFIMatrix->GetEntry(i, 0));
    double rhs_s = elapsed(start);
    export_matrix(solver.M, directory + "/M.bin");
    export_vector(solver.KN, directory + "/b.bin");
    export_basis(solver.G->Surfaces[0], directory);
    start = Clock::now();
    int lu_info = solver.M->LUFactorize();
    double lu_s = elapsed(start);
    if (lu_info) throw std::runtime_error("LUFactorize failed: " + std::to_string(lu_info));
    start = Clock::now();
    int solve_info = solver.M->LUSolve(solver.KN);
    double solve_s = elapsed(start);
    if (solve_info) throw std::runtime_error("LUSolve failed: " + std::to_string(solve_info));
    export_vector(solver.KN, directory + "/x.bin");
    start = Clock::now();
    cdouble zin;
    if (gap) zin = gap_impedance(solver.KN, weights);
    else {
      HMatrix *impedance = solver.GetZMatrix();
      zin = impedance->GetEntry(0, 0);
      delete impedance;
    }
    if (!std::isfinite(real(zin)) || !std::isfinite(imag(zin)))
      throw std::runtime_error("Non-finite port impedance");
    double impedance_s = elapsed(start);
    if (std::stoi(argv[7])) {
      scuffSolver reference("reference");
      initialize(reference, argv, max_unknowns, gap);
      if (gap) {
        reference.G->UpdateCachedEpsMuValues(omega);
        reference.M = reference.G->AllocateBEMMatrix();
        AssembleMOIMatrix(reference.G, omega, reference.M);
        reference.KN = reference.G->AllocateRHSVector();
        for (int i=0; i<reference.KN->N; ++i) reference.KN->SetEntry(i, -weights[i]/ZVAC);
        if (reference.M->LUFactorize() || reference.M->LUSolve(reference.KN))
          throw std::runtime_error("Reference voltage-gap solve failed");
      } else {
        reference.AssembleSystemMatrix(omega);
        reference.Solve(0, cdouble(1.0, 0.0));
      }
      export_vector(reference.KN, directory + "/x_reference.bin");
      HMatrix *reference_z = gap ? new HMatrix(1, 1, LHM_COMPLEX) : reference.GetZMatrix();
      if (gap) reference_z->SetEntry(0, 0, gap_impedance(reference.KN, weights));
      export_matrix(reference_z, directory + "/z_reference.bin");
      delete reference_z;
    }
    auto meta = output(directory + "/native.json");
    RWGSurface *surface = solver.G->Surfaces[0];
    double rho_range[2];
    GetRhoMinMax(solver.G, rho_range);
    cdouble port_current = gap ? 1.0/zin : cdouble(1.0, 0.0);
    cdouble port_voltage = gap ? cdouble(1.0, 0.0) : zin;
    meta << "{\"unknowns\":" << solver.G->TotalBFs
         << ",\"triangles\":" << solver.G->TotalPanels
         << ",\"port_edges\":" << port_edges
         << ",\"feed_model\":\"" << (gap ? "planar_delta_gap_voltage" : "point_port") << '"'
         << ",\"omega\":" << omega << ",\"lu_info\":" << lu_info
         << ",\"solve_info\":" << solve_info
         << ",\"input_impedance_ohm\":[" << real(zin) << ',' << imag(zin) << ']'
         << ",\"port_voltage_V\":[" << real(port_voltage) << ',' << imag(port_voltage) << ']'
         << ",\"port_current_A\":[" << real(port_current) << ',' << imag(port_current) << ']'
         << ",\"raw_format\":\"complex128 little-endian row-major\""
         << ",\"surface_bounds_mm\":[[" << surface->RMin[0] << ',' << surface->RMin[1] << ',' << surface->RMin[2]
         << "],[" << surface->RMax[0] << ',' << surface->RMax[1] << ',' << surface->RMax[2] << "]]"
         << ",\"rho_range_mm\":[" << rho_range[0] << ',' << rho_range[1] << ']'
         << ",\"timings_s\":{\"assembly\":" << assembly_s << ",\"rhs\":" << rhs_s
         << ",\"lu\":" << lu_s << ",\"solve\":" << solve_s
         << ",\"impedance\":" << impedance_s << "}}\n";
    meta.close();
    std::cout << "N=" << solver.G->TotalBFs << " system exported successfully\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
