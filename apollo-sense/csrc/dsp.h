#ifndef APOLLO_DSP_H
#define APOLLO_DSP_H

#include <stddef.h>

#define DSP_DBFS_REFERENCE (1.0)

#define DSP_SILENCE_FLOOR_DB (-120.0)

double dsp_rms_db(const float *samples, size_t n);

double dsp_peak_db(const float *samples, size_t n);

#endif
