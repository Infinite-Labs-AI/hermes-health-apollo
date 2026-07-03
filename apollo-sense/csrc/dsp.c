#include "dsp.h"

#include <math.h>

static double to_dbfs(double magnitude) {
    if (magnitude <= 0.0) {
        return DSP_SILENCE_FLOOR_DB;
    }
    return 20.0 * log10(magnitude / DSP_DBFS_REFERENCE);
}

double dsp_rms_db(const float *samples, size_t n) {
    if (samples == NULL || n == 0) {
        return DSP_SILENCE_FLOOR_DB;
    }
    double sum_sq = 0.0;
    for (size_t i = 0; i < n; i++) {
        double s = (double)samples[i];
        sum_sq += s * s;
    }
    return to_dbfs(sqrt(sum_sq / (double)n));
}

double dsp_peak_db(const float *samples, size_t n) {
    if (samples == NULL || n == 0) {
        return DSP_SILENCE_FLOOR_DB;
    }
    double peak = 0.0;
    for (size_t i = 0; i < n; i++) {
        double m = fabs((double)samples[i]);
        if (m > peak) {
            peak = m;
        }
    }
    return to_dbfs(peak);
}
