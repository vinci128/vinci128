CXX      = g++
CXXFLAGS = -O2 -std=c++17 -Wall -Wextra
LDFLAGS  = -lm

TARGET = elliptic_feynman

all: $(TARGET)

$(TARGET): elliptic_feynman.cpp
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f $(TARGET)

.PHONY: all clean
