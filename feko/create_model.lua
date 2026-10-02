-- CADFEKO 2022.2 / 2024 API. Generated parameters never contain user Lua code.
-- GENERATED_PARAMETERS

-- A small line protocol keeps status readable without a third-party JSON module.
local has_socket, socket = pcall(require, "socket")
local wall_time = has_socket and socket.gettime or os.time
local function status(state, stage, detail)
    local timing = assert(io.open("stage_times.tsv", "a"))
    timing:write(stage, "\t", string.format("%.6f", wall_time()), "\n")
    timing:close()
    local file = assert(io.open("feko_status.txt", "w"))
    file:write(state, "\n", stage, "\n", detail or "", "\n")
    file:close()
end

local function build()
    status("running", "geometry")
    local application = cf.Application.GetInstance()
    local project = application:NewProject()
    project.ModelAttributes.Unit = cf.Enums.ModelUnitEnum.Millimetres
    local geometry = project.Contents.Geometry
    local parts = {}
    for index, pixel in ipairs(pixels) do
        local part = geometry:AddRectangle(cf.Point(pixel[1], pixel[2], 0), pixel[3], pixel[4])
        part.Label = "Pixel_" .. index
        parts[#parts+1] = part
    end
    local antenna = geometry:Union(parts)
    antenna.Label = "PixelAntenna"
    for index=1, antenna.Faces.Count do
        antenna.Faces[index].Medium = project.Definitions.Media.PerfectElectricConductor
    end
    local positive = antenna.Faces:ClosestTo(cf.Point(feed_x+feed_width/4, feed_y, 0))
    local negative = antenna.Faces:ClosestTo(cf.Point(feed_x-feed_width/4, feed_y, 0))
    assert(positive.Label ~= negative.Label, "Voltage-gap faces were merged")
    local port = project.Contents.Ports:AddEdgePort({positive}, {negative})
    port.Label = "FeedGap"

    status("running", "substrate")
    local dielectric = project.Definitions.Media.Dielectric:AddDielectric()
    dielectric.Label = "Substrate"
    local material = dielectric:GetProperties()
    material.DielectricModelling.DefinitionMethod = cf.Enums.MediumDielectricDefinitionMethodEnum.FrequencyIndependent
    material.DielectricModelling.ConductivityType = cf.Enums.MediumDielectricConductivityTypeEnum.LossTangent
    material.DielectricModelling.RelativePermittivity = eps_r
    material.DielectricModelling.LossTangent = tan_delta
    dielectric:SetProperties(material)
    local ground = project.Contents.SolutionSettings.GroundPlane:GetProperties()
    ground.DefinitionMethod = cf.Enums.GroundPlaneDefinitionMethodEnum.MultilayerSubstrate
    ground.ZValue = 0
    ground.Layers = {{Thickness=h, Medium=dielectric, GroundBottom=cf.Enums.GroundBottomTypeEnum.PEC}}
    project.Contents.SolutionSettings.GroundPlane:SetProperties(ground)

    status("running", "requests")
    local configurations = project.Contents.SolutionConfigurations
    configurations:SetSourcesPerConfiguration()
    local radiation = configurations[1]
    radiation.Label = "Radiation"
    local source = radiation.Sources:AddVoltageSource(port)
    source.Label = "FeedVoltage"
    source.Magnitude = 1
    source.Phase = 0
    source.Impedance = z0
    if compute_pattern then
        local farfield = radiation.FarFields:Add(0, 0, 90, 360, angle_step, angle_step)
        farfield.Label = "Gain_UpperHemisphere"
        farfield.Advanced.RequestType = cf.Enums.FarFieldRequestTypeEnum.Gain
        farfield.Advanced.ExportSettings.ASCIIEnabled = true
    end

    local scattering = configurations:AddMultiportSParameter({port})
    scattering.Label = "Reflection"
    scattering.SParameter.PortProperties[1].Impedance = z0
    scattering.SParameter.PortProperties[1].Active = true
    scattering.SParameter.TouchstoneExportEnabled = true
    local frequency_settings = configurations.GlobalFrequency:GetProperties()
    if sweep then
        frequency_settings.RangeType = cf.Enums.FrequencyRangeTypeEnum.LinearSpacedDiscrete
        frequency_settings.Start = f_start
        frequency_settings.End = f_stop
        frequency_settings.NumberOfDiscreteValues = f_count
    else
        frequency_settings.RangeType = cf.Enums.FrequencyRangeTypeEnum.Single
        frequency_settings.Start = frequency
    end
    configurations.GlobalFrequency:SetProperties(frequency_settings)

    status("running", "mesh")
    local mesh = project.Mesher.Settings
    mesh.MeshSizeOption = cf.Enums.MeshSizeOptionEnum.Custom
    mesh.TriangleEdgeLength = a_mesh
    -- Save before meshing too, so an input model survives a mesher failure.
    application:SaveAs("antenna.cfx")
    project.Mesher:Mesh()
    application:SaveAs("antenna.cfx")
    if run_solver then
        status("running", "solver")
        local result = application.Launcher:RunFEKO()
        local log = assert(io.open("solver.log", "w"))
        log:write(result.Output or "", "\n", result.Errors or "")
        log:close()
        assert(result.Succeeded, "FEKO failed, exit code " .. tostring(result.ExitCode) .. ": " .. tostring(result.Errors))
        application:SaveAs("antenna.cfx")
        status("done", "solved")
    else
        status("done", "model_created")
    end
end

local ok, message = xpcall(build, debug.traceback)
if not ok then
    status("failed", "error", tostring(message))
    print(message)
elseif run_solver then
    -- The project and solver files are saved before the final status is written.
    if not SCUFF_FEKO_SESSION then cf.Application.GetInstance():Exit() end
end
