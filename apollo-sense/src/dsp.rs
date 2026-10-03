extern "C" {
    fn dsp_rms_db(samples: *const f32, n: usize) -> f64;
    fn dsp_peak_db(samples: *const f32, n: usize) -> f64;
}

pub const DEFAULT_SILENCE_THRESHOLD_DB: f64 = -50.0;

pub fn rms_db(samples: &[f32]) -> f64 {
    unsafe { dsp_rms_db(samples.as_ptr(), samples.len()) }
}

pub fn peak_db(samples: &[f32]) -> f64 {
    unsafe { dsp_peak_db(samples.as_ptr(), samples.len()) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rms_db_of_constant_amplitude() {
        let db = rms_db(&vec![0.1f32; 1000]);
        assert!((db - (-20.0)).abs() < 0.1, "db={db}");
    }

    #[test]
    fn peak_db_may_exceed_zero_for_float_over_full_scale() {
        assert!(peak_db(&vec![2.0f32; 10]) > 0.0);
    }

    #[test]
    fn empty_buffer_is_floor() {
        let empty: Vec<f32> = Vec::new();
        assert!(rms_db(&empty) <= -120.0 + 1e-9);
    }
}
