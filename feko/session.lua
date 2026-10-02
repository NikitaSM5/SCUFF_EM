-- One owned CADFEKO instance accepts generated jobs until the experiment ends.
local socket = require("socket")
local lfs = require("lfs")
local root = lfs.currentdir()
local application = cf.Application.GetInstance()
SCUFF_FEKO_SESSION = true

local function state(value, detail)
    local file = assert(io.open(root .. "/session_status.txt", "w"))
    file:write(value, "\n", detail or "", "\n")
    file:close()
end

local function serve()
    state("ready")
    while true do
        local file = io.open(root .. "/request.txt", "r")
        if file then
            local directory = file:read("*l")
            local script = file:read("*l")
            file:close()
            assert(os.remove(root .. "/request.txt"))
            if directory == "STOP" then break end
            state("busy", directory)
            assert(lfs.chdir(directory))
            dofile(assert(script))
            application:CloseProject()
            assert(lfs.chdir(root))
            state("ready")
        else
            socket.sleep(0.05)
        end
    end
end

local ok, message = xpcall(serve, debug.traceback)
state(ok and "stopped" or "failed", ok and "" or tostring(message))
application:Exit()
