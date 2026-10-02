ROOT ?= $(CURDIR)
.DEFAULT_GOAL := all
SRC = $(ROOT)/library/source
OUTPUT ?= $(ROOT)/test/bin/export_system.exe
PLANAR_OUTPUT ?= $(ROOT)/antenna/bin/export_planar.exe
RF = $(SRC)/applications/scuff-rf
RF_OBJECTS = $(addprefix $(RF)/,RWGPorts.o GetPortVoltages.o GetPanelPotentials.o EdgePanelInteractions.o)
INCLUDES = -DSCUFF $(foreach d,libhrutil libhmat libscuff libMatProp libIncField libMDInterp libSGJC libSubstrate libTriInt,-I$(SRC)/libs/$(d)) -I$(RF)

.PHONY: all
all: $(OUTPUT) $(PLANAR_OUTPUT)

$(OUTPUT): $(ROOT)/test/src/export_system.cc $(RF_OBJECTS) $(SRC)/libs/libscuff/.libs/libscuff.a $(ROOT)/scripts/helper.mk
	$(SRC)/libtool --mode=link g++ -O2 -std=c++11 -fopenmp $(CPPFLAGS) $(INCLUDES) $(ROOT)/test/src/export_system.cc $(RF_OBJECTS) $(SRC)/libs/libscuff/libscuff.la -o $@

$(PLANAR_OUTPUT): $(ROOT)/antenna/native/export_planar.cc $(SRC)/libs/libscuff/.libs/libscuff.a $(ROOT)/scripts/helper.mk
	$(SRC)/libtool --mode=link g++ -O2 -std=c++11 -fopenmp $(CPPFLAGS) $(INCLUDES) -I$(SRC)/libs/libscuffSolver $(ROOT)/antenna/native/export_planar.cc $(SRC)/libs/libscuff/libscuff.la -o $@
